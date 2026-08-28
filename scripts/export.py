#!/usr/bin/env python3
"""
export.py — emit BibTeX, claims JSONL, and Research Bible HTML.

Inputs:
  - A directory containing section markdown files with [Author, Year] cites
  - A master bibliography file

Outputs:
  - bibliography.bib  : BibTeX entries (one per bib row, key = AuthorYear)
  - claims.jsonl      : one row per inline citation with file + surrounding sentence
  - Research Bible HTML: jimemo when compatible, otherwise a built-in page

Usage:
  python3 export.py --sections research/topic/sections/ \
      --bibliography research/topic/sections/bibliography.md \
      --output-dir research/topic/export/
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Support both documented direct execution (`python3 scripts/export.py`) and
# package imports (`python3 -m scripts.export`, tests).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.research_bible_html import (
    assembled_html_name,
    build_document,
    export_html,
    resolve_bible_path,
)


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\)\]]+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}[a-z]?\b")
# INLINE_CITE_RE is kept BYTE-FOR-BYTE identical to verify_citations.py's copy
# (two named alternatives — `domain` for bare lowercase web domains, `author`
# for academic author heads — plus a shared year group that accepts n.d.). If
# these diverge, n.d. / digit-domain web cites never reach claims.jsonl.
_AUTHOR_GROUP = r"(?:[A-Za-z][A-Za-z\.\-' ]{0,80}?)"
_AUTHOR_ALT = rf"{_AUTHOR_GROUP}(?:\s+(?:et\s+al\.?|&\s+{_AUTHOR_GROUP}|and\s+{_AUTHOR_GROUP}))?"
_DOMAIN_ALT = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"
_YEAR_ALT = r"\d{4}[a-z]?|n\.d\."
INLINE_CITE_RE = re.compile(
    rf"[\[\(]\s*(?:(?P<domain>{_DOMAIN_ALT})|(?P<author>{_AUTHOR_ALT})),?\s*(?P<year>{_YEAR_ALT})\s*[\]\)]"
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
        # A real academic entry has a 4-digit year. A WEB entry may be undated
        # (n.d.) — accept it when it carries a URL or an explicit n.d. marker so
        # year-less web sources are not silently dropped.
        if not YEAR_RE.search(body):
            if not (URL_RE.search(body) or "n.d." in body.lower()):
                continue
        entries.append(body)
    return entries


def bibtex_escape(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}").replace("%", "\\%").replace("$", "\\$").replace("&", "\\&")


_LEADING_DOMAIN_RE = re.compile(r"^\s*([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b")
_ND_RE = re.compile(r"\bn\.d\.", re.IGNORECASE)


def extract_authors_year_title(entry: str):
    """Split a bibliography line into (author_key, year, authors, title).

    Academic lines have a 4-digit year. Undated WEB lines (no year) are handled
    separately: the leading token is a bare domain, the year is `n.d.`, and the
    title is the text between the `n.d.` marker (or the domain) and the URL — so
    the author/title fields are clean instead of a truncated dot-split."""
    year_m = YEAR_RE.search(entry)
    if year_m:
        year = year_m.group(0)
        pre = entry[:year_m.start()].rstrip(" (.,")
        author_first = re.match(r"([A-Z][A-Za-z\-']+)", pre)
        if author_first:
            author_key = author_first.group(1)
        else:
            # A dated WEB entry leads with a bare lowercase domain — key off it.
            dom_m = _LEADING_DOMAIN_RE.match(entry)
            author_key = re.sub(r"[^A-Za-z0-9]", "", dom_m.group(1)) if dom_m else "Anon"
        post = entry[year_m.end():].strip(" .,)")
        title_m = re.match(r"[^.\"]+", post)
        title = title_m.group(0).strip(" \"'.,") if title_m else post[:200]
        return author_key, year, pre.strip(), title

    # Undated web entry: pull the leading domain as author + BibTeX key stem.
    dom_m = _LEADING_DOMAIN_RE.match(entry)
    if dom_m:
        domain = dom_m.group(1)
        # BibTeX keys must be alphanumeric-ish; strip dots/hyphens from the stem.
        author_key = re.sub(r"[^A-Za-z0-9]", "", domain) or "Anon"
        authors = domain
    else:
        author_key = "Anon"
        authors = ""
    # Title = text after the n.d. marker (or the domain), up to the first URL.
    tail = entry
    nd_m = _ND_RE.search(entry)
    if nd_m:
        tail = entry[nd_m.end():]
    elif dom_m:
        tail = entry[dom_m.end():]
    tail = URL_RE.sub("", tail)
    title = tail.strip(" ().,\"'").strip()
    title = re.split(r"(?<=[.!?])\s", title, maxsplit=1)[0].strip(" .\"'")
    if not title:
        title = authors or "Untitled"
    return author_key, "n.d.", authors, title[:300]


def to_bibtex_entry(entry: str, author_year_counter: dict) -> str:
    author_key, year, authors, title = extract_authors_year_title(entry)
    doi_m = DOI_RE.search(entry)
    url_m = URL_RE.search(entry)
    # BibTeX cite keys must not contain punctuation like the '.' in "n.d." —
    # sanitize the key stem (the `year` FIELD below keeps its literal value).
    year_key = re.sub(r"[^A-Za-z0-9]", "", year) or "nd"
    base = f"{author_key}{year_key}"
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


def _url_host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _cite_dict(m, sentence: str):
    """Turn one INLINE_CITE_RE match into a claim citation dict, or None if the
    match is not a valid citation (e.g. an academic-head `n.d.`, which is
    web-only — mirrors extract_inline_cites in verify_citations.py).

    A `domain`-matched head is a WEB citation and carries `kind="web"` plus a
    `url` chosen by HOST match (exact host or a subdomain of the cited domain),
    NOT a naive substring — `foo.com` must not match `notfoo.com`. If no URL in
    the sentence has a matching host, the bare domain is used as the https://
    fallback (we do NOT borrow an unrelated URL). An `author`-matched head is an
    ACADEMIC citation; an academic head with `n.d.` is rejected (web-only)."""
    domain = m.group("domain")
    author = m.group("author")
    year = m.group("year")
    if domain:
        domain = domain.lower()
        url = None
        for raw in URL_RE.findall(sentence):
            u = raw.rstrip(".,;)")
            host = _url_host(u)
            if host == domain or host.endswith("." + domain):
                url = u
                break
        if url is None:
            url = f"https://{domain}"
        return {"author": domain, "year": year, "kind": "web", "url": url}
    if year == "n.d.":
        # `n.d.` is web-only; an author-head n.d. is not a valid citation.
        return None
    return {"author": (author or "").strip(), "year": year, "kind": "academic"}


def extract_claims(sections_dir: Path):
    for f in sorted(sections_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sentences = SENTENCE_SPLIT_RE.split(text)
        for sent in sentences:
            cites = [c for c in (_cite_dict(m, sent) for m in INLINE_CITE_RE.finditer(sent))
                     if c is not None]
            if not cites:
                continue
            yield {
                "file": str(f),
                "sentence": sent.strip()[:600],
                "citations": cites,
            }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections", required=True, help="Directory of section markdown files")
    ap.add_argument("--bibliography", required=True, help="Master bibliography file")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--bible", help="Finished Markdown Bible to use for page metadata and basename")
    ap.add_argument("--no-html", action="store_true", help="Skip the automatic HTML companion")
    args = ap.parse_args(argv)

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

    if not args.no_html:
        try:
            bible_path = resolve_bible_path(out_dir, Path(args.bible) if args.bible else None)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        document = build_document(Path(args.sections), Path(args.bibliography), bible_path)
        html_name = bible_path.with_suffix(".html").name if bible_path else assembled_html_name(Path(args.sections))
        result = export_html(document, out_dir / html_name)
        if result.renderer == "jimemo":
            print(f"HTML: {result.path} (jimemo)")
        elif result.fallback_reason == "jimemo unavailable":
            print(f"HTML: {result.path} (built-in; jimemo unavailable)")
        else:
            print(f"HTML: {result.path} (built-in; {result.fallback_reason})")
    return 0


if __name__ == "__main__":
    main()
