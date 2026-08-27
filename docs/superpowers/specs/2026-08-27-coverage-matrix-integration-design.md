# Coverage-Matrix Integration — Design Spec

*deeper-research pipeline · 2026-08-27 · branch `feat/source-selection-protocol`*

## Problem statement

The coverage-matrix core (`scripts/coverage_matrix.py`, merged in the prior run)
is a pure, offline logic object but nothing in the live pipeline builds or reads
it. To be useful it must be assembled from the run's real artifacts — the scoped
domain map (`scope.py --output`) and the retrieved slice rows
(`round1/slice_*.jsonl` written by `slice_search.py`) — and consulted where the
run actually decides "is coverage enough?": the `coverage_audit.py` loop.

Hard constraint (user-chosen approach): **additive, opt-in, default-safe.** The
default pipeline behavior must not change and all 430 existing tests must stay
green. "Complete" means the matrix is fully wired and runnable behind an opt-in
flag — not the forced default.

## Success criteria (measurable)

1. A new module `scripts/coverage_matrix_adapter.py` (offline, no network) that
   turns the pipeline's existing artifacts into a `CoverageMatrix`:
   - `cells_from_scope(payload: dict) -> list[Cell]` — derive `(subtopic, lens)`
     cells from a `scope.py` output payload (`ranked_domains` / `domains` /
     `priority_sources`). Deterministic, documented heuristic.
   - `source_from_row(row: dict, current_year: int) -> Source | None` — map one
     slice JSONL row to a `Source`: `id`/`primary_id` from a normalized `url`
     (rows with no url → `None`, mirroring `slice_search._result_to_item`);
     `lane` from `row["slice"]`; `authority` from `row["tier"]` via a fixed
     tier→float map; `age_years = max(0, current_year - year(published_date))`
     (year parsed as in `_year_or_nd`, which returns `"n.d."` for unparseable/
     None — guard `int()`, fall back to age 0, never crash); `inclusion_reason`
     by EXACT slice-name match — `row["slice"] == "anchor"` → `ANCHOR`,
     `row["slice"].startswith("gap_")` → `UNIQUE_COVERAGE`, else `EVIDENCE`;
     `relevance` a documented heuristic (has full text/highlights → higher);
     `citation_count = 0` (rows carry none — documented degradation);
     `cells` mapped from the row's slice/domain.
   - `build_matrix(payload, rows, current_year, required_lanes=("anchor",),
     k_dry=2) -> CoverageMatrix` — seed required cells from scope, admit every
     mappable row, mark each row's lane run.
   - `matrix_report(matrix) -> dict` — JSON-serializable
     `{status, empty_cells, single_primary_cells, primaries, counts}` for
     `coverage_audit` to write and log.
2. `coverage_audit.py` gains an **opt-in** `--use-matrix` flag (default
   `False`). When set, after the existing gap enumeration it builds the matrix
   from the scope payload + `_iter_slice_rows`, writes
   `round1/coverage_matrix.json`, and prints a one-line status
   (`coverage-matrix: status=<S> empty=<n> single_primary=<n>`). When unset, the
   code path is byte-for-byte the prior behavior.
3. Offline tests: `tests/test_coverage_matrix_adapter.py` (adapter, dict/tmp
   fixtures) and a new `tests/test_coverage_audit_matrix.py` (flag default-off
   leaves behavior unchanged; flag-on writes the report file). No network, no
   real subprocess, no clock (current_year passed in).
4. The full existing suite (430 tests) still passes; no existing test file is
   modified.

Out of scope (explicitly): making the matrix the *default* selection driver;
replacing the LLM gap loop or the slice ranking order; a DISSENT retrieval lane
and query-vocabulary expansion (future slices). Those are the "invasive" path
the user did not choose.

## Proposed approach

- **Only one existing file is edited: `coverage_audit.py`** (the opt-in flag +
  a small `_emit_matrix_report` helper, both no-ops unless `--use-matrix`).
  `scope.py` and `slice_search.py` are **not** edited — the adapter reads their
  existing JSON output and row schema. This departs from the option's
  illustrative preview (which showed hooks in all three) on purpose: consuming
  existing outputs is strictly lower-risk than adding default-off hooks to two
  more live files, and avoids a `scope`↔`adapter` import cycle. All mapping
  logic lives in the adapter, where it is unit-testable with plain dicts.
- The adapter imports only `scripts.coverage_matrix` (the pure core) + stdlib.
- `current_year` is a parameter (caller stamps it) so the adapter stays a pure
  function of its inputs and tests are deterministic.

## Adapter contracts (pinned before build — each becomes a test)

1. **Slice-name matching is exact.** `row["slice"] == "anchor"` → `ANCHOR`;
   `row["slice"].startswith("gap_")` → `UNIQUE_COVERAGE` (gap rows carry
   `gap_r{round}_{slug}`, never a bare slug); everything else → `EVIDENCE`.
2. **`primary_id` reuses `slice_search._norm_key`.** The adapter imports (or
   exactly duplicates) `_norm_key` — including its `doi.org` special-case — so
   its cross-slice URL dedup agrees with the rest of the pipeline. It must NOT
   invent its own normalization.
3. **`tier`→authority map covers all 8 tiers + a default.** `tier_of` can return
   any of `peer_reviewed / institutional / preprint / book / news / blog / wiki
   / unknown`; the map has an entry for each plus a fallback for a missing/None
   `tier` (→ lowest).
4. **Missing/degraded fields never crash.** No `url` → `source_from_row` returns
   `None` and `build_matrix` skips it WITHOUT counting it admitted. `"n.d."`/None
   `published_date` → age 0. Missing `tier` → default authority.
5. **The `anchor` lane clears the required-lane gate.** Anchor rows set
   `lane == "anchor"` (exact string) so that admitting them marks the required
   lane run — even when an anchor row maps to no required cell (a row that maps
   to no cell is still admitted; its lane still counts as run). Empty scope
   payload → zero required cells is acceptable; `status()` is then driven by the
   lane gate. `--use-matrix` emit is wrapped `try/except` so a matrix-build error
   can NEVER change `coverage_audit`'s existing return code, and is placed at a
   single deterministic point (once per run, guarded), not inside every round.

## Alternatives considered

- **Add default-off helpers to `scope.py` and `slice_search.py` for locality.**
  Rejected for this run: it touches two more live files for marginal locality
  benefit and risks an import cycle (`scope.py` → adapter → `scope.py`). The
  mapping is just as testable inside the adapter. Can be revisited if a reviewer
  shows the locality materially aids maintenance.
- **Make the matrix the default gap/stop driver.** Rejected: that is the
  invasive approach the user explicitly did not choose; it would rewrite
  existing `coverage_audit` tests and change default behavior.

## Blast radius / rollback

- New: `scripts/coverage_matrix_adapter.py`, `tests/test_coverage_matrix_adapter.py`,
  `tests/test_coverage_audit_matrix.py`.
- Edited: `scripts/coverage_audit.py` — additive only (one new CLI flag + one
  helper + one guarded call). Default path (flag absent) is unchanged, so
  `tests/test_coverage_audit.py` and the rest of the 430 stay green.
- Rollback: revert the `coverage_audit.py` hunk and delete the three new files.
  With `--use-matrix` never passed, the feature is entirely dormant.

## Open questions

- The scope→cell and tier→authority mappings are coarse heuristics (scope only
  emits domains, not fine subtopics; rows carry no citation counts). That is
  acceptable for an opt-in report; refining them is future work and does not
  gate this run.
- Which lane should be `required` by default — `anchor` (the academic anchor
  slice) is the natural analog of the citation-graph lane. Defaulting to
  `("anchor",)`; configurable via the adapter signature.
