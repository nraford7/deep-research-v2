#!/usr/bin/env python3
"""citation_chase.py — one-hop citation expansion over the Round-1 corpus.

The evidence gate asks "is the corpus thick enough?"; the coverage auditor asks
"what expected coverage is absent?". This asks a THIRD question, purely from the
citation graph and never from an LLM: "which works do the retrieved seeds most
agree are worth citing, and which newer works cite the strongest seeds?" It walks
one hop out of the corpus in two directions:

  * BACKWARD (co-citation): every seed's ``referenced_works`` are counted across
    the whole seed set. A W-id many seeds reference in common is a spine work the
    topic keeps pointing back to.
  * FORWARD (citing works): for the strongest seeds (highest ``cited_by``), the
    works that CITE them are pulled in as candidates, tagged source=forward. This
    surfaces newer follow-on work the backward pass structurally cannot see.

ONE-HOP GUARD: seeds are loaded from round1/slice_anchor.jsonl + round1/slice_*.jsonl
but ``slice_citation*.jsonl`` is EXCLUDED, so a re-run never chases the citations
of citations it already added (unbounded expansion). A seed is any row carrying an
``openalex_id`` OR a ``doi`` (or a doi.org URL a DOI is parseable from).

DEDUPE is bidirectional: the corpus is indexed by ALL identities each row exposes
(``doi:<normDOI>`` AND ``oa:<bareWid>`` when a row carries both), and every
candidate is dropped when EITHER of its own identities is already in the corpus.
So a candidate carrying both a DOI and a W-id matches a corpus row known by only
one of them. The citation slice only ever adds NEW works.

RANKING is mechanical and LLM-FREE: co-citation count desc, then cited_by desc,
then recency (year desc). A WIDER pool (top K by co-citation, K = max(mc*4, mc+20))
is hydrated FIRST so cited_by + year are known before the tie-breakers run, THEN
the hydrated pool is ranked by the full key and the top --max-candidates survivors
are written to round1/slice_citation.jsonl via slice_search._anchor_item (so each row
gets the same tier + authority_tag as an anchor row), then fetch_fulltext and the
evidence gate run over the now-larger corpus.

WHAT IT WRITES: round1/slice_citation.jsonl (when graph expansion adds works), a
short round1/citation_chase.md note, and round1/citation_chase_status.json. ZERO
Bible prose: expanding the corpus is its whole job; synthesis is someone else's.

PROVIDER CASCADE: OpenAlex is primary. If it is unavailable or yields no usable
seed graph, Semantic Scholar is attempted automatically. If that fallback also
fails or cannot resolve a seed, the chase returns 0 in explicit DEGRADED mode and
records graph_verified=false. Downstream stages must carry that limitation into
the final report; the run never labels a skipped graph chase as verified.

CHILD RETURN CODES: after writing the slice this shells out to fetch_fulltext.py
then evidence_gate.py. The gate's exit is surfaced: 0 is clean; 22 (still thin /
a row failed re-validation) is returned nonzero; any other non-zero is surfaced.

CALL CEILING: --openalex-call-ceiling bounds total OpenAlex HTTP requests. When it
is hit the chase stops expanding and proceeds with whatever it has, logging it, so
a graph-heavy topic cannot fan out without bound.

Usage:
  python3 scripts/citation_chase.py --run-dir DIR --topic "..."
      [--max-seeds N] [--max-candidates N] [--forward | --no-forward]
      [--openalex-call-ceiling N]
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Allow running both as `python3 scripts/citation_chase.py` and
# `-m scripts.citation_chase`. config + siblings live at the skill ROOT, mirror
# coverage_audit.py:73-79.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: F401 — imported to mirror the sibling scripts' contract
from scripts import lit_search, slice_search
from scripts.helper_runtime import is_broker_managed, require_managed_mutation, resolve_helper_layout

# Retained for callers that imported the old constants. The default pipeline now
# cascades to Semantic Scholar and then explicit degraded mode instead of emitting
# these provider-specific failures.
CHASE_OPENALEX_UNREACHABLE = 40
CHASE_NO_SEEDS = 41

# evidence_gate's FAIL verdict (corpus still too thin / a row failed re-validation).
# Surfaced as a nonzero chase result: exit 0 is required before synthesis and a
# thin re-gate has not earned it.
GATE_FAIL_EXIT = 22

# How many top seeds (by cited_by) the forward pass expands. Small by design.
FORWARD_TOP_SEEDS = 5
# How many citing works the forward pass pulls per expanded seed. Small by design.
FORWARD_LIMIT = 10
S2_FALLBACK_SEEDS = 5


def _read_jsonl(path):
    """Best-effort parse of a jsonl file into a list of row dicts. A missing or
    malformed file/line contributes nothing rather than raising."""
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _seed_files(round1_dir):
    """Every slice file that may hold SEEDS: slice_anchor.jsonl + slice_*.jsonl,
    with slice_citation*.jsonl EXCLUDED (the one-hop guard, so a re-run never
    chases citations of citations it already added). De-duplicated + sorted.

    NOTE (G1): this exclusion is for SEED SELECTION ONLY. Existing slice_citation
    rows are still folded into the DEDUPE corpus (see _corpus_files) so a re-run
    does not re-add works it added on a prior pass."""
    paths = set()
    anchor = round1_dir / "slice_anchor.jsonl"
    if anchor.exists():
        paths.add(anchor)
    for p in round1_dir.glob("slice_*.jsonl"):
        if p.name.startswith("slice_citation"):
            continue  # one-hop guard (seeds only)
        paths.add(p)
    return sorted(paths)


def _corpus_files(round1_dir):
    """Every slice file whose rows belong to the DEDUPE corpus identity set —
    the seed files PLUS slice_citation*.jsonl. Unlike _seed_files this INCLUDES
    prior citation rows (G1): a Round-5 re-run must see previously-added citation
    works so it neither re-adds nor overwrites them. De-duplicated + sorted."""
    paths = set(_seed_files(round1_dir))
    for p in round1_dir.glob("slice_citation*.jsonl"):
        paths.add(p)
    return sorted(paths)


def _row_doi(row):
    """Normalized DOI for a row, from its ``doi`` field or a doi.org URL. Empty
    string when neither yields a DOI."""
    doi = lit_search._normalize_doi(row.get("doi"))
    if doi:
        return doi
    url = (row.get("url") or "").strip()
    if url:
        low = url.lower()
        if "doi.org/" in low:
            # _normalize_doi strips the doi.org prefix and normalizes.
            return lit_search._normalize_doi(url)
    return ""


def _row_bare_id(row):
    """Bare OpenAlex W-id for a row, from ``openalex_id`` or an openalex.org URL.
    Empty string when neither yields one."""
    oa = row.get("openalex_id")
    if oa:
        return lit_search._bare_openalex_id(oa)
    url = (row.get("url") or "").strip()
    if url and "openalex.org/" in url.lower():
        return lit_search._bare_openalex_id(url)
    return ""


def _row_s2_id(row):
    """Semantic Scholar paper id carried by a row, if present."""
    return str(row.get("semantic_scholar_id") or row.get("paper_id") or "").strip()


def _identities(row):
    """ALL dedupe identities a corpus row exposes — BOTH ``doi:<normDOI>`` (when a
    DOI is present) AND ``oa:<bareWid>`` (when an openalex_id/openalex.org url is
    present). A row known by both contributes both, so a candidate carrying either
    matches. Empty set when the row exposes neither (uncounted)."""
    out = set()
    doi = _row_doi(row)
    if doi:
        out.add(f"doi:{doi}")
    bare = _row_bare_id(row)
    if bare:
        out.add(f"oa:{bare}")
    s2_id = _row_s2_id(row)
    if s2_id:
        out.add(f"s2:{s2_id}")
    return out


def _work_identities(work):
    """ALL identities a raw lit_search work dict (doi/id fields) exposes — BOTH
    doi:<...> and oa:<...> when each is present. A candidate is dropped if EITHER
    is already in the corpus identity set."""
    out = set()
    doi = lit_search._normalize_doi(work.get("doi"))
    if doi:
        out.add(f"doi:{doi}")
    wid = work.get("id")
    if wid:
        out.add(f"oa:{lit_search._bare_openalex_id(wid)}")
    return out


def _hydrate_batch_reqs(ids):
    """Number of OpenAlex HTTP requests openalex_works_by_id will make for these
    ids. DOIs and W-ids are batched SEPARATELY (a DOI never shares a W-id filter),
    so the request count is ceil(#unique_Wids/50) + ceil(#unique_DOIs/50). Mirrors
    the split in lit_search.openalex_works_by_id so the call-ceiling and the
    oa_attempts/oa_failures accounting stay coherent with reality."""
    bare, dois = set(), set()
    for x in ids or []:
        if lit_search._is_doi_input(x):
            d = lit_search._normalize_doi(x)
            if d:
                dois.add(d)
        else:
            b = lit_search._bare_openalex_id(x)
            if b:
                bare.add(b)
    return (len(bare) + 49) // 50 + (len(dois) + 49) // 50


def _s2_seed_input(row):
    paper_id = _row_s2_id(row)
    if paper_id:
        return paper_id
    doi = _row_doi(row)
    return f"DOI:{doi}" if doi else ""


def _s2_work_identities(work):
    out = set()
    doi = lit_search._normalize_doi(work.get("doi"))
    if doi:
        out.add(f"doi:{doi}")
    paper_id = str(work.get("paper_id") or "").strip()
    if paper_id:
        out.add(f"s2:{paper_id}")
    return out


def _citation_row_from_s2(work, source, cocitation_count):
    """Map a normalized Semantic Scholar work to the gate-visible slice schema."""
    paper_id = str(work.get("paper_id") or "").strip()
    doi = lit_search._normalize_doi(work.get("doi"))
    if doi:
        url = f"https://doi.org/{doi}"
    elif paper_id:
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
    else:
        return None
    row = slice_search._anchor_item({
        "title": work.get("title"),
        "doi": doi or None,
        "year": work.get("year"),
        "cited_by": work.get("cited_by"),
        "authors": work.get("authors") or [],
        "venue": work.get("venue"),
    })
    if row is None:
        # _anchor_item needs a DOI or id. A DOI-less S2 paper still has a stable
        # public URL, so supply the minimal gate-valid row directly.
        year = work.get("year")
        row = {
            "title": (work.get("title") or "").strip(),
            "url": url,
            "published_date": str(year) if year else None,
            "author": (work.get("authors") or [None])[0],
            "highlights": [],
            "tier": slice_search.tier_of(url, work.get("venue")),
            "slice": "anchor",
        }
        row["authority_tag"] = slice_search.authority_tag(row)
    row["url"] = url
    row["slice"] = "citation"
    row["semantic_scholar_id"] = paper_id
    row["citation_provider"] = "semantic_scholar"
    row["citation_source"] = source
    row["cocitation_count"] = cocitation_count
    return row


def _write_status(round1_dir, *, mode, graph_verified, openalex_status,
                  semantic_scholar_status, reason):
    status = {
        "mode": mode,
        "graph_verified": bool(graph_verified),
        "openalex_status": openalex_status,
        "semantic_scholar_status": semantic_scholar_status,
        "reason": reason,
    }
    try:
        (round1_dir / "citation_chase_status.json").write_text(
            json.dumps(status, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _merge_citation_rows(round1_dir, rows):
    slice_path = round1_dir / "slice_citation.jsonl"
    prior_rows = _read_jsonl(slice_path) if slice_path.exists() else []
    merged = []
    seen = set()
    for row in prior_rows + rows:
        identities = _identities(row)
        if identities and identities & seen:
            continue
        seen |= identities
        merged.append(row)
    with slice_path.open("w", encoding="utf-8") as handle:
        for row in merged:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return slice_path, len(prior_rows)


def _run_children(run_dir):
    if is_broker_managed():
        from scripts import fetch_fulltext
        ft_rc = fetch_fulltext.main(["--run-dir", str(run_dir)])
    else:
        ft_rc = subprocess.run(
            [sys.executable, str(Path(_ROOT) / "scripts" / "fetch_fulltext.py"),
             "--run-dir", str(run_dir)]
        ).returncode
    if ft_rc != 0:
        print(f"  citation-chase: fetch_fulltext returned {ft_rc} (non-fatal).",
              file=sys.stderr)
    gate_rc = subprocess.run(
        [sys.executable, str(Path(_ROOT) / "scripts" / "evidence_gate.py"),
         "--run-dir", str(run_dir)]
    ).returncode
    if gate_rc == GATE_FAIL_EXIT:
        print(f"  citation-chase: evidence gate returned exit {gate_rc} (corpus "
              "still too thin or a row failed re-validation).", file=sys.stderr)
    elif gate_rc != 0:
        print(f"  citation-chase: evidence gate returned exit {gate_rc} (not a pass).",
              file=sys.stderr)
    return gate_rc


def _semantic_scholar_fallback(*, round1_dir, run_dir, seeds, corpus_identities,
                               max_candidates, forward, reason):
    """Run a bounded S2 citation chase, or record an explicit degraded mode."""
    inputs = []
    for seed in seeds:
        value = _s2_seed_input(seed)
        if value and value not in inputs:
            inputs.append(value)
        if len(inputs) >= S2_FALLBACK_SEEDS:
            break

    def degrade(s2_status, detail):
        note = (
            "Citation chase DEGRADED: OpenAlex could not complete and Semantic "
            f"Scholar {detail}. Citation-graph expansion is unverified; continue "
            "with the evidence-gated base corpus and carry this limitation into "
            "synthesis, integration, and the final Research Bible."
        )
        _write_note(round1_dir, note)
        _write_status(
            round1_dir, mode="degraded", graph_verified=False,
            openalex_status="failed", semantic_scholar_status=s2_status,
            reason=reason,
        )
        print(f"  citation-chase: {note}", file=sys.stderr)
        return 0

    if not inputs:
        return degrade("no-resolvable-seeds", "had no resolvable DOI or paper id")

    try:
        seed_works = lit_search.semantic_scholar_papers_by_id(inputs)
    except Exception:  # noqa: BLE001 — provider failure is recorded, not hidden
        return degrade("failed", "also failed")
    if not seed_works:
        return degrade("no-resolvable-seeds", "resolved no usable seed")

    seed_works.sort(key=lambda work: -(work.get("cited_by") or 0))
    cocite = Counter()
    candidate_source = {}
    candidate_meta = {}
    graph_attempts = graph_failures = 0

    for seed in seed_works[:S2_FALLBACK_SEEDS]:
        paper_id = seed.get("paper_id")
        if not paper_id:
            continue
        graph_attempts += 1
        try:
            references = lit_search.semantic_scholar_references(paper_id, limit=100)
        except Exception:  # noqa: BLE001
            graph_failures += 1
            continue
        for work in references:
            candidate_id = str(work.get("paper_id") or "").strip()
            if not candidate_id:
                continue
            cocite[candidate_id] += 1
            candidate_source.setdefault(candidate_id, "backward")
            candidate_meta[candidate_id] = work

    if forward:
        strong_ids = [work.get("paper_id") for work in seed_works[:FORWARD_TOP_SEEDS]
                      if work.get("paper_id")]
        if strong_ids:
            graph_attempts += 1
            try:
                citing = lit_search.semantic_scholar_cites(
                    strong_ids, limit=FORWARD_LIMIT * len(strong_ids))
            except Exception:  # noqa: BLE001
                graph_failures += 1
                citing = []
            for work in citing:
                candidate_id = str(work.get("paper_id") or "").strip()
                if not candidate_id:
                    continue
                candidate_source.setdefault(candidate_id, "forward")
                candidate_meta[candidate_id] = work

    if graph_attempts and graph_failures == graph_attempts:
        return degrade("failed", "also failed")

    candidate_ids = [paper_id for paper_id in candidate_source
                     if not (_s2_work_identities(candidate_meta[paper_id])
                             & corpus_identities)]
    if not candidate_ids:
        note = ("Citation chase completed via Semantic Scholar fallback; the graph "
                "produced no new works after dedupe.")
        _write_note(round1_dir, note)
        _write_status(
            round1_dir, mode="semantic-scholar-fallback", graph_verified=True,
            openalex_status="failed", semantic_scholar_status="completed",
            reason=reason,
        )
        print(f"  citation-chase: {note}")
        return 0

    try:
        hydrated = lit_search.semantic_scholar_papers_by_id(candidate_ids)
    except Exception:  # noqa: BLE001
        hydrated = []
    for work in hydrated:
        paper_id = str(work.get("paper_id") or "").strip()
        if paper_id:
            candidate_meta[paper_id] = work

    ranked = sorted(
        candidate_ids,
        key=lambda paper_id: (
            -cocite.get(paper_id, 0),
            -(candidate_meta[paper_id].get("cited_by") or 0),
            -(candidate_meta[paper_id].get("year") or 0),
        ),
    )
    rows = []
    written = set()
    for paper_id in ranked:
        if len(rows) >= max_candidates:
            break
        work = candidate_meta[paper_id]
        identities = _s2_work_identities(work)
        if identities & corpus_identities or identities & written:
            continue
        row = _citation_row_from_s2(
            work, candidate_source[paper_id], cocite.get(paper_id, 0))
        if row is None:
            continue
        written |= _identities(row)
        rows.append(row)

    if not rows:
        return degrade("failed", "could not materialize its graph candidates")

    slice_path, n_prior = _merge_citation_rows(round1_dir, rows)
    note = (
        f"Citation chase added {len(rows)} new work(s) via Semantic Scholar fallback "
        f"to {slice_path.name} ({n_prior} prior row(s) preserved)."
    )
    _write_note(round1_dir, note)
    _write_status(
        round1_dir, mode="semantic-scholar-fallback", graph_verified=True,
        openalex_status="failed", semantic_scholar_status="completed",
        reason=reason,
    )
    print(f"Citation chase: {note}")
    return _run_children(run_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--max-seeds", type=int, default=25,
                    help="Cap on the number of seed rows walked (default 25).")
    ap.add_argument("--max-candidates", type=int, default=15,
                    help="Cap on candidate works hydrated + written (default 15).")
    ap.add_argument("--forward", dest="forward", action="store_true", default=True,
                    help="Enable the forward (citing-works) pass (default on, small).")
    ap.add_argument("--no-forward", dest="forward", action="store_false",
                    help="Disable the forward pass; backward co-citation only.")
    ap.add_argument("--openalex-call-ceiling", type=int, default=60,
                    help="Cap on total OpenAlex HTTP requests; on hit, stop "
                         "expanding and proceed with what is gathered (default 60).")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    layout = resolve_helper_layout(run_dir)
    require_managed_mutation(layout, "citation chase")
    round1_dir = layout.round1
    round1_dir.mkdir(parents=True, exist_ok=True)

    # Bounded budget of OpenAlex HTTP requests. Every re-hydration / cites call is
    # charged here; once the ceiling trips the chase proceeds with what it has.
    calls = {"used": 0}
    ceiling = args.openalex_call_ceiling

    def _budget_left():
        return calls["used"] < ceiling

    # ---- Step 1: load seeds + build the dedupe corpus -----------------------
    # SEED rows come from _seed_files (slice_citation EXCLUDED — the one-hop
    # guard). The DEDUPE corpus identity set is built from _corpus_files, which
    # ADDS prior slice_citation rows (G1): a re-run must see works it already
    # added so it neither re-adds nor overwrites them.
    seed_rows = []
    for path in _seed_files(round1_dir):
        seed_rows.extend(_read_jsonl(path))

    # Index the corpus by ALL identities each row exposes (both DOI and W-id when
    # a row carries both), so a candidate matches on EITHER of its own identities.
    # Includes existing slice_citation rows so a rerun does not re-add them.
    corpus_identities = set()
    for path in _corpus_files(round1_dir):
        for r in _read_jsonl(path):
            corpus_identities |= _identities(r)

    # A seed is any SEED row carrying a resolvable openalex_id OR a doi (or a
    # doi.org URL). Cap at --max-seeds. Prefer highest cited_by first so the seed
    # cap keeps the strongest works.
    seed_candidates = [r for r in seed_rows if _row_bare_id(r) or _row_doi(r)]
    seed_candidates.sort(key=lambda r: -(r.get("cited_by") or 0))
    seeds = seed_candidates[: args.max_seeds]

    # ---- Step 2: resolve each seed's bare id + referenced_works -------------
    # Anchor rows already carry openalex_id + referenced_works. Seeds lacking refs
    # (e.g. Exa rows with only a DOI) are collected and BATCH re-hydrated via
    # openalex_works_by_id to recover a bare id + referenced_works.
    # GLOBAL OpenAlex accounting across ALL call sites (seed hydration, forward
    # cites, wider-pool hydration, final hydration). The helpers RAISE
    # OpenAlexError / other errors on a real outage, so each call site wraps its
    # call in try/except and increments oa["failures"] by the request count it
    # attempted. A total outage triggers the Semantic Scholar fallback.
    oa = {"attempts": 0, "failures": 0}

    def _charge(reqs):
        """Record ``reqs`` OpenAlex HTTP requests as attempted (global + budget)."""
        oa["attempts"] += reqs
        calls["used"] += reqs

    # seed_id_by_identity maps a seed's composite identity → its bare W-id (once
    # known), so the forward pass can find seed ids and dedupe stays coherent.
    seed_refs = []          # list of (bare_seed_id_or_None, [bare_ref_ids])
    seed_by_id = {}         # bare W-id → the seed row (for forward cited_by lookup)
    need_hydrate = []       # ids/dois for seeds missing refs, to batch re-hydrate

    for r in seeds:
        bare = _row_bare_id(r)
        refs = r.get("referenced_works")
        if bare and isinstance(refs, list) and refs:
            bare_refs = [lit_search._bare_openalex_id(x) for x in refs]
            seed_refs.append((bare, bare_refs))
            seed_by_id[bare] = r
            continue
        # Missing refs — remember an id to re-hydrate. Prefer a bare W-id; else the
        # DOI (openalex_works_by_id resolves a doi.org form too via bare-id tail).
        if bare:
            need_hydrate.append(bare)
        else:
            doi = _row_doi(r)
            if doi:
                need_hydrate.append(f"https://doi.org/{doi}")

    # BATCH re-hydrate the seeds that lacked refs. openalex_works_by_id batches at
    # <=50 ids/request, so this is ceil(len/50) requests, not one per id.
    if need_hydrate and _budget_left():
        # Number of HTTP requests this call will make (DOIs + W-ids batched at 50
        # SEPARATELY, so a DOI never poisons a W-id batch).
        batch_reqs = _hydrate_batch_reqs(need_hydrate)
        _charge(batch_reqs)
        try:
            hydrated = lit_search.openalex_works_by_id(need_hydrate)
        except Exception:  # noqa: BLE001 — HTTP/network error is a failed attempt
            hydrated = []
            oa["failures"] += batch_reqs
        # An empty (but successful) result is NOT a failure — it is a valid
        # zero-hit query, so oa["failures"] is left untouched here.
        for w in hydrated:
            bare = lit_search._bare_openalex_id(w.get("id")) if w.get("id") else ""
            if not bare:
                continue
            refs = w.get("referenced_works") or []
            bare_refs = [lit_search._bare_openalex_id(x) for x in refs]
            seed_refs.append((bare, bare_refs))
            seed_by_id[bare] = w

    # Detect a total OpenAlex outage at each early-return boundary. The provider
    # cascade handles it: Semantic Scholar next, explicit degraded mode last.
    def _all_openalex_failed():
        return oa["attempts"] > 0 and oa["failures"] >= oa["attempts"]

    def _fallback_return(reason="OpenAlex unavailable"):
        return _semantic_scholar_fallback(
            round1_dir=round1_dir,
            run_dir=run_dir,
            seeds=seeds,
            corpus_identities=corpus_identities,
            max_candidates=args.max_candidates,
            forward=args.forward,
            reason=reason,
        )

    if _all_openalex_failed():
        return _fallback_return()

    # If NO seed yielded a resolvable id/refs, there is nothing to chase.
    resolvable = [sid for sid, _ in seed_refs if sid]
    if not resolvable and not any(refs for _, refs in seed_refs):
        return _fallback_return("OpenAlex produced no resolvable seeds")

    # ---- Step 3: BACKWARD co-citation frequency ----------------------------
    # Count how many seeds reference each bare W-id in common. A ref many seeds
    # point back to is a spine work.
    cocite = Counter()
    for _sid, bare_refs in seed_refs:
        for rid in set(bare_refs):  # per-seed set so one seed counts a ref once
            if rid:
                cocite[rid] += 1

    # candidate_ids maps a candidate bare W-id → source tag ("backward"/"forward").
    # Backward candidates are the referenced W-ids (identity oa:<id>) not already
    # in the corpus. Forward candidates get added below.
    candidate_source = {}
    for rid in cocite:
        if f"oa:{rid}" in corpus_identities:
            continue  # already in the corpus — dedupe
        candidate_source.setdefault(rid, "backward")

    # ---- Step 4: FORWARD citing-works pass (optional, small) ----------------
    # forward_meta caches the hydrated forward works so their cited_by/year rank
    # without a second hydrate.
    forward_meta = {}
    if args.forward:
        # Pick the strongest seeds by cited_by, keyed by their bare W-id. resolvable
        # already holds every seed's bare W-id; seed_by_id[sid] is EITHER the raw
        # anchor ROW (carries the id as ``openalex_id`` / an openalex.org url, NOT
        # ``id``) OR a re-hydrated work dict (carries ``id``). Reading only ``id``
        # here (the F1 bug) dropped every normal anchor seed, so the forward pass
        # never fired. Take the bare id from resolvable, look up cited_by via the
        # seed_by_id value under WHICHEVER field it carries.
        def _seed_cited(sid):
            src = seed_by_id.get(sid) or {}
            return src.get("cited_by") or src.get("cited_by_count") or 0

        strong_ids = sorted(
            set(resolvable), key=lambda sid: -_seed_cited(sid)
        )[:FORWARD_TOP_SEEDS]
        if strong_ids and _budget_left():
            # openalex_cites batches at <=50 ids/request; charge the real batch count
            # so the ceiling stays honest if FORWARD_TOP_SEEDS ever exceeds 50.
            _charge((len(strong_ids) + 49) // 50)
            try:
                citing = lit_search.openalex_cites(strong_ids, limit=FORWARD_LIMIT)
            except Exception:  # noqa: BLE001 — a failed forward call is non-fatal
                oa["failures"] += 1
                citing = []
            for cw in citing:
                ident_set = _work_identities(cw)
                if not ident_set or (ident_set & corpus_identities):
                    continue  # dedupe against the corpus (EITHER identity matches)
                bare = lit_search._bare_openalex_id(cw.get("id")) if cw.get("id") else ""
                if not bare:
                    continue
                candidate_source.setdefault(bare, "forward")
                forward_meta[bare] = cw
        elif strong_ids:
            print(f"  citation-chase: OpenAlex call ceiling ({ceiling}) reached "
                  f"before forward pass; proceeding with what is gathered.",
                  file=sys.stderr)

    if not candidate_source:
        # No candidates. Distinguish "ran cleanly, nothing new" from "every
        # OpenAlex call failed" — a failed forward pass could have emptied the
        # forward candidates while backward was already empty.
        if _all_openalex_failed():
            return _fallback_return()
        # Ran fine; the seeds simply produced no NEW works after dedupe.
        note = ("Citation chase: seeds produced no new works after dedupe "
                "(na). Ran cleanly; nothing to add.")
        _write_note(round1_dir, note)
        _write_status(
            round1_dir, mode="openalex", graph_verified=True,
            openalex_status="completed", semantic_scholar_status="not-needed",
            reason="OpenAlex completed",
        )
        print(f"  citation-chase: {note}")
        return 0

    # ---- Step 5/6: hydrate a WIDER pool, THEN rank -------------------------
    # ACCEPTED MINOR LIMITATIONS (deliberately NOT fixed — do not "fix" these):
    #   * G2: ranking ties at the K-hydrate boundary are broken by insertion
    #     order. Two candidates equal on (co-citation, cited_by, year) can fall
    #     on either side of the K cutoff by the order they entered the pool. The
    #     tie is between works already judged equivalent by every real signal, so
    #     the arbitrary pick is harmless.
    #   * G4: --openalex-call-ceiling is a SOFT per-logical-call bound. A single
    #     logical hydrate/cites call may span several HTTP batches, so the total
    #     request count can slightly overrun the ceiling. OpenAlex is $0, so an
    #     overrun costs nothing and is harmless.
    #
    # Documented order: co-citation count desc → cited_by desc → recency (year
    # desc). The tie-breakers need cited_by + year, which backward candidates do
    # NOT carry until hydrated. Truncating to --max-candidates BEFORE hydration
    # (the F5 bug) left backward cited_by/year at 0, so the cut degraded to
    # co-citation-then-insertion-order and the documented tie-breakers were dead.
    #
    # FIX: hydrate a WIDER pool first. Take the top min(len, K) candidates by
    # co-citation where K = max(--max-candidates*4, --max-candidates+20), hydrate
    # that pool (via openalex_works_by_id) to learn cited_by + year, THEN rank the
    # HYDRATED pool by the full key and take the final --max-candidates. Forward
    # works already carry cited_by/year (forward_meta) and need no re-hydrate. The
    # wider hydrate respects the call ceiling: shrink the pool if it would breach.
    mc = args.max_candidates
    K = max(mc * 4, mc + 20)
    pre_ranked = sorted(
        candidate_source.keys(),
        key=lambda b: (-cocite.get(b, 0), -(forward_meta.get(b, {}).get("cited_by") or 0)),
    )
    pool = pre_ranked[: min(len(pre_ranked), K)]

    # Which pool members still need hydration (forward works are already hydrated).
    hydrated_meta = dict(forward_meta)
    need_meta = [b for b in pool if b not in hydrated_meta]

    # Respect the ceiling: if hydrating the full need_meta would breach the budget,
    # shrink the pool (drop the lowest-co-citation members) until it fits. Each 50
    # W-ids is one request; keep trimming from the tail until batch_reqs fits.
    def _budget_remaining():
        return max(0, ceiling - calls["used"])

    if need_meta:
        while need_meta and _hydrate_batch_reqs(need_meta) > _budget_remaining():
            dropped_id = need_meta.pop()  # tail = lowest co-citation in the pool
            pool = [b for b in pool if b != dropped_id]

    if need_meta and _budget_left():
        batch_reqs = _hydrate_batch_reqs(need_meta)
        _charge(batch_reqs)
        try:
            works = lit_search.openalex_works_by_id(need_meta)
        except Exception:  # noqa: BLE001 — non-fatal; unhydrated ids drop below
            works = []
            oa["failures"] += batch_reqs
        for w in works:
            bare = lit_search._bare_openalex_id(w.get("id")) if w.get("id") else ""
            if bare:
                hydrated_meta[bare] = w
    elif need_meta and not _budget_left():
        print(f"  citation-chase: OpenAlex call ceiling ({ceiling}) reached before "
              f"hydrating {len(need_meta)} backward candidates; they are dropped.",
              file=sys.stderr)

    # Now rank the HYDRATED pool by the FULL documented key and take the final cut.
    def _cited(bare):
        w = hydrated_meta.get(bare)
        return (w.get("cited_by") or 0) if w else 0

    def _year(bare):
        w = hydrated_meta.get(bare)
        return (w.get("year") or 0) if w else 0

    ranked = sorted(
        pool,
        key=lambda b: (-cocite.get(b, 0), -_cited(b), -_year(b)),
    )

    # G3: accept in RANK ORDER, backfilling. The old code cut to ranked[:mc]
    # BEFORE the post-hydration dedupe, so a duplicate inside the top mc shrank
    # the result below mc even though lower-ranked UNIQUE candidates existed.
    # Now iterate the WHOLE ranked pool, accept a candidate only when its
    # identity is new (not in corpus, not already accepted), and stop once mc
    # UNIQUE rows are accepted or the pool is exhausted — a dup at the top is
    # backfilled from the remainder.
    rows = []
    written_identities = set()
    for bare in ranked:
        if len(rows) >= mc:
            break
        work = hydrated_meta.get(bare)
        if not work:
            continue  # could not hydrate (ceiling / network) — drop it
        # _anchor_item computes tier + authority_tag and carries openalex_id +
        # referenced_works. Single dict arg.
        row = slice_search._anchor_item(work)
        if row is None:
            continue
        row["slice"] = "citation"
        doi = lit_search._normalize_doi(work.get("doi"))
        if doi:
            row["url"] = f"https://doi.org/{doi}"
        elif work.get("id"):
            row["url"] = str(work["id"])
        # keep openalex_id on the row (already set by _anchor_item; ensure it).
        if work.get("id"):
            row["openalex_id"] = lit_search._bare_openalex_id(work["id"])
        row["citation_source"] = candidate_source.get(bare, "backward")
        row["cocitation_count"] = cocite.get(bare, 0)
        ident_set = _identities(row)
        # Post-hydration dedupe (BOTH identities). A backward candidate is only
        # known by its bare W-id until hydration reveals its DOI. If EITHER its
        # DOI or its W-id is already in the corpus, drop it now — the earlier
        # oa:<id> guard could not have seen a doi:<...> corpus identity.
        if ident_set & corpus_identities:
            continue
        if ident_set & written_identities:
            continue  # guard against a DOI/id collision inside the batch
        written_identities |= ident_set
        rows.append(row)

    if not rows:
        # If every OpenAlex call failed, the empty result is an outage, not a
        # clean pass — trigger the provider cascade rather than a false na.
        if _all_openalex_failed():
            return _fallback_return()
        note = ("Citation chase: candidates found but none could be hydrated "
                "into rows (na). Ran cleanly; nothing to add.")
        _write_note(round1_dir, note)
        _write_status(
            round1_dir, mode="openalex", graph_verified=True,
            openalex_status="completed", semantic_scholar_status="not-needed",
            reason="OpenAlex completed",
        )
        print(f"  citation-chase: {note}")
        return 0

    # ---- Step 8: write the gate-visible citation slice ---------------------
    # G1: MERGE, don't clobber. If slice_citation.jsonl already exists (a prior
    # round wrote forward/backward results), PRESERVE those rows and APPEND only
    # the genuinely-new ones. New rows are already deduped against a corpus that
    # includes the prior citation rows, so a rerun never re-adds or overwrites a
    # work it added before. Prior rows are kept even if malformed lines are
    # skipped by _read_jsonl (best-effort), deduped by identity for safety.
    slice_path, n_prior = _merge_citation_rows(round1_dir, rows)

    _write_note(
        round1_dir,
        f"Citation chase added {len(rows)} new work(s) to slice_citation.jsonl "
        f"({n_prior} prior row(s) preserved) "
        f"(backward co-citation + {'forward' if args.forward else 'no forward'} pass). "
        f"OpenAlex requests used: {calls['used']}/{ceiling}.",
    )
    _write_status(
        round1_dir, mode="openalex", graph_verified=True,
        openalex_status="completed", semantic_scholar_status="not-needed",
        reason="OpenAlex completed",
    )
    print(f"Citation chase: wrote {len(rows)} new works to {slice_path.name} "
          f"({n_prior} prior preserved; OpenAlex requests {calls['used']}/{ceiling}).")

    # ---- Step 9: fetch full text, then re-run the evidence gate ------------
    return _run_children(run_dir)


def _write_note(round1_dir, message):
    """Write round1/citation_chase.md — the ONLY prose the chase produces (no
    Bible text). Best-effort: a write failure is non-fatal."""
    round1_dir.mkdir(parents=True, exist_ok=True)
    body = "# Citation chase\n\n" + message.strip() + "\n"
    try:
        (round1_dir / "citation_chase.md").write_text(body, encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
