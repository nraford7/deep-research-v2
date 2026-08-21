#!/usr/bin/env python3
"""
lit_search.py — query OpenAlex and Semantic Scholar for a topic.

Used for two purposes:
  1. SCOPING — surface highly-cited canonical works before Round 1, so model
     prompts can be primed with the literature spine.
  2. MISSING-LIT CHECK — compare a finished bibliography against the top-N
     highly-cited works in the topic area; flag major works absent.

OpenAlex: free, no key.
Semantic Scholar: free for low volume; set SEMANTIC_SCHOLAR_KEY for higher rate.

Usage:
  python3 lit_search.py --topic "central bank digital currencies" --limit 50 \
      --output canonical-works.md

  python3 lit_search.py --topic "..." --compare-bib sections/bibliography.md \
      --output missing-lit.md
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)


def _make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "deep-research/1.0"})
    retry = Retry(total=4, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _normalize_doi(value):
    if not value:
        return ""
    v = str(value).strip().lower().rstrip("/.,)")
    v = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v


CONTACT = os.environ.get("CONTACT_EMAIL", "anonymous@example.com")
SS_KEY = os.environ.get("SEMANTIC_SCHOLAR_KEY")
OPENALEX = "https://api.openalex.org"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1"
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def query_openalex(topic: str, limit: int = 50):
    s = _make_session()
    s.headers.update({"User-Agent": f"deep-research/1.0 (mailto:{CONTACT})"})
    results = []
    per_page = min(50, limit)
    pages = (limit + per_page - 1) // per_page
    for page in range(1, pages + 1):
        r = s.get(f"{OPENALEX}/works", params={
            "search": topic,
            "per-page": per_page,
            "page": page,
            "sort": "cited_by_count:desc",
            "mailto": CONTACT,
        }, timeout=30)
        if not r.ok:
            break
        for w in r.json().get("results", []):
            results.append({
                "title": (w.get("title") or "").strip(),
                "year": w.get("publication_year"),
                "cited_by": w.get("cited_by_count"),
                "doi": w.get("doi"),
                "id": w.get("id"),
                "authors": [a.get("author", {}).get("display_name") for a in (w.get("authorships") or [])[:5]],
                "type": w.get("type"),
                "venue": (w.get("host_venue") or {}).get("display_name") or ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "source": "openalex",
            })
        if len(results) >= limit:
            break
    return results[:limit]


def query_semantic_scholar(topic: str, limit: int = 50):
    s = _make_session()
    if SS_KEY:
        s.headers["x-api-key"] = SS_KEY
    try:
        r = s.get(
            f"{SEMANTIC_SCHOLAR}/paper/search",
            params={
                "query": topic,
                "limit": min(100, limit),
                "fields": "title,year,citationCount,authors,venue,externalIds",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"  Semantic Scholar error: {e}", file=sys.stderr)
        return []
    if not r.ok:
        print(f"  Semantic Scholar HTTP {r.status_code}", file=sys.stderr)
        return []
    out = []
    for p in r.json().get("data", []) or []:
        out.append({
            "title": (p.get("title") or "").strip(),
            "year": p.get("year"),
            "cited_by": p.get("citationCount"),
            "doi": (p.get("externalIds") or {}).get("DOI"),
            "authors": [a.get("name") for a in (p.get("authors") or [])[:5]],
            "venue": p.get("venue"),
            "source": "semantic_scholar",
        })
    return out[:limit]


def merge_results(*lists):
    seen_doi = set()
    seen_title = set()
    merged = []
    for lst in lists:
        for w in lst:
            doi = _normalize_doi(w.get("doi"))
            title_norm = re.sub(r"[^a-z0-9]+", " ", (w.get("title") or "").lower()).strip()
            if doi and doi in seen_doi:
                continue
            if title_norm and title_norm in seen_title:
                continue
            if doi:
                seen_doi.add(doi)
            if title_norm:
                seen_title.add(title_norm)
            merged.append(w)
    return sorted(merged, key=lambda w: -(w.get("cited_by") or 0))


def compare_against_bib(canonical, bib_text):
    bib_dois = {_normalize_doi(m.group(0)) for m in DOI_RE.finditer(bib_text)}
    # Token-overlap check should compare against the BIB ENTRIES, not the full file —
    # otherwise canonical-work title tokens that happen to appear in section prose
    # produce false "present" hits. Concatenate just the bibliography entry lines.
    BIB_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)")
    bib_entries_text = []
    for line in bib_text.splitlines():
        if BIB_BULLET_RE.match(line):
            bib_entries_text.append(line.lower())
    bib_titles_norm = re.sub(r"[^a-z0-9 ]+", " ", " ".join(bib_entries_text))
    missing, present = [], []
    for w in canonical:
        doi = _normalize_doi(w.get("doi"))
        title = (w.get("title") or "").lower()
        title_tokens = [t for t in re.split(r"\W+", title) if len(t) > 4]
        if not title_tokens:
            continue
        hits = sum(1 for t in title_tokens[:8] if t in bib_titles_norm)
        match_ratio = hits / min(8, len(title_tokens))
        if (doi and doi in bib_dois) or match_ratio >= 0.7:
            present.append(w)
        else:
            missing.append(w)
    return present, missing


def render_work(w):
    authors = ", ".join(a for a in w.get("authors", []) if a) or "—"
    return (
        f"- **{w.get('title') or 'untitled'}** ({w.get('year') or '?'}) — "
        f"{authors}. *{w.get('venue') or '—'}*. "
        f"Cited **{w.get('cited_by') or 0}×** [{w.get('source')}]"
        + (f" doi:{w.get('doi')}" if w.get('doi') else "")
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--limit", type=int, default=50, help="Top-N highly-cited works")
    ap.add_argument("--output", required=True)
    ap.add_argument("--compare-bib", help="If given, flag missing canonical works vs this bibliography")
    ap.add_argument("--source", choices=["openalex", "semantic_scholar", "both"], default="both")
    args = ap.parse_args()

    print(f"Querying for: {args.topic!r}", flush=True)
    oa, ss = [], []
    if args.source in ("openalex", "both"):
        oa = query_openalex(args.topic, args.limit)
        print(f"  OpenAlex: {len(oa)} works", flush=True)
    if args.source in ("semantic_scholar", "both"):
        ss = query_semantic_scholar(args.topic, args.limit)
        print(f"  Semantic Scholar: {len(ss)} works", flush=True)
    merged = merge_results(oa, ss)[: args.limit]
    print(f"  Merged: {len(merged)} unique", flush=True)

    out = [f"# Canonical works — {args.topic}", "",
           f"Top {len(merged)} works by citation count, OpenAlex + Semantic Scholar.", ""]

    if args.compare_bib:
        bib_text = Path(args.compare_bib).read_text(encoding="utf-8", errors="replace")
        present, missing = compare_against_bib(merged, bib_text)
        out += [
            f"## Comparison vs `{args.compare_bib}`",
            "",
            f"- Canonical works present in bibliography: **{len(present)}**",
            f"- ⚠ Canonical works MISSING from bibliography: **{len(missing)}**",
            "",
            "### Missing (review and consider adding)",
            "",
        ]
        for w in missing:
            out.append(render_work(w))
        out += ["", "### Present (no action)", ""]
        for w in present:
            out.append(render_work(w))
    else:
        for w in merged:
            out.append(render_work(w))

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("\n".join(out), encoding="utf-8")
    Path(args.output).with_suffix(".json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()
