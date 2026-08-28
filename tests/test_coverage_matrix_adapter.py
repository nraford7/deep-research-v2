"""test_coverage_matrix_adapter — OFFLINE. Maps pipeline artifacts (scope payload
+ slice rows) into a CoverageMatrix. No network, no subprocess, no clock
(current_year passed in). Covers the five pinned adapter contracts."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import coverage_matrix_adapter as cma  # noqa: E402
from scripts.coverage_matrix import InclusionReason, Lens  # noqa: E402


def _row(url, slice_="web", tier="peer_reviewed", published_date="2020-01-01",
         **extra):
    r = {"url": url, "slice": slice_, "tier": tier,
         "published_date": published_date}
    r.update(extra)
    return r


SCOPE = {"ranked_domains": ["technology", "economics"],
         "domains": ["rand.org", "brookings.edu"]}


# --- cells_from_scope --------------------------------------------------------

def test_cells_from_scope():
    cells = cma.cells_from_scope(SCOPE)
    assert [c.subtopic for c in cells] == ["rand.org", "brookings.edu"]
    assert all(c.lens is Lens.TRADITION for c in cells)  # hostname cells
    # ranked_domains (topic names) are NOT seeded as required cells.
    assert "technology" not in {c.subtopic for c in cells}
    # dedup + order-stable
    dup = cma.cells_from_scope({"domains": ["rand.org", "RAND.org", "x.com"]})
    assert [c.subtopic for c in dup] == ["rand.org", "x.com"]
    # empty / missing
    assert cma.cells_from_scope({}) == []
    assert cma.cells_from_scope({"domains": []}) == []


# --- source_from_row field mapping -------------------------------------------

def test_source_from_row_fields():
    s = cma.source_from_row(_row("https://rand.org/a", slice_="web",
                                 tier="peer_reviewed", published_date="2020"),
                            current_year=2026)
    assert s.lane == "web"
    assert s.authority == 1.0            # peer_reviewed
    assert s.age_years == 6              # 2026 - 2020
    assert s.inclusion_reason is InclusionReason.EVIDENCE
    assert s.relevance == 0.4           # no body → lower


def test_host_match_populates_cell():
    scope_cells = cma.cells_from_scope(SCOPE)
    # a row whose host == a scope hostname covers that cell.
    s = cma.source_from_row(_row("https://rand.org/report"), current_year=2026,
                            scope_cells=scope_cells)
    assert any(c.subtopic == "rand.org" for c in s.cells)
    # a non-matching host covers nothing.
    s2 = cma.source_from_row(_row("https://elsewhere.com/x"), current_year=2026,
                             scope_cells=scope_cells)
    assert s2.cells == frozenset()
    # end to end: matched row shrinks empty_cells.
    m = cma.build_matrix(SCOPE, [_row("https://rand.org/report")],
                         current_year=2026)
    empty = {c.subtopic for c in m.empty_cells()}
    assert "rand.org" not in empty and "brookings.edu" in empty


# --- degraded / missing fields never crash (contract #4) ---------------------

def test_row_no_url_returns_none():
    assert cma.source_from_row(_row("", slice_="web"), current_year=2026) is None
    assert cma.source_from_row({"slice": "web"}, current_year=2026) is None


def test_missing_tier_defaults():
    s = cma.source_from_row({"url": "https://x.com/a"}, current_year=2026)
    assert s.authority == cma._AUTHORITY_DEFAULT  # no tier → lowest


def test_nd_date_age_zero():
    for pd in ("n.d.", None, "", "abcd"):
        s = cma.source_from_row(_row("https://x.com/a", published_date=pd),
                                current_year=2026)
        assert s.age_years == 0  # unparseable → 0, no crash


def test_current_year_none_age_zero():
    s = cma.source_from_row(_row("https://x.com/a", published_date="1990"),
                            current_year=None)
    assert s.age_years == 0  # no current_year → age 0 for all


# --- inclusion reason (contract #1) ------------------------------------------

def test_inclusion_reason_matching():
    r = lambda sl: cma.source_from_row(_row("https://x.com/a", slice_=sl),
                                       current_year=2026).inclusion_reason
    assert r("anchor") is InclusionReason.ANCHOR
    assert r("gap_r1_slime") is InclusionReason.UNIQUE_COVERAGE
    assert r("web") is InclusionReason.EVIDENCE
    assert r("news") is InclusionReason.EVIDENCE


# --- primary dedup via _norm_key (contract #2) -------------------------------

def test_primary_dedup_uses_norm_key():
    # trailing slash + utm_ param normalize to the same key.
    rows = [_row("https://rand.org/report/"),
            _row("https://rand.org/report?utm_source=x")]
    m = cma.build_matrix(SCOPE, rows, current_year=2026)
    assert len(m.primaries()) == 1  # one distinct primary
    # doi.org special case
    assert cma._norm_key("https://doi.org/10.1/AbC") == "doi:10.1/abc"


# --- anchor lane clears the required-lane gate (contract #5) ------------------

def test_anchor_lane_clears_gate():
    # cover the one required cell so status can reach SATURATED once the gate clears.
    scope = {"domains": ["rand.org"]}
    with_anchor = cma.build_matrix(
        scope,
        [_row("https://rand.org/a", slice_="anchor")],
        current_year=2026)
    with_anchor.end_round(); with_anchor.end_round()
    assert with_anchor.status() == "SATURATED"  # anchor lane ran + cell covered
    # same rows but non-anchor lane → required 'anchor' lane never ran → PARTIAL
    without = cma.build_matrix(
        scope,
        [_row("https://rand.org/a", slice_="web")],
        current_year=2026)
    without.end_round(); without.end_round()
    assert without.status() == "PARTIAL"


def test_build_matrix_skips_nones():
    rows = [_row(""), _row("https://rand.org/a")]  # first has no url
    m = cma.build_matrix(SCOPE, rows, current_year=2026)
    assert m._sources and len(m._sources) == 1  # urlless row not admitted


# --- report is JSON-serializable & complete ----------------------------------

def test_matrix_report_serializable():
    m = cma.build_matrix(SCOPE, [_row("https://rand.org/a", slice_="anchor")],
                         current_year=2026)
    rep = cma.matrix_report(m)
    s = json.dumps(rep)  # must not raise
    assert json.loads(s)["status"] in ("OPEN", "PARTIAL", "SATURATED")
    for key in ("status", "empty_cells", "single_primary_cells", "primaries",
                "n_required", "n_sources", "coverage_note"):
        assert key in rep
    # deterministic ordering
    assert cma.matrix_report(m) == rep
