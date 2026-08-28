#!/usr/bin/env python3
"""
lit_search.py — query OpenAlex and Semantic Scholar for a topic.

Used for two purposes:
  1. SCOPING — surface highly-cited canonical works before Round 1, so model
     prompts can be primed with the literature spine.
  2. MISSING-LIT CHECK — compare a finished bibliography against the top-N
     highly-cited works in the topic area; flag major works absent.

OpenAlex: metered — every request bills a prepaid credit pool; set OPENALEX_KEY
  (unauthenticated requests now 429). See https://openalex.org/pricing.
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
import time
from pathlib import Path
from urllib.parse import quote

from scripts.helper_runtime import standalone_mutation_guard

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)


class CappedRetry(Retry):
    """Retry that honors server Retry-After headers but caps the sleep.

    urllib3 sleeps for the full Retry-After value with NO ceiling; a
    rate-limiting server (Crossref/OpenAlex 429s especially) can park the
    process for an hour inside the retry machinery, outside every request
    timeout. Cap it so a stall becomes a bounded pause. (Root cause of a
    42-minute silent hang, 2026-08-25.)"""

    RETRY_AFTER_CAP = 30.0  # seconds

    def get_retry_after(self, response):
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return retry_after
        return min(retry_after, self.RETRY_AFTER_CAP)



def _make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "deeper-research/1.0"})
    retry = CappedRetry(total=4, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class OpenAlexError(Exception):
    """Raised when an OpenAlex HTTP request returns a non-ok status (4xx/5xx).

    A valid query that returns zero results is r.ok True with an empty list and
    does NOT raise; only a transport/server error (not r.ok) raises. Callers that
    must fail closed on a real outage (citation_chase's oa_failures counter) can
    catch this to distinguish a genuine empty result from an unreachable API."""
    pass


class SemanticScholarError(Exception):
    """Raised when every request in a Semantic Scholar graph operation fails.

    A successful request with zero matches returns an empty list. Citation-chase
    uses this distinction to record an actual provider failure separately from a
    clean graph query that found no work.
    """
    pass


def _bare_openalex_id(x):
    """OpenAlex ids come back as full URLs (https://openalex.org/W123). Filters
    and dedupe keys need the bare tail form (W123)."""
    return str(x).rsplit("/", 1)[-1]


def _is_doi_input(x):
    """True when an id string is a DOI (or a doi.org URL), NOT an OpenAlex W-id.
    DOI inputs must never be run through _bare_openalex_id (which would mangle a
    DOI's slashes into a bare tail and 400 the whole batch)."""
    s = str(x or "").strip().lower()
    if not s:
        return False
    if "doi.org" in s:
        return True
    return bool(re.search(r"10\.\d{4,9}/", s))


def _normalize_doi(value):
    if not value:
        return ""
    v = str(value).strip().lower().rstrip("/.,)")
    v = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v


CONTACT = os.environ.get("CONTACT_EMAIL", "anonymous@example.com")
SS_KEY = os.environ.get("SEMANTIC_SCHOLAR_KEY")
OA_KEY = os.environ.get("OPENALEX_KEY")  # OpenAlex premium key; metered credit pool
OPENALEX = "https://api.openalex.org"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1"
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _oa_params(params: dict) -> dict:
    """Attach the OpenAlex API key when configured. Passed as a query param (not
    a session header) so the key stays scoped to OpenAlex requests only and is
    never leaked onto a session shared with Semantic Scholar or Crossref."""
    if OA_KEY:
        return {**params, "api_key": OA_KEY}
    return params


def query_openalex(topic: str, limit: int = 50):
    s = _make_session()
    s.headers.update({"User-Agent": f"deeper-research/1.0 (mailto:{CONTACT})"})
    results = []
    per_page = min(50, limit)
    pages = (limit + per_page - 1) // per_page
    for page in range(1, pages + 1):
        r = s.get(f"{OPENALEX}/works", params=_oa_params({
            "search": topic,
            "per-page": per_page,
            "page": page,
            "sort": "cited_by_count:desc",
            "mailto": CONTACT,
        }), timeout=30)
        if not r.ok:
            break
        for w in r.json().get("results", []):
            authorships = w.get("authorships") or []
            institution = None
            if authorships:
                insts = (authorships[0] or {}).get("institutions") or []
                if insts:
                    institution = (insts[0] or {}).get("display_name")
            results.append({
                "title": (w.get("title") or "").strip(),
                "year": w.get("publication_year"),
                "cited_by": w.get("cited_by_count"),
                "doi": w.get("doi"),
                "id": w.get("id"),
                "referenced_works": w.get("referenced_works") or [],
                "authors": [a.get("author", {}).get("display_name") for a in authorships[:5]],
                "institution": institution,
                "type": w.get("type"),
                "venue": (w.get("host_venue") or {}).get("display_name") or ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "source": "openalex",
            })
        if len(results) >= limit:
            break
    return results[:limit]


def _openalex_work_to_dict(w):
    """Map a raw OpenAlex work record to the same dict shape query_openalex
    produces."""
    authorships = w.get("authorships") or []
    institution = None
    if authorships:
        insts = (authorships[0] or {}).get("institutions") or []
        if insts:
            institution = (insts[0] or {}).get("display_name")
    return {
        "title": (w.get("title") or "").strip(),
        "year": w.get("publication_year"),
        "cited_by": w.get("cited_by_count"),
        "doi": w.get("doi"),
        "id": w.get("id"),
        "referenced_works": w.get("referenced_works") or [],
        "authors": [a.get("author", {}).get("display_name") for a in authorships[:5]],
        "institution": institution,
        "type": w.get("type"),
        "venue": (w.get("host_venue") or {}).get("display_name") or ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
        "source": "openalex",
    }


def openalex_cites(work_ids, limit: int = 25):
    """Return the works that CITE ANY of ``work_ids``, most-cited first — the
    forward candidate pool for citation_chase in ONE (or few) batched requests.

    ``work_ids`` accepts either a single id string OR a list of ids (each a full
    OpenAlex URL or a bare W-id); it is normalized to a list and reduced to bare
    W-ids for the filter. Ids are OR'd via filter=cites:W1|W2|... (pipe = OR),
    at most 50 ids per request; >50 seeds are split across ceil(#ids/50) calls.
    ``limit`` bounds the total works returned across all batches.

    A real HTTP error RAISES OpenAlexError so citation_chase can trigger its
    Semantic Scholar fallback, never a silent empty result."""
    if isinstance(work_ids, str):
        work_ids = [work_ids]
    bare = []
    seen = set()
    for x in work_ids or []:
        b = _bare_openalex_id(x)
        if b and b not in seen:
            seen.add(b)
            bare.append(b)
    if not bare:
        return []
    s = _make_session()
    s.headers.update({"User-Agent": f"deeper-research/1.0 (mailto:{CONTACT})"})
    out = []
    batches = 0
    failed = 0
    for i in range(0, len(bare), 50):
        batches += 1
        batch = bare[i:i + 50]
        try:
            r = s.get(f"{OPENALEX}/works", params=_oa_params({
                "filter": f"cites:{'|'.join(batch)}",
                "sort": "cited_by_count:desc",
                "per-page": limit,
                "mailto": CONTACT,
            }), timeout=30)
        except requests.RequestException:
            # Transport error on this batch — partial-tolerant: record it as a
            # failed batch and CONTINUE the remaining batches.
            failed += 1
            continue
        if not r.ok:
            # Partial-tolerant fail: a real HTTP error on ONE batch is recorded
            # and the loop continues. Only a TOTAL outage (every batch failed)
            # raises below, so citation_chase triggers fallback; a partial
            # outage returns whatever succeeded and does NOT trigger a false 40.
            failed += 1
            continue
        out.extend(_openalex_work_to_dict(w) for w in (r.json().get("results") or []))
    # Raise ONLY on a total outage (every batch failed). One batch of ids means
    # this preserves S2's behavior: a genuine total non-ok still raises.
    if batches and failed == batches:
        raise OpenAlexError(f"OpenAlex cites: all {batches} batch(es) failed")
    return out[:limit]


def openalex_works_by_id(ids):
    """Hydrate metadata for many OpenAlex ids OR DOIs. Inputs are split into two
    groups so a DOI never poisons a W-id batch:

      * W-ids  → filter=openalex_id:W1|W2|... (bare tail form, <=50/call)
      * DOIs   → filter=doi:10.x/a|10.y/b|... (normalized, <=50/call)

    A DOI must NEVER be run through _bare_openalex_id (rsplit on "/" mangles the
    DOI into a bare tail like `physrevlett.116.061102`, which 400s the WHOLE batch
    and loses every valid W-id seed batched alongside it). Result sets are merged.
    HTTP requests are bounded by ceil(#Wids/50) + ceil(#DOIs/50), never one per id.
    """
    bare = []
    seen_bare = set()
    dois = []
    seen_doi = set()
    for x in ids or []:
        if _is_doi_input(x):
            d = _normalize_doi(x)
            if d and d not in seen_doi:
                seen_doi.add(d)
                dois.append(d)
        else:
            b = _bare_openalex_id(x)
            if b and b not in seen_bare:
                seen_bare.add(b)
                bare.append(b)
    if not bare and not dois:
        return []
    s = _make_session()
    s.headers.update({"User-Agent": f"deeper-research/1.0 (mailto:{CONTACT})"})
    out = []
    # Partial-tolerant batching (G5): each batch is attempted independently. A
    # batch that errors is recorded as failed and the loop CONTINUES the other
    # batches, so an early success is never discarded when a later batch fails.
    # We raise OpenAlexError ONLY when EVERY batch failed (a total outage); the
    # caller (citation_chase) treats a raise as a total failure and cascades to
    # Semantic Scholar; a partial return is not a total failure. A single-batch
    # total non-ok still raises, preserving S2's fail-closed behavior.
    counts = {"batches": 0, "failed": 0}

    def _run_batches(values, filter_key):
        for i in range(0, len(values), 50):
            counts["batches"] += 1
            batch = values[i:i + 50]
            try:
                r = s.get(f"{OPENALEX}/works", params=_oa_params({
                    "filter": f"{filter_key}:{'|'.join(batch)}",
                    "per-page": 50,
                    "mailto": CONTACT,
                }), timeout=30)
            except requests.RequestException:
                counts["failed"] += 1
                continue
            if not r.ok:
                counts["failed"] += 1
                continue
            out.extend(_openalex_work_to_dict(w) for w in (r.json().get("results") or []))

    _run_batches(bare, "openalex_id")
    _run_batches(dois, "doi")
    if counts["batches"] and counts["failed"] == counts["batches"]:
        raise OpenAlexError(
            f"OpenAlex works_by_id: all {counts['batches']} batch(es) failed")
    return out


def _semantic_scholar_work_to_dict(p):
    """Map an S2 paper object to the graph-neutral work shape used downstream."""
    external = p.get("externalIds") or {}
    paper_id = p.get("paperId") or p.get("paper_id")
    return {
        "paper_id": paper_id,
        "title": (p.get("title") or "").strip(),
        "year": p.get("year"),
        "cited_by": p.get("citationCount", p.get("cited_by")),
        "doi": external.get("DOI") or p.get("doi"),
        "authors": [a.get("name") for a in (p.get("authors") or [])[:5]
                    if isinstance(a, dict)],
        "venue": p.get("venue"),
        "source": "semantic_scholar",
    }


_S2_MIN_INTERVAL = 1.5  # seconds; S2 limit is 1 req/s CUMULATIVE across all endpoints (margin over 1.0s for their bursty fixed-window enforcement; CappedRetry absorbs stragglers)
_s2_last_request = 0.0


def _s2_throttle():
    """Block until >= _S2_MIN_INTERVAL has elapsed since the last Semantic Scholar
    request. S2's introductory limit is 1 request/second cumulative across EVERY
    endpoint, so the batch / references / citations loops must self-space or they
    429. Process-global; the pipeline runs each script as its own process and they
    execute sequentially, so per-process spacing is sufficient in practice."""
    global _s2_last_request
    wait = _S2_MIN_INTERVAL - (time.monotonic() - _s2_last_request)
    if wait > 0:
        time.sleep(wait)
    _s2_last_request = time.monotonic()


def _semantic_scholar_session():
    session = _make_session()
    if SS_KEY:
        session.headers["x-api-key"] = SS_KEY
    return session


def semantic_scholar_papers_by_id(ids):
    """Resolve DOI:/S2 paper ids through the S2 batch endpoint.

    Batches are partial-tolerant; only a total transport/HTTP failure raises
    SemanticScholarError. A successful batch containing null/no matches returns
    the matches it did resolve (possibly none).
    """
    values = []
    seen = set()
    for value in ids or []:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    if not values:
        return []

    session = _semantic_scholar_session()
    fields = "title,year,citationCount,authors,venue,externalIds"
    out = []
    batches = failed = 0
    for start in range(0, len(values), 500):
        batches += 1
        try:
            _s2_throttle()
            response = session.post(
                f"{SEMANTIC_SCHOLAR}/paper/batch",
                params={"fields": fields},
                json={"ids": values[start:start + 500]},
                timeout=30,
            )
        except requests.RequestException:
            failed += 1
            continue
        if not response.ok:
            failed += 1
            continue
        payload = response.json()
        for paper in payload if isinstance(payload, list) else []:
            if isinstance(paper, dict) and paper.get("paperId"):
                out.append(_semantic_scholar_work_to_dict(paper))
    if batches and failed == batches:
        raise SemanticScholarError(
            f"Semantic Scholar papers_by_id: all {batches} batch(es) failed")
    return out


def semantic_scholar_references(paper_id, limit: int = 100):
    """Return papers referenced by one S2 paper, with graph-ready metadata."""
    if not paper_id:
        return []
    session = _semantic_scholar_session()
    fields = "title,year,citationCount,authors,venue,externalIds"
    try:
        _s2_throttle()
        response = session.get(
            f"{SEMANTIC_SCHOLAR}/paper/{quote(str(paper_id), safe=':')}/references",
            params={"fields": fields, "limit": min(1000, max(1, limit))},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SemanticScholarError("Semantic Scholar references request failed") from exc
    if not response.ok:
        raise SemanticScholarError(
            f"Semantic Scholar references HTTP {response.status_code}")
    out = []
    for edge in response.json().get("data", []) or []:
        paper = (edge or {}).get("citedPaper") or {}
        if paper.get("paperId"):
            out.append(_semantic_scholar_work_to_dict(paper))
    return out


def semantic_scholar_cites(work_ids, limit: int = 25):
    """Return works citing any supplied S2 paper id, partial-tolerant by seed."""
    if isinstance(work_ids, str):
        work_ids = [work_ids]
    ids = list(dict.fromkeys(str(x) for x in (work_ids or []) if x))
    if not ids:
        return []
    session = _semantic_scholar_session()
    fields = "title,year,citationCount,authors,venue,externalIds"
    out = []
    failed = 0
    for paper_id in ids:
        try:
            _s2_throttle()
            response = session.get(
                f"{SEMANTIC_SCHOLAR}/paper/{quote(paper_id, safe=':')}/citations",
                params={"fields": fields, "limit": min(1000, max(1, limit))},
                timeout=30,
            )
        except requests.RequestException:
            failed += 1
            continue
        if not response.ok:
            failed += 1
            continue
        for edge in response.json().get("data", []) or []:
            paper = (edge or {}).get("citingPaper") or {}
            if paper.get("paperId"):
                out.append(_semantic_scholar_work_to_dict(paper))
    if failed == len(ids):
        raise SemanticScholarError(
            f"Semantic Scholar cites: all {len(ids)} request(s) failed")
    out.sort(key=lambda work: -(work.get("cited_by") or 0))
    return out[:limit]


def query_semantic_scholar(topic: str, limit: int = 50):
    s = _semantic_scholar_session()
    try:
        _s2_throttle()
        r = s.get(
            f"{SEMANTIC_SCHOLAR}/paper/search",
            params={
                "query": topic,
                "limit": min(100, limit),
                "fields": "title,year,citationCount,authors.hIndex,authors,venue,externalIds",
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
        h_index = (
            max((a.get("hIndex") or 0) for a in (p.get("authors") or []))
            if p.get("authors")
            else None
        )
        out.append({
            "title": (p.get("title") or "").strip(),
            "year": p.get("year"),
            "cited_by": p.get("citationCount"),
            "doi": (p.get("externalIds") or {}).get("DOI"),
            "authors": [a.get("name") for a in (p.get("authors") or [])[:5]],
            "h_index": h_index,
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
        + (f" · h-index {w.get('h_index')}" if w.get('h_index') else "")
        + (f" · {w.get('institution')}" if w.get('institution') else "")
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

    output_path = Path(args.output)
    with standalone_mutation_guard(output_path, operation="literature search report"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(out), encoding="utf-8")
        output_path.with_suffix(".json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()
