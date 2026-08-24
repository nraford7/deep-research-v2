#!/usr/bin/env python3
"""slice_search.py — Round-1 retrieval: Exa search slices + an academic anchor.

For each ENABLED slice in the run's ``RunConfig`` this script fires one Exa
``/search`` call, tiers every returned result, and writes three artifacts under
``<run-dir>/round1/``:

  * ``slice_<name>.jsonl``       — one JSON object per kept result (the machine feed)
  * ``brief_<name>.md``          — a human-readable brief + a terminal ``## Sources`` block
  * ``evidence_manifest.json``   — {"slices": {name: {"unique", "dropped"}}, "global_unique"}

It ALSO writes ``slice_anchor.jsonl`` from ``lit_search.py``'s OpenAlex /
Semantic Scholar query functions — the academic anchor. The anchor is free
($0) so it is NEVER charged to the retrieval ledger.

MONEY: every Exa call is pre-charged against ``RetrievalLedger`` at
``RETRIEVAL_FEES["slice_search"] * RETRY_MULTIPLIER`` (= $0.04, one automatic
retry) BEFORE the request, then reconciled from the response's
``costDollars.total`` after. A charge that would breach the cap raises
``LedgerCapExceeded`` → exit 21, leaving prior slices' files intact.

RETRIES: the HTTP session pins ``Retry(total=1)`` EXPLICITLY. The ×2 ledger
worst-case bound depends on this — do NOT copy lit_search's ``total=4``.

FAIL-OPEN: any HTTP/parse error for ONE slice writes an empty
``slice_<name>.jsonl`` + prints a notice and the run continues (exit 0). Thin
evidence is the evidence-gate's job to catch, not this script's.

Usage:
  python3 scripts/slice_search.py --run-dir DIR --topic "..." [--resume]
      [--max-retrieval-usd X] [--fresh-since YYYY-MM-DD]
      [--only-slice NAME --query "..."]      # Round-5 single-slice ad-hoc rerun
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - dependency preflight
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)

# Allow running both as `python3 scripts/slice_search.py` and `-m scripts.slice_search`.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from scripts.classify_sources import tier_of
from scripts.cost import RETRIEVAL_FEES, RETRY_MULTIPLIER
from scripts.ledger import RetrievalLedger, LedgerCapExceeded
from scripts import lit_search

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_PREFLIGHT_EXIT = 20
NUM_RESULTS = 15

# Full-text retrieval: ask Exa to extract each result's page/PDF text (server-side,
# so PDF-backed white papers, reports, and articles are read too — not just HTML).
# Capped so a single source can't blow the synthesis context. Override with
# DR_TEXT_MAX_CHARS. Set to 0 to fall back to highlights-only.
TEXT_MAX_CHARS = int(os.environ.get("DR_TEXT_MAX_CHARS", "12000"))

# The per-call worst-case pre-charge (fee × retry multiplier). Single source of
# truth is scripts/cost.py — never hardcode $0.04 here.
SLICE_WORST_CASE = RETRIEVAL_FEES["slice_search"] * RETRY_MULTIPLIER


def make_session():
    """A requests session whose adapter pins ``Retry(total=1)`` EXPLICITLY.

    The ledger's ×2 worst-case bound assumes at most one automatic retry per
    call; do not raise ``total`` here without revisiting RETRY_MULTIPLIER."""
    s = requests.Session()
    retry = Retry(
        total=1,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST", "GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def build_request_body(name, spec, topic, query_override=None, fresh_since=None):
    """Assemble the Exa /search body for one slice.

    ``category`` and ``includeDomains`` are XOR (the Exa contract accepts one
    filter axis, never both). The news slice adds ``startPublishedDate`` when a
    ``--fresh-since`` date is supplied."""
    query = (query_override if query_override is not None
             else spec.query.format(topic=topic))
    contents = {"highlights": True}
    if TEXT_MAX_CHARS > 0:
        contents["text"] = {"maxCharacters": TEXT_MAX_CHARS}
    body = {
        "query": query,
        "numResults": NUM_RESULTS,
        "contents": contents,
    }
    if spec.include_domains:
        body["includeDomains"] = list(spec.include_domains)
    elif spec.category:
        body["category"] = spec.category
    if name == "news" and fresh_since:
        body["startPublishedDate"] = fresh_since
    return body


def _norm_key(url):
    """Normalization key for cross-result dedupe: prefer a DOI when the URL is a
    doi.org link; else lowercase host + path with the trailing slash and utm_*
    query params stripped."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower()
    path = parts.path or ""
    # DOI key when this is a doi.org URL.
    if host.endswith("doi.org"):
        doi = path.strip("/").lower()
        if doi:
            return f"doi:{doi}"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")]
    query = urlencode(kept)
    return urlunsplit(("", host, path, query, "")).lstrip("/") or host


def _result_to_item(raw, name):
    """Map one Exa result dict to the slice JSONL item schema, or None if it has
    no URL (caller drops + counts those)."""
    url = (raw.get("url") or "").strip()
    if not url:
        return None
    venue = raw.get("author") or raw.get("source")
    return {
        "title": (raw.get("title") or "").strip(),
        "url": url,
        "published_date": raw.get("publishedDate") or None,
        "author": raw.get("author") or None,
        "highlights": list(raw.get("highlights") or []),
        # Full extracted page/PDF text (Exa server-side). Kept out of the jsonl
        # index by run_slice — spilled to sources/<file>.txt and replaced with a
        # text_path pointer so the index stays small but synthesis can read it.
        "text": (raw.get("text") or "").strip(),
        "tier": tier_of(url, venue),
        "slice": name,
    }


def _source_filename(item):
    """Deterministic, filesystem-safe name for a source's full-text spill file."""
    key = f"{item.get('slice', 'slice')}:{item.get('url', '')}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{item.get('slice', 'slice')}_{digest}.txt"


def _spill_fulltext(items, round1_dir):
    """Move each item's inline ``text`` into round1/sources/<file>.txt and replace
    it with ``text_path`` (relative to round1_dir) + ``text_chars``. Items with no
    text get text_chars=0 and no path — fetch_fulltext.py fills those later.
    Mutates items in place so the jsonl written afterwards stays lean."""
    sources_dir = round1_dir / "sources"
    for item in items:
        text = (item.pop("text", "") or "").strip()
        if text:
            fname = _source_filename(item)
            sources_dir.mkdir(parents=True, exist_ok=True)
            (sources_dir / fname).write_text(text, encoding="utf-8")
            item["text_path"] = f"sources/{fname}"
            item["text_chars"] = len(text)
        else:
            item["text_chars"] = 0


def _jsonl_parses(path):
    """True iff every non-empty line of ``path`` is valid JSON (used by --resume)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
    return True


def _write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def _year_or_nd(published_date):
    if published_date and len(str(published_date)) >= 4 and str(published_date)[:4].isdigit():
        return str(published_date)[:4]
    return "n.d."


def _write_brief(path, name, items):
    lines = [f"# Round-1 brief — slice `{name}`", ""]
    if items:
        lines += ["> Sources with a `full text` marker have their extracted "
                  "page/PDF text saved under `round1/<text_path>` — read those "
                  "files for evidence, not just the highlight snippets.", ""]
    if not items:
        lines.append("_(no results)_")
    for it in items:
        chars = it.get("text_chars") or 0
        ft = (f" · full text {chars:,} chars → {it['text_path']}"
              if chars and it.get("text_path") else "")
        lines.append(f"- {it['title'] or 'untitled'} — {it['url']} "
                     f"({_year_or_nd(it['published_date'])}) [{it['tier']}]{ft}")
    lines += ["", "## Sources", ""]
    for it in items:
        lines.append(f"- {it['title'] or 'untitled'} — {it['url']} "
                     f"({_year_or_nd(it['published_date'])})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_slice(name, spec, topic, session, api_key, ledger, round1_dir,
              query_override=None, fresh_since=None):
    """Fire one Exa slice, tier + dedupe results, write jsonl + brief.

    Returns (unique_count, dropped_count). Ledger-charges BEFORE the call and
    reconciles from costDollars.total after. HTTP/parse errors fail open: empty
    jsonl + a notice, counts (0, 0). Ledger refusal propagates (caller exits 21)."""
    jsonl_path = round1_dir / f"slice_{name}.jsonl"
    brief_path = round1_dir / f"brief_{name}.md"

    # LEDGER: charge the worst case BEFORE the call. LedgerCapExceeded propagates.
    idx = ledger.charge("slice_search", "slice_search", SLICE_WORST_CASE)

    body = build_request_body(name, spec, topic, query_override, fresh_since)
    try:
        resp = session.post(
            EXA_SEARCH_URL, headers={"x-api-key": api_key},
            json=body, timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — per-slice fail-open by design
        # Reconcile to $0 actual (the call failed) and write an empty slice.
        ledger.reconcile(idx, 0.0)
        _write_jsonl(jsonl_path, [])
        _write_brief(brief_path, name, [])
        print(f"  ⚠ slice '{name}' failed open ({type(exc).__name__}: {exc}) — empty slice written",
              file=sys.stderr)
        return 0, 0

    # Reconcile from costDollars.total when present; else leave conservative
    # worst-case (None keeps the charge, but the spec says reconcile with the
    # actual — None means "unknown", so we pass None to record no better data).
    actual = None
    cost = payload.get("costDollars")
    if isinstance(cost, dict) and "total" in cost:
        try:
            actual = float(cost["total"])
        except (TypeError, ValueError):
            actual = None
    ledger.reconcile(idx, actual)

    seen = set()
    items = []
    dropped = 0
    for raw in payload.get("results") or []:
        item = _result_to_item(raw, name)
        if item is None:
            dropped += 1
            continue
        key = _norm_key(item["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)

    _spill_fulltext(items, round1_dir)
    _write_jsonl(jsonl_path, items)
    _write_brief(brief_path, name, items)
    return len(seen), dropped


def _anchor_item(work):
    """Map a lit_search work dict to the slice item schema. Anchor works may lack
    a resolvable URL; a DOI becomes a doi.org URL, else the OpenAlex id, else the
    row is dropped by the caller."""
    doi = work.get("doi")
    url = ""
    if doi:
        d = str(doi).strip()
        url = d if d.lower().startswith("http") else f"https://doi.org/{d.lstrip('/')}"
    elif work.get("id"):
        url = str(work["id"])
    if not url:
        return None
    venue = work.get("venue")
    year = work.get("year")
    return {
        "title": (work.get("title") or "").strip(),
        "url": url,
        "published_date": str(year) if year else None,
        "author": (work.get("authors") or [None])[0],
        "highlights": [],
        "tier": tier_of(url, venue),
        "slice": "anchor",
    }


def run_anchor(topic, round1_dir):
    """Academic anchor via lit_search's query functions. $0 — never ledgered.

    Fail-open like a slice: any error → empty anchor jsonl. Returns
    (unique_keys, unique_count, dropped)."""
    jsonl_path = round1_dir / "slice_anchor.jsonl"
    brief_path = round1_dir / "brief_anchor.md"
    try:
        oa = lit_search.query_openalex(topic, limit=15)
        ss = lit_search.query_semantic_scholar(topic, limit=15)
        works = lit_search.merge_results(oa, ss)[:15]
    except Exception as exc:  # noqa: BLE001 — fail-open
        _write_jsonl(jsonl_path, [])
        _write_brief(brief_path, "anchor", [])
        print(f"  ⚠ academic anchor failed open ({type(exc).__name__}: {exc})", file=sys.stderr)
        return set(), 0, 0

    seen = set()
    items = []
    dropped = 0
    for w in works:
        item = _anchor_item(w)
        if item is None:
            dropped += 1
            continue
        key = _norm_key(item["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    _spill_fulltext(items, round1_dir)
    _write_jsonl(jsonl_path, items)
    _write_brief(brief_path, "anchor", items)
    return seen, len(seen), dropped


def _slice_keys(jsonl_path):
    """Recompute the set of normalized keys from an existing slice jsonl (used to
    fold a --resume-skipped slice into global_unique)."""
    keys = set()
    try:
        text = Path(jsonl_path).read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        keys.add(_norm_key(obj.get("url", "")))
    return keys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-retrieval-usd", type=float, default=None)
    ap.add_argument("--fresh-since", default=None)
    ap.add_argument("--only-slice", default=None, help="Round-5 single-slice ad-hoc rerun")
    ap.add_argument("--query", default=None, help="ad-hoc query for --only-slice")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    round1_dir = run_dir / "round1"
    round1_dir.mkdir(parents=True, exist_ok=True)

    run_cfg = config.load_run_config()
    cap = args.max_retrieval_usd if args.max_retrieval_usd is not None else run_cfg.max_retrieval_usd

    # Which slices to run.
    if args.only_slice:
        spec = run_cfg.slices.get(args.only_slice)
        if spec is None:
            print(f"unknown slice '{args.only_slice}'", file=sys.stderr)
            return 2
        to_run = {args.only_slice: spec}
    else:
        to_run = {n: s for n, s in run_cfg.slices.items() if s.enabled}

    # PREFLIGHT — only hard-fail when we are actually about to call Exa.
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        print("EXA_API_KEY is not set — cannot run Exa slices. "
              "Export EXA_API_KEY and re-run.", file=sys.stderr)
        return EXA_PREFLIGHT_EXIT

    ledger = RetrievalLedger(run_dir, cap)
    session = make_session()

    manifest_slices = {}
    global_keys = set()

    for name, spec in to_run.items():
        jsonl_path = round1_dir / f"slice_{name}.jsonl"
        if args.resume and jsonl_path.exists() and _jsonl_parses(jsonl_path):
            print(f"  ↻ slice '{name}' present and parses — skipping (resume)")
            keys = _slice_keys(jsonl_path)
            manifest_slices[name] = {"unique": len(keys), "dropped": 0}
            global_keys |= keys
            continue
        try:
            unique, dropped = run_slice(
                name, spec, args.topic, session, api_key, ledger, round1_dir,
                query_override=args.query if args.only_slice else None,
                fresh_since=args.fresh_since,
            )
        except LedgerCapExceeded as exc:
            print(f"  ✗ retrieval cap reached: {exc}", file=sys.stderr)
            print(f"    Prior slices' files are intact. Raise --max-retrieval-usd to continue.",
                  file=sys.stderr)
            return LedgerCapExceeded.EXIT_CODE
        manifest_slices[name] = {"unique": unique, "dropped": dropped}
        global_keys |= _slice_keys(jsonl_path)

    # Academic anchor ($0) — only on a full run, not a Round-5 single-slice rerun.
    if not args.only_slice:
        if args.resume and (round1_dir / "slice_anchor.jsonl").exists() and \
                _jsonl_parses(round1_dir / "slice_anchor.jsonl"):
            akeys = _slice_keys(round1_dir / "slice_anchor.jsonl")
            manifest_slices["anchor"] = {"unique": len(akeys), "dropped": 0}
            global_keys |= akeys
        else:
            akeys, aunique, adropped = run_anchor(args.topic, round1_dir)
            manifest_slices["anchor"] = {"unique": aunique, "dropped": adropped}
            global_keys |= akeys

    manifest = {"slices": manifest_slices, "global_unique": len(global_keys)}
    (round1_dir / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Round-1 retrieval complete: {len(manifest_slices)} slices, "
          f"global_unique={len(global_keys)}, committed=${ledger.committed():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
