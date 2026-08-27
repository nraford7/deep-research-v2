# Plan: source-selection-protocol (coverage-matrix core)

Spec: docs/superpowers/specs/2026-08-27-source-selection-protocol-design.md
Rung: light · Lens: "Additive only — no existing file is modified"
Deliverable: `scripts/coverage_matrix.py` (NEW) + `tests/test_coverage_matrix.py` (NEW). Additive only.

## Chunk 1 — `scripts/coverage_matrix.py` (pure, offline core)

Single new module. No imports of existing repo code, no network, no clocks, no
randomness. Stdlib only (`dataclasses`, `enum`, `math`, `typing`).

- [ ] Module docstring: what the matrix is and the "one object, three reads"
  contract (ranking / saturation / audit).
- [ ] `class Lens(str, Enum)` — a small open set of review lenses
  (`METHOD`, `PERSPECTIVE`, `PERIOD`, `TRADITION`, `SUBCLAIM`). String-valued for
  greppability.
- [ ] `class InclusionReason(str, Enum)` — `ANCHOR, EVIDENCE, UNIQUE_COVERAGE,
  DISSENT, UPDATE`.
- [ ] `@dataclass(frozen=True) class Cell` — `subtopic: str`, `lens: Lens`.
  Hashable; sort key = `(subtopic, lens.value)`.
- [ ] `@dataclass class Source` — `id, primary_id (required), relevance: float,
  citation_count: int, age_years: float, authority: float, lane: str,
  inclusion_reason: InclusionReason, cells: frozenset[Cell]`. `__post_init__`
  raises `ValueError` if `primary_id` is None/empty or `inclusion_reason` is not
  an `InclusionReason`.
- [ ] `@dataclass(frozen=True) class SliceWeighting` — `half_life_years: float`,
  `older_is_better: bool`; method `age_adjusted_weight(citation_count, age_years)`
  = `citation_count * exp((+1 if older_is_better else -1) * ln2 * age_years /
  half_life_years)`. Guard `half_life_years > 0`.
- [ ] `class CoverageMatrix`:
  - `__init__(required_cells=(), required_lanes=("citation_graph",), k_dry=2)`.
  - internal: `_required: set[Cell]`, `_nominated: set[Cell]`, `_sources: list`,
    `_lanes_run: set[str]`, `_dry_rounds: int`, `_round_had_new_cell: bool`.
  - `nominate_cell(cell)` — idempotent: only a genuinely new cell adds to
    `_nominated` and sets `_round_had_new_cell = True`.
  - `require_cells(cells)` — add to `_required` (and `_nominated`).
  - `admit(source)` — validates (delegates to Source), records source, marks
    `_lanes_run.add(source.lane)`; nominates any of the source's cells not yet
    known (growing map).
  - `mark_lane_run(lane)` — for lanes that ran but admitted nothing.
  - `score(candidate) -> tuple` — `(open_cells_filled, relevance, authority)`
    where `open_cells_filled = len(candidate.cells & self.open_cells_set())`.
  - `rank(candidates) -> list` — sort best-first via a **negated key**
    `sorted(candidates, key=lambda c: tuple(-x for x in score(c)))` (NOT
    `reverse=True`, which would reverse input order on genuine full ties). Stable
    sort → full ties preserve original input order. Return a new list.
  - `end_round()` — `_dry_rounds = 0 if _round_had_new_cell else _dry_rounds+1`;
    reset `_round_had_new_cell = False`.
  - `open_cells_set() -> set[Cell]` — required cells with zero admitted sources.
  - `empty_cells() -> list[Cell]` — sorted `open_cells_set()`.
  - `single_primary_cells() -> list[Cell]` — required∪nominated cells whose
    admitted sources resolve to exactly one distinct `primary_id`; sorted.
  - `primaries() -> list[str]` — sorted distinct `primary_id`.
  - `status() -> str` — lane gate FIRST: if any `required_lane` not in
    `_lanes_run` → `"PARTIAL"`; else `"SATURATED"` iff `_dry_rounds >= k_dry`
    AND not `empty_cells()`; else `"OPEN"`.

Verification:
- [ ] `cd ~/Projects/deeper-research && python3 -c "from scripts import coverage_matrix"` imports clean.

Acceptance: module imports; every spec success-criterion 1–8 has a corresponding
method; no stdlib-external imports; no network/clock/random.

## Chunk 2 — `tests/test_coverage_matrix.py` (offline pytest)

Match repo test convention: header docstring noting OFFLINE, `from scripts import
coverage_matrix`, plus the `_ROOT` sys.path stanza (from `scripts/evidence_gate.py`)
so it also runs under bare `python3 -m pytest`.

Tests (one per behavior; names in parens):
- [ ] `test_rank_coverage_beats_authority` — zero-authority source filling an
  open cell outranks a max-authority source on a filled cell.
- [ ] `test_rank_ties_break_by_relevance_then_authority`.
- [ ] `test_age_weight_canonical_up_evidence_down` — numeric assert on the
  formula at `age_years == half_life_years` (canonical → 2×, evidence → 0.5×).
- [ ] `test_admit_rejects_missing_primary_id` and
  `test_admit_rejects_bad_reason` — `ValueError`.
- [ ] `test_typed_reason_grep` — `single_primary_cells` / reason labels usable
  for audit.
- [ ] `test_primary_dedup` — three docs, two share a `primary_id` → `primaries()`
  has 2; a cell with those two docs is a single-primary cell.
- [ ] `test_two_condition_stop` — cover all required cells but only 1 dry round →
  `OPEN`; a 2nd dry round → `SATURATED`; nominating a new cell mid-way resets.
- [ ] `test_growing_map_blocks_saturation` — a late nomination flips SATURATED→OPEN.
- [ ] `test_required_lane_gate` — all cells covered + dry rounds met but
  `citation_graph` never ran → `PARTIAL`; after `mark_lane_run` → `SATURATED`.
- [ ] `test_lane_gate_precedence` — required lane down AND `empty_cells()`
  non-empty AND dry rounds NOT met → still `PARTIAL` (proves lane-gate fires
  before the saturation test, not `OPEN`).
- [ ] split edge cases into named tests: `test_duplicate_nomination_idempotent`,
  `test_zero_cell_source_admitted`, `test_empty_matrix_open`,
  `test_reads_deterministic_order`.

Verification:
- [ ] `cd ~/Projects/deeper-research && python3 -m pytest tests/test_coverage_matrix.py -q` → all pass.

Acceptance: all tests pass; each maps to a spec criterion or edge case; no
network/subprocess; runs in <2s.

## Rollback
Delete `scripts/coverage_matrix.py` and `tests/test_coverage_matrix.py` (or
`git checkout main -- .` / drop the branch). Nothing else references either file.
