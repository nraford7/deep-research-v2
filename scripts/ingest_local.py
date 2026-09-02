#!/usr/bin/env python3
"""ingest_local.py — ingest user-provided (client KB) documents into the run's
evidence store, so every downstream checker (number sweep, adversary,
[kb:slug] citation resolution) can see them.

Writes extracted text next to Exa spills (layout-resolved sources dir) and
appends/updates rows in <round1>/slice_local.jsonl. Rows carry url+tier so the
evidence gate re-validates them like any slice row; they additionally carry
kb_slug / origin / sha256 for KB citation resolution and provenance.

EMPTY EXTRACTION IS A FAILURE (exit 3): a scanned/encrypted PDF must never
become a resolvable KB handle or count toward the evidence gate.

Usage:
  python3 scripts/ingest_local.py --run-dir DIR [--year YYYY] [--title T]
      [--slug S] FILE [FILE...]
(--title/--slug apply only when ingesting a single file.)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.helper_runtime import (
    is_broker_managed, require_managed_mutation, resolve_helper_layout,
    standalone_mutation_guard)
from scripts.run_layout import LayoutKind
from scripts.fetch_fulltext import _html_to_text, _pdf_to_text, _looks_like_pdf
from scripts.slice_search import _source_filename

EXIT_USAGE = 2
EXIT_EXTRACTION = 3

# Format allowlist: anything else is a usage error — decoding arbitrary
# binaries as UTF-8 could mint a resolvable KB handle out of noise.
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # must match KB_CITE_RE's slug


def _slugify(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "doc"


def _extract_text(path: Path, data: bytes) -> str | None:
    """Extract text, or None when the format is not ingestable (allowlist)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf" or _looks_like_pdf(path.name, "", data):
        return _pdf_to_text(data)
    if suffix in (".html", ".htm"):
        return _html_to_text(data)
    if suffix in _TEXT_SUFFIXES:
        # Binary content wearing a text suffix must NOT mint a KB handle:
        # NUL bytes, or a decoded stream that is >10% U+FFFD replacement
        # characters, is an extraction FAILURE (None → exit 3, no row).
        if b"\x00" in data:
            return None
        text = data.decode("utf-8", errors="replace")
        if text and text.count("�") / len(text) > 0.10:
            return None
        return text
    return None


def _load_entries(jsonl: Path) -> list:
    """Ordered file entries: parsed dicts for JSON rows, raw ``str`` lines
    (kept VERBATIM) for anything unparseable/foreign — never dropped."""
    if not jsonl.exists():
        return []
    entries = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append(line)
    return entries


def _write_entries(jsonl: Path, entries: list) -> None:
    """Atomic rewrite: tmp file in the same dir + os.replace, so a crash can
    never leave a half-written slice_local.jsonl. Foreign ``str`` entries are
    written back verbatim."""
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n"
        for e in entries)
    tmp = jsonl.with_name(jsonl.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, jsonl)


def ingest_one(path: Path, layout, entries: list, *, title=None, slug=None,
               year=None) -> dict | None:
    """Extract one document and merge its row into ``entries`` in place.
    Returns the row, or None when extraction yields no text.
    (Format allowlist is enforced by the caller BEFORE this runs.)"""
    data = path.read_bytes()
    text = (_extract_text(path, data) or "").strip()
    if not text:
        return None
    url = path.resolve().as_uri()
    rows = [e for e in entries if isinstance(e, dict)]
    # Idempotence is by kb_slug (spec): an EXPLICIT --slug always replaces the
    # row carrying that slug — a replacement document under the same logical
    # handle updates the entry. An AUTO slug replaces only its own url; a
    # different file whose stem slugs identically gets a stable sha1 suffix.
    existing_by_url = {r.get("url"): r for r in rows}
    existing_by_slug = {r.get("kb_slug"): r for r in rows}
    if slug is not None:
        kb_slug = slug
        replace = existing_by_slug.get(kb_slug)
        # One document = one row: an explicit slug also retires any OTHER row
        # this same file produced earlier (e.g. under an auto slug).
        for stale in [r for r in rows
                      if r.get("url") == url and r is not replace]:
            entries.remove(stale)
    elif url in existing_by_url:
        replace = existing_by_url[url]
        kb_slug = replace["kb_slug"]
    else:
        kb_slug = _slugify(path.stem)
        replace = None
        if kb_slug in existing_by_slug:
            kb_slug = f"{kb_slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    row = {
        "title": title or path.stem,
        "url": url,
        "text_chars": len(text),
        "slice": "local",
        "tier": "user-provided",
        "origin": "user-provided",
        "kb_slug": kb_slug,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if year is not None:
        row["year"] = year
    # text_path mirrors slice_search._spill_fulltext EXACTLY: V2 rows carry
    # "Sources/Extracted/<f>" (run-root-relative); legacy rows carry
    # "sources/<f>" (round1-relative). background.py / run_extension.py
    # resolve them with those bases.
    spill_name = _source_filename({"slice": "local", "url": url})
    if layout.kind is LayoutKind.V2:
        sources_dir = layout.extracted_sources
        row["text_path"] = f"Sources/Extracted/{spill_name}"
    else:
        sources_dir = layout.round1 / "sources"
        row["text_path"] = f"sources/{spill_name}"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / spill_name).write_text(text, encoding="utf-8")
    if replace is not None:
        entries[entries.index(replace)] = row
    else:
        entries.append(row)
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)

    files = [Path(f) for f in args.files]
    if (args.title or args.slug) and len(files) != 1:
        print("--title/--slug require exactly one FILE", file=sys.stderr)
        return EXIT_USAGE
    if args.slug and not _SLUG_RE.match(args.slug):
        print(f"invalid --slug {args.slug!r}: must match [a-z0-9][a-z0-9-]* "
              f"(the [kb:slug] citation grammar)", file=sys.stderr)
        return EXIT_USAGE
    if args.year is not None and not 1000 <= args.year <= 9999:
        print(f"invalid --year {args.year}: must be a 4-digit year "
              f"(1000–9999 — KB_CITE_RE only matches 4 digits)",
              file=sys.stderr)
        return EXIT_USAGE
    for f in files:
        if not f.is_file():
            print(f"not a file: {f}", file=sys.stderr)
            return EXIT_USAGE
        suffix = f.suffix.lower()
        if suffix not in _TEXT_SUFFIXES | {".pdf", ".html", ".htm"}:
            print(f"unsupported format {suffix!r}: {f} — ingestable formats "
                  f"are pdf/html/htm/txt/md/markdown/text", file=sys.stderr)
            return EXIT_USAGE

    layout = resolve_helper_layout(Path(args.run_dir))
    require_managed_mutation(layout, "local KB ingestion")
    jsonl = layout.round1 / "slice_local.jsonl"
    # Legacy CLI path additionally takes the run lease (immutable-run guard);
    # broker-managed V2 invocations already hold the broker's lease, and
    # standalone_mutation_guard would (correctly) refuse a V2 target.
    guard = (nullcontext() if is_broker_managed()
             else standalone_mutation_guard(jsonl, operation="local KB ingestion"))
    with guard:
        entries = _load_entries(jsonl)
        failures = 0
        for f in files:
            row = ingest_one(f, layout, entries, title=args.title,
                             slug=args.slug, year=args.year)
            if row is None:
                failures += 1
                print(f"  ✗ {f}: extraction produced no text (scanned/encrypted "
                      f"PDF?) — NOT ingested", file=sys.stderr)
            else:
                print(f"  ✓ [kb:{row['kb_slug']}] {f.name} → {row['text_path']} "
                      f"({row['text_chars']} chars)")
        _write_entries(jsonl, entries)
    return EXIT_EXTRACTION if failures else 0


if __name__ == "__main__":
    sys.exit(main())
