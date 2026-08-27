"""citation_chase — OFFLINE. Every citation-graph helper is monkeypatched and the
fetch_fulltext / evidence_gate subprocess shell-outs are stubbed, so NO network
and NO real child process ever run.

Covers: seed selection, the one-hop guard (slice_citation excluded), co-citation
ranking, composite identity dedupe, OpenAlex → Semantic Scholar fallback,
explicit degraded mode, and a gate-visible citation slice.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import citation_chase, lit_search


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _write_slice(round1, name, rows):
    round1.mkdir(parents=True, exist_ok=True)
    p = round1 / f"slice_{name}.jsonl"
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def _stub_children(monkeypatch, gate_rc=0):
    """Stub every subprocess.run so no real fetch_fulltext / evidence_gate runs.
    The last child (evidence_gate) returns gate_rc; earlier ones return 0."""
    calls = {"n": 0}

    def fake_run(argv, *a, **k):
        calls["n"] += 1
        if "evidence_gate.py" in " ".join(str(x) for x in argv):
            return _Proc(gate_rc)
        return _Proc(0)
    monkeypatch.setattr(citation_chase.subprocess, "run", fake_run)
    return calls


def _stub_s2_failure(monkeypatch):
    """Make Semantic Scholar unavailable without allowing a real network call."""
    def boom(*_args, **_kwargs):
        raise lit_search.SemanticScholarError("Semantic Scholar unavailable")

    monkeypatch.setattr(lit_search, "semantic_scholar_papers_by_id", boom)
    monkeypatch.setattr(lit_search, "semantic_scholar_references", boom)
    monkeypatch.setattr(lit_search, "semantic_scholar_cites", boom)


def _seed_row(bare_id=None, doi=None, refs=None, cited=0, extra=None):
    row = {"title": "Seed", "tier": "peer_reviewed", "slice": "anchor"}
    if bare_id:
        row["openalex_id"] = bare_id
        row["url"] = f"https://openalex.org/{bare_id}"
    if doi:
        row["doi"] = doi
        row["url"] = f"https://doi.org/{doi}"
    if refs is not None:
        row["referenced_works"] = refs
    if cited:
        row["cited_by"] = cited
    if extra:
        row.update(extra)
    return row


# --- seed selection ---------------------------------------------------------

def test_seed_selection_skips_rows_without_id_or_doi(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # W1 references W9 twice-over via two seeds; a bare web row has no id/doi and
    # must be skipped as a seed.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="W1", refs=["W9"]),
        _seed_row(bare_id="W2", refs=["W9"]),
    ])
    _write_slice(round1, "web", [
        {"url": "https://news.example/x", "tier": "news", "slice": "web"},
    ])
    seen_ids = {"all": []}

    def fake_by_id(ids):
        seen_ids["all"].extend(lit_search._bare_openalex_id(i) for i in ids)
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": lit_search._bare_openalex_id(i),
                 "referenced_works": [], "cited_by_count": 0} for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    rc = citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])
    assert rc == 0
    # Both anchor rows already carried refs; the url-only web row never entered
    # the seed set (no openalex_id / doi), so its url was never walked as a seed.
    # W9 (the co-cited ref of the two real seeds) IS the only candidate hydrated.
    assert seen_ids["all"] == ["W9"]
    slice_path = round1 / "slice_citation.jsonl"
    rows = [json.loads(l) for l in slice_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["openalex_id"] == "W9"


# --- one-hop guard ----------------------------------------------------------

def test_one_hop_guard_excludes_slice_citation(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [_seed_row(bare_id="W1", refs=["W9"])])
    # A prior citation slice: its rows must NEVER be used as seeds (no chasing
    # citations of citations). W1000's refs would leak in if it were a seed.
    _write_slice(round1, "citation", [
        _seed_row(bare_id="W1000", refs=["W777"], extra={"slice": "citation"}),
    ])

    hydrate_calls = {"ids": []}

    def fake_by_id(ids):
        hydrate_calls["ids"].extend(ids)
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": "cand", "referenced_works": [], "cited_by_count": 0}
                for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    ids_written = {r.get("openalex_id") for r in rows}
    # W9 (from the anchor seed) is a candidate; W777 (from the excluded citation
    # slice) must NOT appear — its seed was never walked.
    assert "W9" in ids_written
    assert "W777" not in ids_written


# --- co-citation ranking ----------------------------------------------------

def test_cocitation_ranking_prefers_more_referenced_work(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # W_HOT is referenced by 3 seeds; W_COLD by 1. With max-candidates 1, only the
    # more-co-cited work survives the ranked cut.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_HOT", "W_COLD"]),
        _seed_row(bare_id="A2", refs=["W_HOT"]),
        _seed_row(bare_id="A3", refs=["W_HOT"]),
    ])

    def fake_by_id(ids):
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": lit_search._bare_openalex_id(i),
                 "referenced_works": [], "cited_by_count": 1}
                for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward",
        "--max-candidates", "1"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["openalex_id"] == "W_HOT"
    assert rows[0]["cocitation_count"] == 3


# --- F1: forward pass fires on normal anchor seeds (openalex_id, not id) -----

def test_forward_pass_uses_openalex_id_from_anchor_seeds(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Two anchor seeds carrying refs + openalex_id (NOT `id`) — the shape
    # _anchor_item persists. The forward pass must resolve their W-ids from
    # `openalex_id` and pass them to openalex_cites, or it never fires (F1).
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_BACK"], cited=100),
        _seed_row(bare_id="A2", refs=["W_BACK"], cited=50),
    ])

    cites_seen = {"ids": None}

    def fake_cites(work_ids, limit=10):
        # openalex_cites now takes a LIST of bare W-ids. Record what it got.
        cites_seen["ids"] = list(work_ids) if isinstance(work_ids, list) else [work_ids]
        return [{"id": "https://openalex.org/W_FWD", "title": "citing",
                 "referenced_works": [], "cited_by_count": 9,
                 "publication_year": 2024}]
    monkeypatch.setattr(lit_search, "openalex_cites", fake_cites)
    monkeypatch.setattr(
        lit_search, "openalex_works_by_id",
        lambda ids: [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                      "title": lit_search._bare_openalex_id(i),
                      "referenced_works": [], "cited_by_count": 0} for i in ids])
    _stub_children(monkeypatch)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    # The forward pass fired with the seeds' bare W-ids (from openalex_id), so the
    # citing work W_FWD is present and tagged forward.
    assert cites_seen["ids"] is not None, "forward pass never called openalex_cites"
    assert set(cites_seen["ids"]) == {"A1", "A2"}
    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    fwd = [r for r in rows if r.get("openalex_id") == "W_FWD"]
    assert fwd and fwd[0]["citation_source"] == "forward"


# --- F5: ranking hydrates a wider pool, so cited_by breaks co-citation ties --

def test_ranking_hydrates_wider_pool_for_tiebreak(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Two backward candidates tie on co-citation (each referenced by both seeds).
    # W_HI has high cited_by, W_LO low. With max-candidates=1 the cut MUST keep
    # W_HI — only possible if BOTH were hydrated BEFORE the cut (F5). If the old
    # code truncated to 1 before hydration, cited_by would be 0 for both and the
    # tie would resolve by insertion order, not cited_by.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_LO", "W_HI"]),
        _seed_row(bare_id="A2", refs=["W_LO", "W_HI"]),
    ])

    def fake_by_id(ids):
        # Mirror the real openalex_works_by_id return shape (mapped dict: `cited_by`
        # and `year`, not the raw OpenAlex `cited_by_count`/`publication_year`).
        out = []
        for i in ids:
            bare = lit_search._bare_openalex_id(i)
            cited = 999 if bare == "W_HI" else 1
            out.append({"id": f"https://openalex.org/{bare}", "title": bare,
                        "referenced_works": [], "cited_by": cited, "year": 2020})
        return out
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward",
        "--max-candidates", "1"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    # cited_by (999 vs 1) broke the co-citation tie — proves the wider pool was
    # hydrated before the final cut.
    assert rows[0]["openalex_id"] == "W_HI"


# --- composite dedupe (DOI vs bare OpenAlex id) -----------------------------

def test_dedupe_candidate_already_in_corpus_by_doi(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # The corpus already holds a work under its DOI. A co-cited candidate W_DUP
    # hydrates to that SAME doi, so it must be dropped even though the graph
    # expressed it as a bare OpenAlex id.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_DUP", "W_NEW"]),
        _seed_row(bare_id="A2", refs=["W_DUP", "W_NEW"]),
        # already-in-corpus row, keyed by DOI only (no openalex_id)
        {"url": "https://doi.org/10.5/dup", "doi": "10.5/dup",
         "tier": "peer_reviewed", "slice": "anchor"},
    ])

    def fake_by_id(ids):
        out = []
        for i in ids:
            bare = lit_search._bare_openalex_id(i)
            doi = "10.5/dup" if bare == "W_DUP" else None
            out.append({"id": f"https://openalex.org/{bare}", "title": bare,
                        "doi": doi, "referenced_works": [], "cited_by_count": 0})
        return out
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    urls = {r["url"] for r in rows}
    ids = {r.get("openalex_id") for r in rows}
    # W_NEW survives; the DOI-duplicate W_DUP is dropped by composite identity.
    assert "W_NEW" in ids
    assert not any("10.5/dup" in u for u in urls)


def test_dedupe_bare_id_candidate_matches_corpus_openalex_id(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # The reverse case: the corpus row carries a bare openalex_id and the candidate
    # is also a bare id — same identity, so dropped.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_INCORP", "W_NEW"]),
        _seed_row(bare_id="A2", refs=["W_INCORP", "W_NEW"]),
        _seed_row(bare_id="W_INCORP", refs=[]),  # already in corpus by bare id
    ])

    def fake_by_id(ids):
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": lit_search._bare_openalex_id(i),
                 "referenced_works": [], "cited_by_count": 0} for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    ids = {r.get("openalex_id") for r in rows}
    assert "W_NEW" in ids
    assert "W_INCORP" not in ids


# --- F4: bidirectional dedupe (candidate has DOI+Wid, corpus knows only Wid) -

def test_dedupe_candidate_with_both_ids_matches_corpus_wid_only(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # The corpus knows W_DUP ONLY by its bare W-id. The candidate hydrates to BOTH
    # a DOI and that same W-id. Old code checked the candidate by its DOI only
    # (preferred identity) and missed the W-id match, leaking a duplicate. F4:
    # EITHER identity matching the corpus drops it.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_DUP", "W_NEW"]),
        _seed_row(bare_id="A2", refs=["W_DUP", "W_NEW"]),
        _seed_row(bare_id="W_DUP", refs=[]),  # corpus knows W_DUP by W-id only
    ])

    def fake_by_id(ids):
        out = []
        for i in ids:
            bare = lit_search._bare_openalex_id(i)
            # W_DUP hydrates carrying BOTH a DOI and its W-id.
            doi = "10.7/dup" if bare == "W_DUP" else None
            out.append({"id": f"https://openalex.org/{bare}", "title": bare,
                        "doi": doi, "referenced_works": [], "cited_by_count": 0})
        return out
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    ids = {r.get("openalex_id") for r in rows}
    assert "W_NEW" in ids
    assert "W_DUP" not in ids  # dropped by its W-id identity despite carrying a DOI


# --- provider cascade: OpenAlex → Semantic Scholar → degraded ---------------

def test_openalex_success_does_not_call_semantic_scholar(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W-SPINE"]),
        _seed_row(bare_id="A2", refs=["W-SPINE"]),
    ])

    monkeypatch.setattr(
        lit_search, "openalex_works_by_id",
        lambda ids: [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                      "title": "Spine", "cited_by": 5, "year": 2018,
                      "referenced_works": []} for i in ids],
    )
    monkeypatch.setattr(lit_search, "openalex_cites", lambda *_a, **_k: [])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Semantic Scholar must not run after OpenAlex succeeds")

    monkeypatch.setattr(lit_search, "semantic_scholar_papers_by_id", forbidden)
    monkeypatch.setattr(lit_search, "semantic_scholar_references", forbidden)
    monkeypatch.setattr(lit_search, "semantic_scholar_cites", forbidden)
    _stub_children(monkeypatch)

    rc = citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    assert rc == 0
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status["mode"] == "openalex"
    assert status["graph_verified"] is True
    assert status["semantic_scholar_status"] == "not-needed"

def test_forward_only_openalex_failure_downgrades_when_s2_has_no_seed(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Single seed already carrying refs (no re-hydrate). Its only ref is already
    # in the corpus, so there are NO backward candidates. The forward cites call
    # then RAISES. These seeds have no DOI/S2 id, so the required S2 fallback has
    # no resolvable input and the run must continue in explicit degraded mode.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["A2"]),
        _seed_row(bare_id="A2", refs=["A1"]),
    ])

    def boom_cites(work_ids, limit=10):
        raise lit_search.OpenAlexError("OpenAlex HTTP 503")
    monkeypatch.setattr(lit_search, "openalex_cites", boom_cites)
    # No re-hydrate needed (seeds carry refs); works_by_id must not be the thing
    # that decides the exit code here.
    monkeypatch.setattr(lit_search, "openalex_works_by_id", lambda ids: [])

    def no_child(*a, **k):
        raise AssertionError("no child process when degraded without new rows")
    monkeypatch.setattr(citation_chase.subprocess, "run", no_child)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status["mode"] == "degraded"
    assert status["graph_verified"] is False
    assert not (round1 / "slice_citation.jsonl").exists()


# --- Semantic Scholar fallback succeeds -------------------------------------

def test_openalex_failure_uses_semantic_scholar_fallback(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        _seed_row(doi="10.1/a", cited=20),
        _seed_row(doi="10.1/b", cited=10),
    ])

    def openalex_down(_ids):
        raise lit_search.OpenAlexError("OpenAlex HTTP 429")

    monkeypatch.setattr(lit_search, "openalex_works_by_id", openalex_down)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda *_a, **_k: [])

    def s2_by_id(ids):
        out = []
        for value in ids:
            if value == "DOI:10.1/a":
                out.append({"paper_id": "S2-A", "doi": "10.1/a", "title": "Seed A",
                            "cited_by": 20, "year": 2020, "source": "semantic_scholar"})
            elif value == "DOI:10.1/b":
                out.append({"paper_id": "S2-B", "doi": "10.1/b", "title": "Seed B",
                            "cited_by": 10, "year": 2021, "source": "semantic_scholar"})
            elif value == "S2-SPINE":
                out.append({"paper_id": "S2-SPINE", "doi": "10.2/spine",
                            "title": "Shared spine", "cited_by": 99, "year": 2015,
                            "authors": ["A. Scholar"], "venue": "Journal",
                            "source": "semantic_scholar"})
        return out

    monkeypatch.setattr(lit_search, "semantic_scholar_papers_by_id", s2_by_id)
    monkeypatch.setattr(
        lit_search, "semantic_scholar_references",
        lambda paper_id, limit=100: [{"paper_id": "S2-SPINE", "title": "Shared spine"}],
    )
    monkeypatch.setattr(lit_search, "semantic_scholar_cites", lambda *_a, **_k: [])
    _stub_children(monkeypatch)

    rc = citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])

    assert rc == 0
    rows = [json.loads(line) for line in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["semantic_scholar_id"] == "S2-SPINE"
    assert rows[0]["citation_provider"] == "semantic_scholar"
    assert rows[0]["cocitation_count"] == 2
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status == {
        "mode": "semantic-scholar-fallback",
        "graph_verified": True,
        "openalex_status": "failed",
        "semantic_scholar_status": "completed",
        "reason": "OpenAlex unavailable",
    }


# --- both graph providers fail → explicit degraded mode ---------------------

def test_both_graph_providers_fail_downgrades_and_continues(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Seeds carry a DOI only (no refs), forcing a re-hydrate call — which raises.
    _write_slice(round1, "anchor", [
        {"url": "https://doi.org/10.1/a", "doi": "10.1/a",
         "tier": "peer_reviewed", "slice": "anchor"},
    ])

    def boom(ids):
        raise RuntimeError("network down")
    monkeypatch.setattr(lit_search, "openalex_works_by_id", boom)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_s2_failure(monkeypatch)
    # No new slice means there is no need to re-run the child fetch/gate steps.
    def no_child(*a, **k):
        raise AssertionError("no child process when degraded without new rows")
    monkeypatch.setattr(citation_chase.subprocess, "run", no_child)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    note = (round1 / "citation_chase.md").read_text()
    assert "degraded" in note.lower()
    assert "openalex" in note.lower()
    assert "semantic scholar" in note.lower()
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status["mode"] == "degraded"
    assert status["graph_verified"] is False
    assert status["openalex_status"] == "failed"
    assert status["semantic_scholar_status"] == "failed"
    assert not (round1 / "slice_citation.jsonl").exists()


# --- a real OpenAlexError also enters the provider cascade ------------------

def test_openalex_error_from_helper_enters_degraded_mode_if_s2_fails(monkeypatch, tmp_path):
    # A server HTTP error surfaces as lit_search.OpenAlexError from the helper.
    # citation_chase must count it as a provider failure and enter the fallback
    # cascade, then make the final degraded state explicit when S2 also fails.
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        {"url": "https://doi.org/10.1/a", "doi": "10.1/a",
         "tier": "peer_reviewed", "slice": "anchor"},
    ])

    def raise_oa(ids):
        raise lit_search.OpenAlexError("OpenAlex HTTP 503")
    monkeypatch.setattr(lit_search, "openalex_works_by_id", raise_oa)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_s2_failure(monkeypatch)

    def no_child(*a, **k):
        raise AssertionError("no child process when degraded without new rows")
    monkeypatch.setattr(citation_chase.subprocess, "run", no_child)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status["mode"] == "degraded"
    assert not (round1 / "slice_citation.jsonl").exists()


# --- call ceiling bounds total OpenAlex requests ----------------------------

def test_openalex_call_ceiling_bounds_requests(monkeypatch, tmp_path):
    # With a small --openalex-call-ceiling, the forward pass must stop expanding
    # once the budget trips, so total OpenAlex requests stay bounded by the ceiling.
    round1 = tmp_path / "round1"
    # Many strong seeds, each already carrying refs (no re-hydrate), so the only
    # OpenAlex traffic is the forward cites pass — one request per expanded seed.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id=f"S{i}", refs=["W_HOT"], cited=100 - i)
        for i in range(5)
    ])

    cites_calls = {"n": 0}

    def counting_cites(wid, limit=10):
        cites_calls["n"] += 1
        return []
    monkeypatch.setattr(lit_search, "openalex_cites", counting_cites)
    monkeypatch.setattr(
        lit_search, "openalex_works_by_id",
        lambda ids: [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                      "title": lit_search._bare_openalex_id(i),
                      "referenced_works": [], "cited_by_count": 0} for i in ids])
    _stub_children(monkeypatch)

    citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t",
        "--openalex-call-ceiling", "2"])

    # The forward pass expands at most 2 seeds before the ceiling of 2 trips.
    assert cites_calls["n"] <= 2


# --- no resolvable seed also attempts fallback, then degrades ----------------

def test_no_resolvable_seed_downgrades_after_s2_no_match(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Seeds carry a DOI (so they enter the seed set) but re-hydration returns
    # nothing usable — no bare id, no refs anywhere → nothing to chase.
    _write_slice(round1, "anchor", [
        {"url": "https://doi.org/10.1/a", "doi": "10.1/a",
         "tier": "peer_reviewed", "slice": "anchor"},
    ])
    monkeypatch.setattr(lit_search, "openalex_works_by_id", lambda ids: [])
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    monkeypatch.setattr(lit_search, "semantic_scholar_papers_by_id", lambda ids: [])
    monkeypatch.setattr(lit_search, "semantic_scholar_references", lambda *_a, **_k: [])
    monkeypatch.setattr(lit_search, "semantic_scholar_cites", lambda *_a, **_k: [])
    def no_child(*a, **k):
        raise AssertionError("no child process when degraded without new rows")
    monkeypatch.setattr(citation_chase.subprocess, "run", no_child)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    status = json.loads((round1 / "citation_chase_status.json").read_text())
    assert status["mode"] == "degraded"
    assert status["semantic_scholar_status"] == "no-resolvable-seeds"
    assert not (round1 / "slice_citation.jsonl").exists()


# --- gate-visible: slice matches slice_*.jsonl glob with valid url+tier ------

def test_gate_visible_rows_have_url_and_tier(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_HOT"]),
        _seed_row(bare_id="A2", refs=["W_HOT"]),
    ])

    def fake_by_id(ids):
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": "spine", "doi": "10.9/spine",
                 "referenced_works": [], "cited_by_count": 5} for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch, gate_rc=0)

    rc = citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])
    assert rc == 0

    slice_path = round1 / "slice_citation.jsonl"
    assert slice_path.exists()
    # It IS matched by the same glob the evidence gate / fetch_fulltext walk.
    assert slice_path in set(round1.glob("slice_*.jsonl"))
    rows = [json.loads(l) for l in slice_path.read_text().splitlines() if l.strip()]
    assert rows, "citation slice must carry rows"
    for r in rows:
        assert r.get("url")
        assert r.get("tier")
        assert r.get("slice") == "citation"


# --- G1: a rerun preserves prior slice_citation rows + does not re-add them --

def test_rerun_preserves_prior_citation_rows_and_does_not_readd(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # Two anchor seeds co-cite W_OLD (already added on a prior pass) and W_NEW.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_OLD", "W_NEW"]),
        _seed_row(bare_id="A2", refs=["W_OLD", "W_NEW"]),
    ])
    # A prior citation slice already holds W_OLD (with real body text). A rerun
    # must (a) NOT re-add W_OLD as a "new" candidate (it is in the dedupe corpus)
    # and (b) NOT clobber the prior row — it is preserved in the merged output.
    _write_slice(round1, "citation", [
        {"openalex_id": "W_OLD", "url": "https://openalex.org/W_OLD",
         "tier": "peer_reviewed", "slice": "citation",
         "text_chars": 5000, "text_path": "sources/w_old.txt",
         "cocitation_count": 2, "citation_source": "backward"},
    ])

    def fake_by_id(ids):
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": lit_search._bare_openalex_id(i),
                 "referenced_works": [], "cited_by_count": 0} for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])
    assert rc == 0

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    ids = [r.get("openalex_id") for r in rows]
    # W_OLD appears exactly once (not re-added / duplicated) and its prior body
    # text survived (not clobbered). W_NEW was added.
    assert ids.count("W_OLD") == 1
    assert "W_NEW" in ids
    old = next(r for r in rows if r["openalex_id"] == "W_OLD")
    assert old["text_chars"] == 5000
    assert old["text_path"] == "sources/w_old.txt"


# --- G3: backfill reaches max_candidates when a top candidate is a dup --------

def test_backfill_reaches_max_candidates_when_top_is_post_hydration_dup(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    # The corpus already holds a work by DOI only (no W-id). Two backward
    # candidates tie on co-citation: W_DUP hydrates to that SAME DOI (a
    # post-hydration duplicate, invisible before hydration) and ranks FIRST by
    # cited_by; W_KEEP is unique. With max-candidates=1 the old code cut to
    # [W_DUP] then dropped it on the post-hydration dedupe → 0 rows. G3 backfills
    # from the ranked remainder, so W_KEEP fills the slot → exactly 1 row.
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_DUP", "W_KEEP"]),
        _seed_row(bare_id="A2", refs=["W_DUP", "W_KEEP"]),
        {"url": "https://doi.org/10.9/dup", "doi": "10.9/dup",
         "tier": "peer_reviewed", "slice": "anchor"},
    ])

    def fake_by_id(ids):
        out = []
        for i in ids:
            bare = lit_search._bare_openalex_id(i)
            # W_DUP: high cited_by (ranks first) + the corpus DOI (post-hyd dup).
            # W_KEEP: lower cited_by, unique.
            doi = "10.9/dup" if bare == "W_DUP" else None
            cited = 999 if bare == "W_DUP" else 1
            out.append({"id": f"https://openalex.org/{bare}", "title": bare,
                        "doi": doi, "referenced_works": [],
                        "cited_by": cited, "year": 2020})
        return out
    monkeypatch.setattr(lit_search, "openalex_works_by_id", fake_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward",
        "--max-candidates", "1"])

    rows = [json.loads(l) for l in (round1 / "slice_citation.jsonl")
            .read_text().splitlines() if l.strip()]
    ids = {r.get("openalex_id") for r in rows}
    # Backfill reached max_candidates: exactly 1 unique row, and it is W_KEEP —
    # NOT the dropped duplicate, and NOT an empty result.
    assert len(rows) == 1
    assert ids == {"W_KEEP"}


# --- G5: a PARTIAL OpenAlex outage does NOT exit 40 (only a TOTAL one does) ---

def test_partial_openalex_outage_does_not_exit_40(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_HOT"]),
        _seed_row(bare_id="A2", refs=["W_HOT"]),
    ])

    # openalex_works_by_id RETURNS partial results (does not raise) — the G5
    # contract for a partial outage. citation_chase must treat a non-raising
    # return as success (no failed attempt recorded), so no false exit 40.
    def partial_by_id(ids):
        # Behaves like a partial success: returns SOME hydrated works, no raise.
        return [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                 "title": lit_search._bare_openalex_id(i),
                 "referenced_works": [], "cited_by_count": 3} for i in ids]
    monkeypatch.setattr(lit_search, "openalex_works_by_id", partial_by_id)
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch)

    rc = citation_chase.main(["--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])
    assert rc != citation_chase.CHASE_OPENALEX_UNREACHABLE
    assert rc == 0
    assert (round1 / "slice_citation.jsonl").exists()


def test_gate_fail_exit_22_is_surfaced(monkeypatch, tmp_path):
    round1 = tmp_path / "round1"
    _write_slice(round1, "anchor", [
        _seed_row(bare_id="A1", refs=["W_HOT"]),
        _seed_row(bare_id="A2", refs=["W_HOT"]),
    ])
    monkeypatch.setattr(lit_search, "openalex_works_by_id",
                        lambda ids: [{"id": f"https://openalex.org/{lit_search._bare_openalex_id(i)}",
                                      "title": "x", "referenced_works": [],
                                      "cited_by_count": 0} for i in ids])
    monkeypatch.setattr(lit_search, "openalex_cites", lambda wid, limit=10: [])
    _stub_children(monkeypatch, gate_rc=citation_chase.GATE_FAIL_EXIT)

    rc = citation_chase.main([
        "--run-dir", str(tmp_path), "--topic", "t", "--no-forward"])
    # The slice was still written and is gate-visible, but a thin re-gate (22)
    # surfaces nonzero — exit 0 is not earned.
    assert rc == citation_chase.GATE_FAIL_EXIT
    assert (round1 / "slice_citation.jsonl").exists()
