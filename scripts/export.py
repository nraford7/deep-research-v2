#!/usr/bin/env python3
"""
export.py — emit BibTeX + claims JSONL from a finished Research Bible.

Inputs:
  - A directory containing section markdown files with [Author, Year] cites
  - A master bibliography file

Outputs:
  - bibliography.bib  : BibTeX entries (one per bib row, key = AuthorYear)
  - claims.jsonl      : one row per inline citation with file + surrounding sentence

Usage:
  python3 export.py --sections research/topic/sections/ \
      --bibliography research/topic/sections/bibliography.md \
      --output-dir research/topic/export/
"""

import argparse
import json
import re
from pathlib import Path


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\)\]]+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}[a-z]?\b")
_AUTHOR_GROUP = r"(?:[A-Za-z][A-Za-z\.\-' ]{0,80}?)"
INLINE_CITE_RE = re.compile(
    rf"[\[\(]\s*({_AUTHOR_GROUP}(?:\s+(?:et\s+al\.?|&\s+{_AUTHOR_GROUP}|and\s+{_AUTHOR_GROUP}))?),?\s*(\d{{4}}[a-z]?)\s*[\]\)]"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
BIB_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)")


def parse_bib_entries(text: str):
    """Parse bibliography list entries. Skip prose lines (frontmatter,
    section dividers, dedup notes). A real entry is a bullet/numbered list
    item containing a 4-digit year."""
    entries = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if not BIB_BULLET_RE.match(line):
            continue
        body = BIB_BULLET_RE.sub("", line, count=1).strip()
        if len(body) < 30:
            continue
        if not YEAR_RE.search(body):
            continue
        entries.append(body)
    return entries


def bibtex_escape(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}").replace("%", "\\%").replace("$", "\\$").replace("&", "\\&")


def extract_authors_year_title(entry: str):
    year_m = YEAR_RE.search(entry)
    year = year_m.group(0) if year_m else "n.d."
    pre = entry[:year_m.start()] if year_m else entry
    pre = pre.rstrip(" (.,")
    author_first = re.match(r"([A-Z][A-Za-z\-']+)", pre)
    author_key = author_first.group(1) if author_first else "Anon"
    post = entry[year_m.end():].strip(" .,)") if year_m else entry
    title_m = re.match(r"[^.\"]+", post)
    title = title_m.group(0).strip(" \"'.,") if title_m else post[:200]
    return author_key, year, pre.strip(), title


def to_bibtex_entry(entry: str, author_year_counter: dict) -> str:
    author_key, year, authors, title = extract_authors_year_title(entry)
    doi_m = DOI_RE.search(entry)
    url_m = URL_RE.search(entry)
    base = f"{author_key}{year}"
    seen = author_year_counter.get(base, 0)
    author_year_counter[base] = seen + 1
    if seen == 0:
        key = base
    elif seen < 26:
        key = f"{base}{chr(ord('a') + seen)}"
    else:
        key = f"{base}_{seen}"
    fields = [f"  author    = {{{bibtex_escape(authors)}}}",
              f"  year      = {{{year}}}",
              f"  title     = {{{bibtex_escape(title)}}}"]
    if doi_m:
        fields.append(f"  doi       = {{{doi_m.group(0)}}}")
    if url_m:
        fields.append(f"  url       = {{{url_m.group(0)}}}")
    fields.append(f"  note      = {{{bibtex_escape(entry[:400])}}}")
    return "@misc{" + key + ",\n" + ",\n".join(fields) + "\n}"


def extract_claims(sections_dir: Path):
    for f in sorted(sections_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sentences = SENTENCE_SPLIT_RE.split(text)
        for sent in sentences:
            cites = list(INLINE_CITE_RE.finditer(sent))
            if not cites:
                continue
            yield {
                "file": str(f),
                "sentence": sent.strip()[:600],
                "citations": [{"author": m.group(1), "year": m.group(2)} for m in cites],
            }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections", required=True, help="Directory of section markdown files")
    ap.add_argument("--bibliography", required=True, help="Master bibliography file")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bib_entries = parse_bib_entries(Path(args.bibliography).read_text(encoding="utf-8", errors="replace"))
    counter = {}
    bibtex_lines = [to_bibtex_entry(e, counter) for e in bib_entries]
    bib_path = out_dir / "bibliography.bib"
    bib_path.write_text("\n\n".join(bibtex_lines), encoding="utf-8")
    print(f"BibTeX: {bib_path} ({len(bib_entries)} entries)")

    claims_path = out_dir / "claims.jsonl"
    n = 0
    with claims_path.open("w", encoding="utf-8") as f:
        for row in extract_claims(Path(args.sections)):
            f.write(json.dumps(row) + "\n")
            n += 1
    print(f"Claims: {claims_path} ({n} rows)")


if __name__ == "__main__":
    main()
