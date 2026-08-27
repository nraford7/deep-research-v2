"""test_coverage_matrix — OFFLINE. Pure-logic core; no network, no subprocess,
no clock, no randomness. Every case is a deterministic function of its inputs.

Covers the design spec's success criteria 1-8 and each named edge case:
ranking (coverage-beats-authority, tie-break), per-slice age weighting, typed
inclusion reasons, primary-space dedup, the two-condition stop over a growing
map, required-lane gating and its precedence, and the enumerated invariants.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import coverage_matrix as cm  # noqa: E402
from scripts.coverage_matrix import (  # noqa: E402
    Cell,
    CoverageMatrix,
    InclusionReason,
    Lens,
    SliceWeighting,
    Source,
)

# --- helpers -----------------------------------------------------------------

C_A = Cell("photosynthesis", Lens.METHOD)
C_B = Cell("photosynthesis", Lens.TRADITION)
C_C = Cell("respiration", Lens.SUBCLAIM)


def mk(
    id_,
    *,
    primary_id=None,
    relevance=0.5,
    citation_count=10,
    age_years=0.0,
    authority=0.5,
    lane="scholarly",
    reason=InclusionReason.EVIDENCE,
    cells=(),
):
    return Source(
        id=id_,
        primary_id=primary_id if primary_id is not None else id_,
        relevance=relevance,
        citation_count=citation_count,
        age_years=age_years,
        authority=authority,
        lane=lane,
        inclusion_reason=reason,
        cells=frozenset(cells),
    )


# --- ranking (spec crit 2) ---------------------------------------------------

def test_rank_coverage_beats_authority():
    m = CoverageMatrix(required_cells=[C_A, C_B])
    # high-authority source on an ALREADY-FILLED cell vs zero-authority on OPEN.
    m.admit(mk("seed", authority=0.0, cells=[C_A]))  # fills C_A
    prestige = mk("prestige", authority=1.0, relevance=1.0, cells=[C_A])  # filled cell
    scrappy = mk("scrappy", authority=0.0, relevance=0.1, cells=[C_B])  # open cell
    ranked = m.rank([prestige, scrappy])
    assert ranked[0].id == "scrappy"  # unique coverage wins despite zero authority
    assert ranked[1].id == "prestige"


def test_rank_ties_break_by_relevance_then_authority():
    m = CoverageMatrix(required_cells=[C_A])  # C_A open; both fill it → tie on coverage
    hi_rel = mk("hi_rel", relevance=0.9, authority=0.1, cells=[C_A])
    lo_rel = mk("lo_rel", relevance=0.2, authority=0.9, cells=[C_A])
    assert [s.id for s in m.rank([lo_rel, hi_rel])] == ["hi_rel", "lo_rel"]
    # equal coverage AND relevance → authority breaks it
    a = mk("a", relevance=0.5, authority=0.9, cells=[C_A])
    b = mk("b", relevance=0.5, authority=0.1, cells=[C_A])
    assert [s.id for s in m.rank([b, a])] == ["a", "b"]


def test_rank_full_tie_preserves_input_order():
    m = CoverageMatrix(required_cells=[C_A])
    x = mk("x", relevance=0.5, authority=0.5, cells=[C_A])
    y = mk("y", relevance=0.5, authority=0.5, cells=[C_A])
    assert [s.id for s in m.rank([x, y])] == ["x", "y"]
    assert [s.id for s in m.rank([y, x])] == ["y", "x"]


# --- age-adjusted weight (spec crit 3) ---------------------------------------

def test_age_weight_canonical_up_evidence_down():
    canonical = SliceWeighting(half_life_years=10.0, older_is_better=True)
    evidence = SliceWeighting(half_life_years=10.0, older_is_better=False)
    # at age == half_life: canonical doubles, evidence halves.
    assert canonical.age_adjusted_weight(100, 10.0) == pytest.approx(200.0)
    assert evidence.age_adjusted_weight(100, 10.0) == pytest.approx(50.0)
    # at age 0 both are identity.
    assert canonical.age_adjusted_weight(100, 0.0) == pytest.approx(100.0)
    assert evidence.age_adjusted_weight(100, 0.0) == pytest.approx(100.0)


def test_slice_weighting_rejects_nonpositive_half_life():
    with pytest.raises(ValueError):
        SliceWeighting(half_life_years=0.0, older_is_better=True)


# --- typed inclusion reason (spec crit 4) ------------------------------------

def test_admit_rejects_missing_primary_id():
    with pytest.raises(ValueError):
        Source(
            id="x", primary_id="", relevance=0.5, citation_count=1, age_years=0.0,
            authority=0.5, lane="scholarly", inclusion_reason=InclusionReason.EVIDENCE,
        )


def test_admit_rejects_bad_reason():
    with pytest.raises(ValueError):
        Source(
            id="x", primary_id="x", relevance=0.5, citation_count=1, age_years=0.0,
            authority=0.5, lane="scholarly", inclusion_reason="not-a-reason",
        )


def test_typed_reason_grep():
    # reasons are greppable labels usable to audit "no DISSENT for cell X".
    m = CoverageMatrix(required_cells=[C_A])
    m.admit(mk("d", reason=InclusionReason.DISSENT, cells=[C_A]))
    reasons_on_a = {s.inclusion_reason for s in m._sources if C_A in s.cells}
    assert InclusionReason.DISSENT in reasons_on_a


# --- primary-space dedup (spec crit 5) ---------------------------------------

def test_primary_dedup():
    m = CoverageMatrix(required_cells=[C_A])
    # two docs derive from the same primary; a third is its own primary.
    m.admit(mk("doc1", primary_id="P", cells=[C_A]))
    m.admit(mk("doc2", primary_id="P", cells=[C_A]))
    m.admit(mk("doc3", primary_id="doc3", cells=[C_A]))
    assert m.primaries() == ["P", "doc3"]  # 2 distinct primaries, sorted
    # C_A rests on TWO distinct primaries → not single-primary.
    assert C_A not in m.single_primary_cells()


def test_single_primary_cell_flagged():
    m = CoverageMatrix(required_cells=[C_A, C_B])
    m.admit(mk("d1", primary_id="P", cells=[C_A]))
    m.admit(mk("d2", primary_id="P", cells=[C_A]))  # C_A: 1 primary
    m.admit(mk("d3", primary_id="Q", cells=[C_B]))  # C_B: 1 primary
    assert m.single_primary_cells() == [C_A, C_B]  # both single-primary, sorted


# --- two-condition stop over a growing map (spec crit 6) ---------------------

def test_two_condition_stop():
    m = CoverageMatrix(required_cells=[C_A], required_lanes=(), k_dry=2)
    m.admit(mk("s", cells=[C_A]))  # nominates a genuinely-new... no, C_A is required
    # first round covered the cell but nominated no NEW cell → dry.
    m.end_round()  # dry_rounds = 1
    assert m.status() == "OPEN"  # covered but only 1 dry round
    m.end_round()  # dry_rounds = 2
    assert m.status() == "SATURATED"  # covered AND 2 dry rounds


def test_growing_map_blocks_saturation():
    m = CoverageMatrix(required_cells=[C_A], required_lanes=(), k_dry=2)
    m.admit(mk("s", cells=[C_A]))
    m.end_round()
    m.end_round()
    assert m.status() == "SATURATED"
    # a late nomination of a NEW cell resets dryness (C_C is non-required, so
    # this exercises the dry-round-reset half of the stop, not the empty-cell half).
    m.nominate_cell(C_C)
    m.end_round()  # round had a new cell → dry_rounds reset to 0
    assert m.status() == "OPEN"


# --- required-lane gating and precedence (spec crit 7) -----------------------

def test_required_lane_gate():
    m = CoverageMatrix(required_cells=[C_A], required_lanes=("citation_graph",), k_dry=1)
    m.admit(mk("s", lane="scholarly", cells=[C_A]))  # covers C_A, but not the required lane
    m.end_round()
    assert m.status() == "PARTIAL"  # required lane never ran
    m.mark_lane_run("citation_graph")
    assert m.status() == "SATURATED"


def test_lane_gate_precedence():
    # required lane down AND empty cells AND dry rounds NOT met → still PARTIAL,
    # proving the lane gate is evaluated BEFORE the saturation test.
    m = CoverageMatrix(required_cells=[C_A], required_lanes=("citation_graph",), k_dry=2)
    assert m.empty_cells() == [C_A]  # uncovered
    assert m.status() == "PARTIAL"  # not OPEN — lane gate fires first


# --- enumerated edge cases / invariants --------------------------------------

def test_duplicate_nomination_idempotent():
    m = CoverageMatrix(required_cells=(), required_lanes=(), k_dry=1)
    assert m.nominate_cell(C_A) is True   # genuinely new
    assert m.nominate_cell(C_A) is False  # duplicate → no-op
    # a duplicate-only round is still dry.
    m.end_round()  # first round HAD a new cell (the True above) → resets to 0
    m.nominate_cell(C_A)  # duplicate only
    m.end_round()  # dry → dry_rounds = 1
    assert m.status() == "SATURATED"  # no required cells, lane gate open, 1 dry round


def test_zero_cell_source_admitted():
    m = CoverageMatrix(required_cells=[C_A], required_lanes=())
    z = mk("z", cells=[])  # covers no matrix cell
    m.admit(z)  # admitted without error
    assert m.score(z) == (0, z.relevance, z.authority)  # scores zero coverage
    assert "z" in m.primaries()  # still participates in the claim space


def test_empty_matrix_open():
    # no required cells, no required lane → OPEN until k_dry rounds elapse.
    m = CoverageMatrix(required_cells=(), required_lanes=(), k_dry=2)
    assert m.status() == "OPEN"
    # with a required lane unrun, an empty matrix is PARTIAL first.
    m2 = CoverageMatrix(required_cells=(), required_lanes=("citation_graph",), k_dry=2)
    assert m2.status() == "PARTIAL"


def test_reads_deterministic_order():
    # admit in a scrambled order; reads come back stably sorted regardless.
    m = CoverageMatrix(required_cells=[C_C, C_A, C_B], required_lanes=())
    m.admit(mk("s3", primary_id="P3", cells=[C_C]))
    m.admit(mk("s1", primary_id="P1", cells=[C_A]))
    m.admit(mk("s2", primary_id="P2", cells=[C_B]))
    expected_cells = sorted([C_A, C_B, C_C], key=lambda c: c.sort_key)
    assert m.single_primary_cells() == expected_cells
    assert m.primaries() == ["P1", "P2", "P3"]
    # empty_cells is sorted too (none open here since all covered).
    assert m.empty_cells() == []
