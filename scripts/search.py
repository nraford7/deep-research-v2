#!/usr/bin/env python3
"""deep-research — semantic search over your research (native wrapper).

Bundles the vendored semantic-search engine and bakes in deep-research
conventions: ONE project-wide index over each topic's Bible
(README.md + sections/*.md) at research/.semantic-index.db.

GRACEFUL DEGRADATION: every failure mode exits 0 with a one-line stderr notice.
Semantic search is strictly additive and must never break a research run.

Usage:
    python3 scripts/search.py index [--root PATH]
    python3 scripts/search.py "<query>" [--topic SLUG] [--top N] [--json]
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIBLE_GLOBS = ["*/README.md", "*/sections/*.md"]
DB_NAME = ".semantic-index.db"
SEARCH_REQ = "requirements-search.txt"


def _notice(msg: str) -> None:
    print(f"[semantic-search skipped] {msg}", file=sys.stderr)


def _ensure_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


class _QuietParser(argparse.ArgumentParser):
    """argparse that does NOT print its own multi-line usage/error text.

    On a bad invocation, stock argparse writes `usage: ...` + `error: ...` to
    stderr before exiting. The wrapper's contract is a single one-line notice, so
    we suppress argparse's output and let run() emit the notice. error() still
    exits non-zero (like argparse) so run()'s SystemExit handler fires; exit()
    (used by --help after print_help) is preserved with its status."""

    def error(self, message):  # noqa: D401
        raise SystemExit(2)

    def exit(self, status=0, message=None):
        raise SystemExit(status)


def _load_engine():
    """Import the vendored engine and probe its optional runtime dep.

    The engine imports `sqlite_vec` lazily (deep inside cmd_index/cmd_query), so a
    plain module import succeeds even when the optional search deps are NOT
    installed — the failure would otherwise surface later as a cryptic engine
    SystemExit. Probing `sqlite_vec` up front makes a missing optional dep raise
    ImportError here, so the caller gives the actionable
    'pip install -r requirements-search.txt' guidance instead."""
    _ensure_path()
    from vendor.semantic_search import search as engine
    import sqlite_vec  # noqa: F401  — optional dep; engine imports it lazily, probe early
    return engine


def _resolve_key() -> bool:
    """Populate os.environ['OPENAI_API_KEY'] using deep-research's own loader
    (env -> ~/.env -> ./.env, first-set-wins). Returns True if a key is available."""
    if os.environ.get("OPENAI_API_KEY"):
        return True
    try:
        _ensure_path()
        import config
        merged = config.load_env_files()
        key = merged.get("OPENAI_API_KEY")
        if key:
            os.environ["OPENAI_API_KEY"] = key
    except Exception:
        pass
    return bool(os.environ.get("OPENAI_API_KEY"))


def _research_root(arg_root) -> Path:
    return Path(arg_root).resolve() if arg_root else (Path.cwd() / "research").resolve()


def _do_index(root: Path) -> int:
    try:
        engine = _load_engine()
    except ImportError as e:
        _notice(f"search dependencies not installed ({e}). Run: pip install -r {SEARCH_REQ}")
        return 0
    if not _resolve_key():
        _notice("OPENAI_API_KEY not found (env, ./.env, or ~/.env).")
        return 0
    db = root / DB_NAME
    try:
        engine.cmd_index(root, db, rebuild=False, in_patterns=BIBLE_GLOBS)
    except (Exception, SystemExit) as e:  # engine may sys.exit internally
        _notice(f"indexing failed ({e!r}); run completed without it.")
        return 0
    return 0


def _do_query(query: str, topic, top: int, as_json: bool) -> int:
    try:
        engine = _load_engine()
    except ImportError as e:
        _notice(f"search dependencies not installed ({e}). Run: pip install -r {SEARCH_REQ}")
        return 0
    if not _resolve_key():
        _notice("OPENAI_API_KEY not found (env, ./.env, or ~/.env).")
        return 0
    root = _research_root(None)
    db = root / DB_NAME
    if not db.exists():
        _notice(f"no index yet at {db}. Run: python3 scripts/search.py index")
        return 0
    in_patterns = [f"{topic}/**"] if topic else None
    try:
        engine.cmd_query(query, root, db, top_k=top, in_patterns=in_patterns, as_json=as_json)
    except (Exception, SystemExit) as e:  # engine sys.exit(2) on missing db, etc.
        _notice(f"search failed ({e!r}).")
        return 0
    return 0


def run(argv) -> int:
    """Manual dispatch — NOT argparse subparsers. Subparsers would treat the
    first token as a subcommand and reject a normal query like
    `search.py "why does X happen?"` with SystemExit(2). Rule: first token
    'index' => index subcommand; ANY other first token => positional query.

    argparse calls sys.exit() on bad args (and on --help). We catch that
    SystemExit so the wrapper honors the absolute 'every failure exits 0'
    contract — a malformed invocation prints a notice and exits 0, never nonzero.
    """
    argv = list(argv)
    try:
        if argv and argv[0] == "index":
            ip = _QuietParser(prog="scripts/search.py index")
            ip.add_argument("--root", default=None, help="research root (default ./research)")
            a = ip.parse_args(argv[1:])
            return _do_index(_research_root(a.root))

        qp = _QuietParser(
            prog="scripts/search.py",
            description="Semantic search over your deep-research output.",
        )
        qp.add_argument("query", nargs="?", default=None, help="natural-language query")
        qp.add_argument("--topic", default=None, help="scope search to one topic slug")
        qp.add_argument("--top", type=int, default=5, help="results to return (default 5)")
        qp.add_argument("--json", action="store_true", help="emit JSONL")
        a = qp.parse_args(argv)
    except SystemExit as e:
        # --help exits 0 (help text already printed); bad args exit nonzero.
        if e.code not in (0, None):
            _notice("invalid arguments; run: python3 scripts/search.py --help")
        return 0
    if not a.query:
        _notice("provide a query, or run: python3 scripts/search.py index")
        return 0
    return _do_query(a.query, a.topic, a.top, a.json)


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
