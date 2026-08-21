#!/usr/bin/env python3
"""Semantic search over markdown/text files in the current working directory.

v3: adds (1) pytest suite via tests/ + conftest.py, (2) on-disk embedding
    cache at $XDG_CACHE_HOME/semantic-search/, (3) --stats flag, (4) --quiet
    flag, (5) per-file model column + meta table for dim-safety + same-dim
    auto re-embed, (6) parallel embedding via concurrent.futures (env:
    SEMANTIC_SEARCH_EMBED_WORKERS), (7) .rst + .org heading detection.

v2 (predecessor): incremental sha1 indexing, hybrid BM25+cosine via FTS5+RRF,
    JSON output, line numbers, git-root detection, .txt/.rst/.org file
    support (heading-less for non-md), --in path filter.

Usage:
    python3 -B search.py --index                  # build / refresh incrementally
    python3 -B search.py --rebuild                # drop and rebuild from scratch
    python3 -B search.py "query"                  # query (hybrid by default)
    python3 -B search.py "query" --json           # JSONL output
    python3 -B search.py --git-root --index       # index from nearest .git root
    python3 -B search.py --in 'docs/**' "query"   # scope to subtree (repeatable)
    python3 -B search.py --stats                  # print index summary
    python3 -B search.py --quiet --index          # silent unless work was done
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

DB_FILE = ".semantic-index.db"
MODEL = "text-embedding-3-small"
DIM = 1536
CHUNK_WORDS = 400
OVERLAP_WORDS = 50
TOP_K = 5
MAX_INPUT_CHARS = 24_000
MAX_CHARS_PER_REQUEST = 800_000
MAX_ITEMS_PER_REQUEST = 2048
MAX_RETRIES = 5
BACKOFF_BASE_S = 1.0
SCHEMA_VERSION = 3
RRF_K = 60
EMBEDDING_DIM_KEY = "embedding_dim"

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".org")
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".turbo", ".cache",
    "target", ".tox", "site-packages", ".pytest_cache",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
RST_UNDERLINE_RE = re.compile(r'^([=\-~^"*+#])\1+\s*$')
ORG_HEADING_RE = re.compile(r"^(\*+)\s+(.+?)\s*$")
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")
FTS_TOKEN_RE = re.compile(r"[\w\-]+")

_QUIET = False
_CACHE_WARNED = False


def load_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith("OPENAI_API_KEY="):
                v = line.split("=", 1)[1].strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if v:
                    return v
    print("ERROR: OPENAI_API_KEY not found in environment or ~/.env", file=sys.stderr)
    sys.exit(1)


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if fn.lower().endswith(SUPPORTED_SUFFIXES):
                yield Path(dirpath) / fn


def _sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(65536)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _glob_to_regex(pattern: str) -> str:
    """Path-segment-aware glob → regex.

    * matches any chars except '/'
    ** matches any chars including '/' (and swallows a trailing '/')
    ? matches one char except '/'
    """
    try:
        from glob import translate as _glob_translate
        # include_hidden=True so an explicit pattern like ".config/**" matches
        # dot-prefixed paths (the user wrote the dot deliberately). With the
        # default include_hidden=False, glob.translate emits a regex that
        # rejects path components starting with '.', silently breaking such
        # scopes on Python 3.13+.
        return _glob_translate(pattern, recursive=True, include_hidden=True)
    except (ImportError, AttributeError, TypeError):
        pass
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in ".+()|^$\\{}[]":
            out.append("\\" + c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "(?s:" + "".join(out) + ")\\Z"


def _compile_in_matcher(patterns: list[str] | None) -> Callable[[str], bool]:
    if not patterns:
        return lambda p: True
    combined = "|".join(_glob_to_regex(p) for p in patterns)
    rx = re.compile(combined)
    return lambda p: bool(rx.match(p))


def chunk_text(text: str, *, fmt: str = "md") -> list[tuple[str, str, int]]:
    """Split text into (heading, chunk_text, line_start) tuples.

    fmt ∈ {"md", "rst", "org", "plain"}. Markdown headings (# ... ######)
    are recognized only in fmt="md". rst recognizes title-underline pairs
    (`=`, `-`, `~`, etc.). org recognizes lines starting with `*`/`**`/...
    plain ignores all heading syntax.

    line_start is 1-indexed and refers to the source line of the chunk's
    first word. Headings are hard chunk breaks; no overlap across sections.
    """
    if fmt == "rst":
        return _iter_rst_chunks(text)
    if fmt == "org":
        return _iter_org_chunks(text)
    # md or plain
    chunks: list[tuple[str, str, int]] = []
    current_heading = ""
    buf: list[tuple[str, int]] = []

    def flush() -> None:
        if buf:
            chunks.append((
                current_heading,
                " ".join(w for w, _ in buf),
                buf[0][1],
            ))

    for line_idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if fmt == "md":
            m = HEADING_RE.match(stripped)
            if m:
                flush()
                buf = []
                current_heading = m.group(2).strip()
                continue
        for w in stripped.split():
            buf.append((w, line_idx))
            if len(buf) >= CHUNK_WORDS:
                chunks.append((
                    current_heading,
                    " ".join(x for x, _ in buf),
                    buf[0][1],
                ))
                buf = buf[-OVERLAP_WORDS:]
    flush()
    return chunks


def _iter_rst_chunks(text: str) -> list[tuple[str, str, int]]:
    """RST: a title is a non-empty line followed by a line of repeated
    `=-~^"*+#` chars whose length >= title length."""
    lines = text.splitlines()
    chunks: list[tuple[str, str, int]] = []
    current_heading = ""
    buf: list[tuple[str, int]] = []

    def flush() -> None:
        if buf:
            chunks.append((
                current_heading,
                " ".join(w for w, _ in buf),
                buf[0][1],
            ))

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()
        # Detect heading: this line non-empty AND next line is underline of >= len.
        if (
            i + 1 < n
            and stripped.strip()
            and RST_UNDERLINE_RE.match(lines[i + 1].rstrip())
            and len(lines[i + 1].rstrip()) >= len(stripped)
        ):
            flush()
            buf = []
            current_heading = stripped.strip()
            i += 2
            continue
        # Otherwise: accumulate words.
        for w in stripped.split():
            buf.append((w, i + 1))
            if len(buf) >= CHUNK_WORDS:
                chunks.append((
                    current_heading,
                    " ".join(x for x, _ in buf),
                    buf[0][1],
                ))
                buf = buf[-OVERLAP_WORDS:]
        i += 1
    flush()
    return chunks


def _iter_org_chunks(text: str) -> list[tuple[str, str, int]]:
    """Org: lines starting with `*`/`**`/... + space + text are headings."""
    chunks: list[tuple[str, str, int]] = []
    current_heading = ""
    buf: list[tuple[str, int]] = []

    def flush() -> None:
        if buf:
            chunks.append((
                current_heading,
                " ".join(w for w, _ in buf),
                buf[0][1],
            ))

    for line_idx, line in enumerate(text.splitlines(), start=1):
        m = ORG_HEADING_RE.match(line.rstrip())
        if m:
            flush()
            buf = []
            current_heading = m.group(2).strip()
            continue
        for w in line.strip().split():
            buf.append((w, line_idx))
            if len(buf) >= CHUNK_WORDS:
                chunks.append((
                    current_heading,
                    " ".join(x for x, _ in buf),
                    buf[0][1],
                ))
                buf = buf[-OVERLAP_WORDS:]
    flush()
    return chunks


def _fts_sanitize(q: str) -> str:
    tokens = FTS_TOKEN_RE.findall(q)
    if not tokens:
        return ""
    parts: list[str] = []
    for t in tokens:
        t = t.replace('"', '""')
        parts.append(f'"{t}"')
    return " OR ".join(parts)


# ---- Embedding cache ----

def _cache_disabled() -> bool:
    return os.environ.get("SEMANTIC_SEARCH_NO_CACHE", "") == "1"


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "semantic-search"


def _cache_key(text: str) -> str:
    h = hashlib.sha256()
    h.update(MODEL.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _cache_path(text: str) -> Path:
    key = _cache_key(text)
    return _cache_dir() / key[:2] / (key[2:] + ".bin")


def _cache_warn_once(msg: str) -> None:
    global _CACHE_WARNED
    if not _CACHE_WARNED:
        print(f"cache warning: {msg}", file=sys.stderr)
        _CACHE_WARNED = True


def _cache_get(text: str) -> bytes | None:
    if _cache_disabled():
        return None
    try:
        p = _cache_path(text)
        if p.exists():
            return p.read_bytes()
    except OSError:
        return None
    return None


def _cache_put(text: str, vec_bytes: bytes) -> None:
    if _cache_disabled():
        return
    try:
        final = _cache_path(text)
        parent = final.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(dir=parent, delete=False)
        try:
            tmp.write(vec_bytes)
            tmp.close()
            os.replace(tmp.name, final)
        except OSError:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
    except OSError as exc:
        _cache_warn_once(f"cache write failed: {exc}")


# ---- Embeddings (parallel, bytes-everywhere) ----

def _embed_one_batch(batch: list[str], api_key: str,
                     cancel_event=None) -> list[bytes]:
    """Embed a single batch. api_key is resolved by the caller (main thread).
    cancel_event (threading.Event | None) lets the orchestrator short-circuit
    pending batches between retries on first failure."""
    import openai
    import sqlite_vec
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("embed_all: cancelled by sibling batch failure")
    client = openai.OpenAI(api_key=api_key)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("embed_all: cancelled by sibling batch failure")
        try:
            resp = client.embeddings.create(input=batch, model=MODEL)
            return [sqlite_vec.serialize_float32(item.embedding) for item in resp.data]
        except (openai.RateLimitError, openai.APIError, openai.APIConnectionError) as exc:
            last_err = exc
            if attempt + 1 == MAX_RETRIES:
                raise
            time.sleep(BACKOFF_BASE_S * (2 ** attempt))
    raise RuntimeError(f"OpenAI embeddings failed: {last_err}")


def embed_all(texts: list[str]) -> list[bytes]:
    """Embed `texts` and return one little-endian-float32 blob per input,
    in input order. No-op for empty input. Parallelizes batches via
    SEMANTIC_SEARCH_EMBED_WORKERS (default 4, clamped to [1, 16]).

    Note: on first failure, pending batches are short-circuited via a
    threading.Event, but already-in-flight OpenAI requests run to completion
    (the Python openai SDK uses sync httpx with no cancellation). Calls
    `load_openai_key()` once in the main thread before dispatching workers,
    so a missing OPENAI_API_KEY produces a clean exit rather than a
    SystemExit raised inside a worker.
    """
    if not texts:
        return []
    inputs = [t[:MAX_INPUT_CHARS] for t in texts]

    batches: list[list[str]] = []
    cur: list[str] = []
    cur_chars = 0
    for t in inputs:
        if cur and (
            cur_chars + len(t) > MAX_CHARS_PER_REQUEST
            or len(cur) >= MAX_ITEMS_PER_REQUEST
        ):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(t)
        cur_chars += len(t)
    if cur:
        batches.append(cur)

    # Resolve key once in main thread — SystemExit in a worker bypasses the
    # parent's `except Exception:` and leaves the pool dangling.
    api_key = load_openai_key()

    if len(batches) == 1:
        return _embed_one_batch(batches[0], api_key)

    try:
        workers = int(os.environ.get("SEMANTIC_SEARCH_EMBED_WORKERS", "4"))
    except ValueError:
        workers = 4
    workers = max(1, min(16, workers))

    cancel_event = threading.Event()
    results: list[list[bytes] | None] = [None] * len(batches)
    future_to_idx: dict[concurrent.futures.Future, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, batch in enumerate(batches):
            fut = pool.submit(_embed_one_batch, batch, api_key, cancel_event)
            future_to_idx[fut] = idx
        try:
            for fut in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[fut]
                results[idx] = fut.result()
                if not _QUIET:
                    print(
                        f"  batch {idx + 1}/{len(batches)} ({len(batches[idx])} items) done",
                        file=sys.stderr,
                    )
        except Exception:
            cancel_event.set()
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    flat: list[bytes] = []
    for r in results:
        if r is None:
            raise RuntimeError("embed_all: missing batch result (cancellation race)")
        flat.extend(r)
    return flat


# ---- RRF (extracted for tests) ----

def _rrf_fuse(
    vec_hits: list[tuple],
    fts_hits: list[tuple],
    top_k: int,
) -> list[tuple[int, float]]:
    """Reciprocal rank fusion. Inputs are rank-ordered lists (index 0 = best).
    Returns [(chunk_id, score), ...] in score-descending order, length <= top_k."""
    vec_rank = {row[0]: i + 1 for i, row in enumerate(vec_hits)}
    fts_rank = {row[0]: i + 1 for i, row in enumerate(fts_hits)}
    all_ids = set(vec_rank) | set(fts_rank)
    scored: list[tuple[int, float]] = []
    for cid in all_ids:
        s = 0.0
        if cid in vec_rank:
            s += 1.0 / (RRF_K + vec_rank[cid])
        if cid in fts_rank:
            s += 1.0 / (RRF_K + fts_rank[cid])
        scored.append((cid, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


# ---- SQLite driver ----

_SQLITE_DRIVER: tuple | None = None


def _resolve_sqlite_driver():
    import sqlite3 as _stdlib
    if hasattr(_stdlib.Connection, "enable_load_extension"):
        return _stdlib, "sqlite3"
    try:
        import apsw
        return apsw, "apsw"
    except ImportError:
        pass
    try:
        import pysqlite3
        if hasattr(pysqlite3.Connection, "enable_load_extension"):
            return pysqlite3, "sqlite3"
    except ImportError:
        pass
    print(
        "ERROR: no SQLite driver with extension support found.\n"
        "       Install one: pip install apsw   (recommended on macOS)\n"
        "                or: pip install pysqlite3-binary",
        file=sys.stderr,
    )
    sys.exit(1)


def _drop_all_tables(conn) -> None:
    conn.execute("BEGIN")
    for stmt in (
        "DROP TABLE IF EXISTS vec_chunks",
        "DROP TABLE IF EXISTS chunks_fts",
        "DROP TABLE IF EXISTS chunks",
        "DROP TABLE IF EXISTS files",
        "DROP TABLE IF EXISTS meta",
        "PRAGMA user_version = 0",
    ):
        conn.execute(stmt)
    conn.execute("COMMIT")


def _init_schema(conn) -> None:
    try:
        conn.execute("BEGIN")
        # Order is pinned per plan A.2.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                sha1 TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                model TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
            USING vec0(embedding float[{DIM}] distance_metric=cosine)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                section_heading TEXT,
                chunk_text TEXT NOT NULL,
                line_start INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(chunk_text, section_heading, content='chunks', content_rowid='id')
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid, chunk_text, section_heading)
              VALUES (new.id, new.chunk_text, new.section_heading);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts, rowid, chunk_text, section_heading)
              VALUES('delete', old.id, old.chunk_text, old.section_heading);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_ad_vec AFTER DELETE ON chunks BEGIN
              DELETE FROM vec_chunks WHERE rowid = old.id;
            END
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (EMBEDDING_DIM_KEY, str(DIM)),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        msg = str(exc).lower()
        if "fts5" in msg or "trigger" in msg or "vec0" in msg:
            print(
                "ERROR: your SQLite build lacks required FTS5+trigger+vec0 features.\n"
                "       Upgrade SQLite or use apsw>=3.46.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


def _migrate_v2_to_v3(conn) -> None:
    """Idempotent v2 → v3 migration. Safe on retry after partial failure.

    Note: the literals 'text-embedding-3-small' and '1536' below intentionally
    DO NOT use the MODEL / DIM constants. They record what v2 actually had
    stored — v2 only ever shipped with MODEL='text-embedding-3-small' and
    DIM=1536 (hard-coded, never configurable). If a v3 build changes MODEL,
    the migration correctly stamps the *old* model name so the same-dim stale
    check forces a re-embed on next --index. If v3 changes DIM, _check_dim
    correctly fires exit-3 (forcing --rebuild) because the migration recorded
    v2's true 1536 dim. Using current constants here would mislabel the data.
    """
    try:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "ALTER TABLE files ADD COLUMN model TEXT NOT NULL DEFAULT ''"
            )
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        conn.execute(
            "UPDATE files SET model = ? WHERE model = ''",
            ("text-embedding-3-small",),  # v2's literal default — intentional
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (EMBEDDING_DIM_KEY, "1536"),  # v2's literal DIM — intentional
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _check_dim(conn, db_path: Path) -> None:
    row = next(conn.execute("SELECT value FROM meta WHERE key = ?", (EMBEDDING_DIM_KEY,)), None)
    stored = row[0] if row else ""
    try:
        stored_dim = int(stored) if stored else -1
    except ValueError:
        stored_dim = -1
    if stored_dim != DIM:
        print(
            f"ERROR: embedding dimension mismatch at {db_path} "
            f"(stored={stored or '<missing>'}, current={DIM}). Run with --rebuild.",
            file=sys.stderr,
        )
        sys.exit(3)


def _has_table(conn, name: str) -> bool:
    row = next(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual') AND name = ?",
        (name,),
    ), None)
    if row:
        return True
    # vec0/fts5 virtual tables register as 'table' in sqlite_master; fallback.
    row = next(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ), None)
    return row is not None


def _has_column(conn, table: str, column: str) -> bool:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return True
    return False


def open_db(db_path: Path, *, force_rebuild: bool = False, allow_stale_dim: bool = False):
    global _SQLITE_DRIVER
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        print("ERROR: sqlite-vec not installed. Run: pip install sqlite-vec openai", file=sys.stderr)
        sys.exit(1)
    if _SQLITE_DRIVER is None:
        _SQLITE_DRIVER = _resolve_sqlite_driver()
    mod, kind = _SQLITE_DRIVER
    if kind == "sqlite3":
        conn = mod.connect(str(db_path), isolation_level=None)
    else:
        conn = mod.Connection(str(db_path))
    enable_ext = getattr(conn, "enable_load_extension", None) \
                 or getattr(conn, "enableloadextension", None)
    if enable_ext is None:
        print("ERROR: SQLite driver lacks an extension-loading API", file=sys.stderr)
        sys.exit(1)
    enable_ext(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    finally:
        enable_ext(False)
    # Prevent SQLITE_BUSY raw tracebacks on concurrent index writes; wait up
    # to 5 s for another writer to release before bubbling the error.
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass

    if force_rebuild:
        _drop_all_tables(conn)
        _init_schema(conn)
        _check_dim(conn, db_path)
        return conn

    version = next(conn.execute("PRAGMA user_version"))[0]
    has_chunks = _has_table(conn, "chunks")
    has_fts = _has_table(conn, "chunks_fts")
    has_files = _has_table(conn, "files")
    has_model = has_files and _has_column(conn, "files", "model")

    if version == SCHEMA_VERSION:
        if has_chunks and has_fts and has_files and has_model:
            if not allow_stale_dim:
                _check_dim(conn, db_path)
            return conn
        print(
            f"ERROR: corrupted v{SCHEMA_VERSION} schema at {db_path}; run with --rebuild.",
            file=sys.stderr,
        )
        sys.exit(3)

    if version == 2:
        if has_chunks and has_fts and has_files and not has_model:
            _migrate_v2_to_v3(conn)
            if not allow_stale_dim:
                _check_dim(conn, db_path)
            return conn
        print(
            f"ERROR: corrupted v2 schema at {db_path}; run with --rebuild.",
            file=sys.stderr,
        )
        sys.exit(3)

    if version == 0:
        if has_chunks:
            print(
                f"ERROR: v1 index detected at {db_path}. Run with --rebuild to upgrade.",
                file=sys.stderr,
            )
            sys.exit(3)
        _init_schema(conn)
        _check_dim(conn, db_path)
        return conn

    print(
        f"ERROR: unsupported schema version {version} at {db_path} "
        f"(this build expects {SCHEMA_VERSION}). Run with --rebuild to recreate.",
        file=sys.stderr,
    )
    sys.exit(3)


def _resolve_root(args) -> tuple[Path, bool]:
    """Return (root, git_root_detected). The second element is True only
    when --git-root was passed AND a .git directory was actually found."""
    if args.cwd:
        p = Path(args.cwd)
        if not p.is_absolute():
            print(f"ERROR: --cwd must be an absolute path (got {args.cwd!r})", file=sys.stderr)
            sys.exit(2)
        return p, False
    if args.git_root:
        here = Path.cwd().resolve()
        for candidate in (here, *here.parents):
            if (candidate / ".git").exists():
                if not _QUIET:
                    print(f"Indexing from git root: {candidate}", file=sys.stderr)
                return candidate, True
        if not _QUIET:
            print("--git-root: no .git found above cwd; using cwd", file=sys.stderr)
    return Path.cwd(), False


def _gitignore_check(root: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", DB_FILE],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 and not _QUIET:
            print(f"Tip: add {DB_FILE} to .gitignore", file=sys.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _fmt_for_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".md": "md",
        ".markdown": "md",
        ".rst": "rst",
        ".org": "org",
        ".txt": "plain",
    }.get(ext, "plain")


# ---- Indexing ----

def cmd_index(root: Path, db_path: Path, *, rebuild: bool,
              in_patterns: list[str] | None) -> None:
    conn = open_db(db_path, force_rebuild=rebuild)
    match = _compile_in_matcher(in_patterns)

    # `walked` carries (abs_path, mtime, sha1) for files visible on disk now.
    # If a file disappears between iter_files and stat/read, it is NOT added
    # to walked — preventing a downstream race where the classification path
    # marks it `changed` but the read_text call later returns nothing, leaving
    # a stale (chunk_count=0) `files` row.
    walked: dict[str, tuple[Path, float, str]] = {}
    for abs_path in iter_files(root):
        try:
            rel = str(abs_path.relative_to(root))
        except ValueError:
            continue
        if not match(rel):
            continue
        try:
            st = abs_path.stat()
            sha = _sha1_of_file(abs_path)
        except OSError:
            continue
        walked[rel] = (abs_path, st.st_mtime, sha)

    db_files: dict[str, dict] = {}
    for path, mtime, sha1, chunk_count, model in conn.execute(
        "SELECT path, mtime, sha1, chunk_count, model FROM files"
    ):
        db_files[path] = {
            "mtime": mtime, "sha1": sha1,
            "chunk_count": chunk_count, "model": model,
        }

    # Same-dim model swap: rows whose model != current MODEL are stale → force re-embed.
    unchanged_paths: set[str] = set()
    changed_paths: set[str] = set()
    new_paths: set[str] = set()
    for p, (_, _, sha) in walked.items():
        if p not in db_files:
            new_paths.add(p)
        elif db_files[p]["sha1"] == sha and db_files[p]["model"] == MODEL:
            unchanged_paths.add(p)
        else:
            changed_paths.add(p)

    if in_patterns:
        orphan_candidates = {p for p in db_files if match(p)}
        orphans = orphan_candidates - set(walked.keys())
    else:
        orphans = set(db_files.keys()) - set(walked.keys())

    if not (changed_paths or new_paths or orphans):
        if not _QUIET:
            print(
                f"Indexed 0 new + 0 changed + 0 removed, "
                f"skipped {len(unchanged_paths)} unchanged",
                file=sys.stderr,
            )
        return

    pending: list[tuple[str, str, str, int, float, str]] = []
    read_failed: set[str] = set()
    for rel in sorted(changed_paths | new_paths):
        abs_path, mtime, sha = walked[rel]
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            # File disappeared between sha1 and read_text. Drop it from the
            # classification entirely so we don't write a stale (chunk_count=0)
            # row or destroy the prior good row's chunks.
            read_failed.add(rel)
            continue
        fmt = _fmt_for_path(abs_path)
        for heading, ctext, line_start in chunk_text(text, fmt=fmt):
            if ctext.strip():
                pending.append((rel, heading, ctext, line_start, mtime, sha))
    changed_paths -= read_failed
    new_paths -= read_failed

    # Cache-aware embedding: positional reassembly per plan B.3.
    chunk_texts = [row[2] for row in pending]
    vecs: list[bytes | None] = [None] * len(chunk_texts)
    miss_indices: list[int] = []
    miss_texts: list[str] = []
    for i, t in enumerate(chunk_texts):
        cached = _cache_get(t)
        if cached is not None:
            vecs[i] = cached
        else:
            miss_indices.append(i)
            miss_texts.append(t)
    if miss_texts:
        if not _QUIET:
            print(f"Embedding {len(miss_texts)} chunks with {MODEL}...", file=sys.stderr)
        new_vecs = embed_all(miss_texts)
        for mi, nv in zip(miss_indices, new_vecs):
            vecs[mi] = nv
            _cache_put(chunk_texts[mi], nv)
    assert all(v is not None for v in vecs)

    conn.execute("BEGIN")
    try:
        for rel in sorted(changed_paths | orphans):
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM files WHERE path = ?", (rel,))

        next_id = next(conn.execute("SELECT COALESCE(MAX(id), 0) FROM chunks"))[0] + 1
        per_file_count: dict[str, int] = {}
        for i, (rel, heading, ctext, line_start, _, _) in enumerate(pending):
            conn.execute(
                "INSERT INTO chunks (id, file_path, section_heading, chunk_text, line_start) "
                "VALUES (?, ?, ?, ?, ?)",
                (next_id, rel, heading, ctext, line_start),
            )
            conn.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (next_id, vecs[i]),
            )
            per_file_count[rel] = per_file_count.get(rel, 0) + 1
            next_id += 1
        for rel in sorted(changed_paths | new_paths):
            _, mtime, sha = walked[rel]
            conn.execute(
                "INSERT INTO files (path, mtime, sha1, chunk_count, model) "
                "VALUES (?, ?, ?, ?, ?)",
                (rel, mtime, sha, per_file_count.get(rel, 0), MODEL),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Work happened: always print the final stats line, even under --quiet.
    print(
        f"Indexed {len(new_paths)} new + {len(changed_paths)} changed + "
        f"{len(orphans)} removed, skipped {len(unchanged_paths)} unchanged",
        file=sys.stderr,
    )


def excerpt(text: str, n_sentences: int = 3, hard_cap: int = 500) -> str:
    parts = SENT_SPLIT_RE.split(text.strip())
    out = " ".join(parts[:n_sentences]).strip()
    if len(out) > hard_cap:
        out = out[:hard_cap].rsplit(" ", 1)[0] + "..."
    return out


# ---- Query ----

def cmd_query(q: str, root: Path, db_path: Path, *, top_k: int,
              in_patterns: list[str] | None, as_json: bool) -> None:
    if not db_path.exists():
        print(f"ERROR: no index at {db_path}; run with --index first", file=sys.stderr)
        sys.exit(2)

    conn = open_db(db_path)

    # Same-dim stale-model warning (after open_db dim check, before embed).
    stale_row = next(conn.execute(
        "SELECT COUNT(*) FROM files WHERE model != ?", (MODEL,)
    ), (0,))
    stale = stale_row[0]
    if stale > 0 and not _QUIET:
        print(
            f"Warning: {stale} file(s) embedded with a different model; "
            f"run --index to refresh.",
            file=sys.stderr,
        )

    has_scope = bool(in_patterns)
    if has_scope:
        match = _compile_in_matcher(in_patterns)
        allowed_ids: list[int] = []
        for cid, fpath in conn.execute("SELECT id, file_path FROM chunks"):
            if match(fpath):
                allowed_ids.append(cid)
        if not allowed_ids:
            if not as_json:
                print("No results.")
            return
        conn.execute("DROP TABLE IF EXISTS temp.scope_ids")
        conn.execute("CREATE TEMP TABLE scope_ids(id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO scope_ids(id) VALUES (?)",
                         [(i,) for i in allowed_ids])

    query_bytes = embed_all([q])[0]
    candidate_k = max(20, top_k * 2)

    if has_scope:
        vec_sql = (
            "SELECT chunks.id, chunks.file_path, chunks.section_heading, "
            "chunks.line_start, chunks.chunk_text "
            "FROM vec_chunks JOIN chunks ON chunks.id = vec_chunks.rowid "
            "WHERE vec_chunks.rowid IN (SELECT id FROM scope_ids) "
            "AND vec_chunks.embedding MATCH ? AND k = ? "
            "ORDER BY vec_chunks.distance"
        )
    else:
        vec_sql = (
            "SELECT chunks.id, chunks.file_path, chunks.section_heading, "
            "chunks.line_start, chunks.chunk_text "
            "FROM vec_chunks JOIN chunks ON chunks.id = vec_chunks.rowid "
            "WHERE vec_chunks.embedding MATCH ? AND k = ? "
            "ORDER BY vec_chunks.distance"
        )
    vec_hits = list(conn.execute(vec_sql, (query_bytes, candidate_k)))

    fts_query = _fts_sanitize(q)
    fts_hits: list[tuple] = []
    if fts_query:
        if has_scope:
            fts_sql = (
                "SELECT chunks.id, chunks.file_path, chunks.section_heading, "
                "chunks.line_start, chunks.chunk_text "
                "FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid "
                "JOIN scope_ids ON scope_ids.id = chunks.id "
                "WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?"
            )
        else:
            fts_sql = (
                "SELECT chunks.id, chunks.file_path, chunks.section_heading, "
                "chunks.line_start, chunks.chunk_text "
                "FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?"
            )
        try:
            fts_hits = list(conn.execute(fts_sql, (fts_query, candidate_k)))
        except Exception as exc:
            if not _QUIET:
                print(f"FTS query failed ({exc}); falling back to vec-only", file=sys.stderr)
            fts_hits = []

    scored = _rrf_fuse(vec_hits, fts_hits, top_k)
    if not scored:
        if not as_json:
            print("No results.")
        return

    # Rebuild rank dicts for display annotations.
    vec_rank = {row[0]: i + 1 for i, row in enumerate(vec_hits)}
    fts_rank = {row[0]: i + 1 for i, row in enumerate(fts_hits)}

    id_to_row: dict[int, tuple] = {}
    for row in vec_hits:
        id_to_row[row[0]] = row
    for row in fts_hits:
        id_to_row.setdefault(row[0], row)

    if as_json:
        for cid, score in scored:
            _, path, heading, line, text = id_to_row[cid]
            obj = {
                "path": path,
                "line": int(line),
                "heading": heading or "",
                "score": float(score),
                "vec_rank": vec_rank.get(cid),
                "fts_rank": fts_rank.get(cid),
                "text": text,
            }
            print(json.dumps(obj, ensure_ascii=False))
        return

    for i, (cid, score) in enumerate(scored, 1):
        _, path, heading, line, text = id_to_row[cid]
        loc = f"{path}:{line}" if line else path
        head_str = f"  §{heading}" if heading else ""
        vr = vec_rank.get(cid)
        fr = fts_rank.get(cid)
        vr_s = f"vec#{vr}" if vr else "vec#-"
        fr_s = f"fts#{fr}" if fr else "fts#-"
        print(f"\n[{i}] {loc}{head_str}")
        print(f"    score={score:.4f} ({vr_s} {fr_s})")
        print(f"    {excerpt(text)}")
    print()


# ---- Stats ----

def cmd_stats(db_path: Path) -> None:
    if not db_path.exists():
        print(f"no index at {db_path}; run --index first", file=sys.stderr)
        sys.exit(2)
    # --stats is a debugging aid; show what's there even if the embedding
    # dimension stamped in the meta table no longer matches the current code.
    conn = open_db(db_path, allow_stale_dim=True)
    schema_v = next(conn.execute("PRAGMA user_version"))[0]
    total_chunks = next(conn.execute("SELECT COUNT(*) FROM chunks"))[0]
    total_files = next(conn.execute("SELECT COUNT(*) FROM files"))[0]
    models = list(conn.execute(
        "SELECT model, COUNT(*) FROM files GROUP BY model ORDER BY COUNT(*) DESC"
    ))
    mtime_row = next(conn.execute("SELECT MIN(mtime), MAX(mtime) FROM files"), (None, None))
    top_files = list(conn.execute(
        "SELECT chunk_count, path FROM files ORDER BY chunk_count DESC LIMIT 5"
    ))

    print(f"schema_version: {schema_v}")
    print(f"db_path: {db_path}")
    print(f"total_chunks: {total_chunks}")
    print(f"total_files: {total_files}")
    if len(models) <= 1:
        single = models[0][0] if models else "(none)"
        print(f"model: {single}")
    else:
        print("models:")
        for m, c in models:
            print(f"  {m}: {c} files")
    if mtime_row and mtime_row[0] is not None:
        oldest = _dt.datetime.fromtimestamp(mtime_row[0]).isoformat(timespec="seconds")
        newest = _dt.datetime.fromtimestamp(mtime_row[1]).isoformat(timespec="seconds")
        print(f"oldest_mtime: {oldest}")
        print(f"newest_mtime: {newest}")
    if top_files:
        print()
        print("top files by chunk count:")
        for count, path in top_files:
            print(f"  {count:>5}  {path}")


# ---- Main ----

def main() -> None:
    p = argparse.ArgumentParser(
        description="Semantic search over markdown/text files (hybrid BM25+cosine).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Hybrid retrieval: cosine over vec_chunks + BM25 over chunks_fts, fused via RRF. "
            "Incremental indexing keyed on sha1. v2 indexes auto-upgrade to v3 on first open. "
            "Cache: $XDG_CACHE_HOME/semantic-search/ (opt out: SEMANTIC_SEARCH_NO_CACHE=1). "
            "Parallel embedding: SEMANTIC_SEARCH_EMBED_WORKERS (default 4, clamped [1,16])."
        ),
    )
    p.add_argument("query", nargs="?", default=None, help="natural-language query")
    p.add_argument("--index", action="store_true", help="build/refresh the index incrementally")
    p.add_argument("--rebuild", action="store_true", help="drop and rebuild the index")
    p.add_argument("--top", type=int, default=TOP_K, help=f"results to return (default {TOP_K})")
    p.add_argument("--db", default=DB_FILE, help=f"sqlite path (default {DB_FILE})")
    p.add_argument("--json", action="store_true", help="emit JSONL (one object per hit)")
    p.add_argument("--git-root", action="store_true",
                   help="walk up from cwd to find a .git directory and use that as the index root")
    p.add_argument("--cwd", default=None, help="explicit absolute root path (overrides --git-root)")
    p.add_argument("--in", action="append", dest="in_patterns", default=None, metavar="GLOB",
                   help="restrict to paths matching this glob (POSIX, relative to root). Repeat for OR.")
    p.add_argument("--stats", action="store_true",
                   help="print index summary and exit (mutually exclusive with --index/--rebuild/query)")
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress output; errors and 'work-done' stats lines remain")
    args = p.parse_args()

    globals()["_QUIET"] = args.quiet

    if args.stats:
        if args.index or args.rebuild or args.query:
            p.error("--stats cannot be combined with --index/--rebuild/query")

    root, git_root_detected = _resolve_root(args)
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = root / db_path

    if args.stats:
        cmd_stats(db_path)
        return

    if args.index or args.rebuild:
        cmd_index(root, db_path, rebuild=args.rebuild,
                  in_patterns=args.in_patterns)
        if git_root_detected:
            _gitignore_check(root)
        return

    if not args.query:
        p.error("provide a query, or use --index / --rebuild / --stats")
    cmd_query(args.query, root, db_path, top_k=args.top,
              in_patterns=args.in_patterns, as_json=args.json)


if __name__ == "__main__":
    main()
