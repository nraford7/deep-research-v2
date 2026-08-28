# Plan: coverage-matrix-integration (opt-in adapter)

Spec: docs/superpowers/specs/2026-08-27-coverage-matrix-integration-design.md
Rung: medium · Lens: "all 430 tests green" · "default path byte-for-byte unchanged" · "not the forced default"
Deliverable: NEW scripts/coverage_matrix_adapter.py + tests; EDIT scripts/coverage_audit.py (opt-in `--use-matrix`) + new test. Additive, default-off.

## Chunk 1 — `scripts/coverage_matrix_adapter.py` + `tests/test_coverage_matrix_adapter.py`

New module. Imports ONLY `scripts.coverage_matrix` + stdlib. **DUPLICATE
`_norm_key` verbatim** (slice_search.py:147-166, incl. the doi.org special case)
with a "mirrors slice_search._norm_key" comment — do NOT import slice_search
(it pulls `requests`/`config`/`ledger` at module level; importing it would break
the "stdlib-only, offline" contract of success-criterion #1 even though it's
side-effect-free). Also add `_url_host(url)` via `urllib.parse.urlsplit`.

Functions:
- [ ] `TIER_AUTHORITY: dict` — all 8 tiers → float in [0,1]
  (peer_reviewed 1.0, book .8, institutional .7, preprint .6, news .4, wiki .3,
  blog .2, unknown .1) + `.get(tier, 0.1)` default (contract #3).
- [ ] `_year(published_date) -> int | None` — first 4 chars if digits, else None
  (mirrors `_year_or_nd` but returns int|None, never `"n.d."`) (contract #4).
- [ ] `cells_from_scope(payload) -> list[Cell]` — **v1 cells are HOST-MATCHABLE:**
  build one `Cell(subtopic=host, lens=Lens.TRADITION)` per entry in scope
  `domains` (hostnames like "rand.org"), lowercased/deduped/order-stable. These
  are populatable because a row's url-host can equal them. `ranked_domains`
  (topic names like "technology") are NOT seeded as required cells in v1 — no
  per-row topic signal exists offline to populate them, so seeding them would
  guarantee permanently-empty cells and mislead a reader. Empty/missing
  `domains` → `[]` (lane-gate-only report; contract #5). This v1 scope is the
  reviewer's finding #5 resolution and is documented in `coverage_note` below.
- [ ] `source_from_row(row, current_year, scope_cells=()) -> Source | None` —
  None if no `url`; `primary_id = _norm_key(url)`, `id = url`;
  `lane = row.get("slice","unknown")`;
  `authority = TIER_AUTHORITY.get(row.get("tier"), 0.1)`;
  `age_years = max(0, current_year - y)` when `current_year` is not None and
  `y = _year(...)` parses, else `0` (contract #4 — never crash);
  `inclusion_reason` per contract #1; `relevance = 0.7 if row.get("text_path")
  or row.get("highlights") else 0.4`; `citation_count = 0`;
  `cells = frozenset(c for c in scope_cells if c.subtopic == _url_host(url))`
  (real host match; may be empty — the source is still admitted so its lane
  counts as run).
- [ ] `build_matrix(payload, rows, current_year=None,
  required_lanes=("anchor",), k_dry=2) -> CoverageMatrix` — compute
  `scope_cells = cells_from_scope(payload)`; `require_cells(scope_cells)`;
  for each row: `s = source_from_row(row, current_year, scope_cells)`; skip if
  None; else `m.admit(s)`. Return matrix. (Anchor rows → lane "anchor" clears
  the gate; `current_year=None` → all ages 0, tolerated.)
- [ ] `matrix_report(matrix) -> dict` — `{status, empty_cells: [[subtopic,lens]],
  single_primary_cells: [...], primaries: int, n_required: int, n_sources: int,
  coverage_note: str}`, all JSON-serializable & deterministically ordered. The
  `coverage_note` states the v1 contract: "cells seeded from scope hostname
  `domains` and matched by row url-host; topic-domain coverage is future work —
  OPEN cells here mean an unmatched scope hostname, not necessarily thin
  evidence."

Tests (offline, dict fixtures; `from scripts import coverage_matrix_adapter`):
- [ ] `test_cells_from_scope` — cells from `domains` hostnames (lens TRADITION),
  deduped/order-stable; `ranked_domains` NOT seeded; empty/missing → [].
- [ ] `test_source_from_row_fields` — lane/authority/age/reason mapping on a
  normal peer_reviewed row.
- [ ] `test_host_match_populates_cell` — a row whose url-host == a scope
  `domains` hostname covers that cell → `empty_cells` shrinks; a non-matching
  host leaves the cell empty (proves cells actually populate — reviewer #5).
- [ ] `test_row_no_url_returns_none`, `test_missing_tier_defaults`,
  `test_nd_date_age_zero`, `test_current_year_none_age_zero` (contract #4).
- [ ] `test_inclusion_reason_matching` — "anchor"→ANCHOR, "gap_r1_x"→UNIQUE_COVERAGE,
  "web"→EVIDENCE (contract #1).
- [ ] `test_primary_dedup_uses_norm_key` — two rows, same url up to _norm_key →
  one primary (contract #2).
- [ ] `test_anchor_lane_clears_gate` — build_matrix with an anchor row →
  status not PARTIAL on lane grounds; without → PARTIAL (contract #5).
- [ ] `test_build_matrix_skips_nones` — a urlless row is not admitted.
- [ ] `test_matrix_report_serializable` — `json.dumps(matrix_report(m))` works;
  keys present (incl. `coverage_note`); ordering stable.

Verification:
- [ ] `python3 -m pytest tests/test_coverage_matrix_adapter.py -q` all pass.

Acceptance: module offline & stdlib+core only; every contract 1-5 has a test.

## Chunk 2 — `scripts/coverage_audit.py` opt-in `--use-matrix` + `tests/test_coverage_audit_matrix.py`

Edit coverage_audit.py additively (default-off):
- [ ] argparse: `ap.add_argument("--use-matrix", action="store_true")` (default
  False) + `ap.add_argument("--current-year", type=int, default=None)` near the
  other flags (~line 524). **`--current-year` is the honest deterministic age
  source** — NO run field carries a year (reviewer #3); absent → adapter uses
  age 0 for all rows (tolerated). Never `datetime.now()` (module stays clock-free).
- [ ] new helper `_safe_emit_matrix(run_dir, round1_dir, current_year)`: reads
  `run_dir/scope.json` best-effort (missing/malformed → `{}`), builds the matrix
  via the adapter from `_iter_slice_rows(round1_dir)`, writes
  `round1/coverage_matrix.json`, prints one line
  `coverage-matrix: status=<S> empty=<n> single_primary=<n>`. The WHOLE body is
  wrapped `try/except Exception as e: print("coverage-matrix: skipped (...)",
  file=sys.stderr)` — it can raise nothing and returns nothing, so it can never
  change a return code (reviewer #2/contract #5). Adapter imported lazily INSIDE
  the helper (`from scripts import coverage_matrix_adapter`) so module import is
  unchanged for existing tests.
- [ ] call site: guarded emit at the **3 SUCCESS returns only** (coverage_audit.py
  ~562 no-gaps, ~640 gaps-filled, ~643 max-rounds) — insert
  `if args.use_matrix: _safe_emit_matrix(args.run_dir, round1_dir, args.current_year)`
  immediately before each `return 0`. Only one success return fires per run, so
  it emits exactly once; the FAILURE/early-return paths (610/616/635/651) are
  left untouched, so a run that "could not run" writes no report (reviewer #2
  nuance: emit on success only, never in a blanket finally). Flag-off → these
  guards are dead code, default path byte-for-byte unchanged.

Test (new file, does NOT touch test_coverage_audit.py). Reuse test_coverage_audit.py's
stub pattern (`_patch_provider` at lines 37-43, `monkeypatch.setattr(llm,
"call_model", …)`, and the `_fetch_gap`/`_run_evidence_gate` monkeypatches). The
fixture writes `tmp_path/"scope.json"` (`{"ranked_domains":[...],
"domains":["rand.org","brookings.edu"]}`) + a couple of `tmp_path/"round1"/slice_*.jsonl`
rows (one anchor row, one web row whose host is rand.org):
- [ ] `test_use_matrix_writes_report` — `main([...,"--use-matrix","--current-year","2026"])`
  → `round1/coverage_matrix.json` exists, parses, has the expected keys, and the
  audit's return code is the same as the flag-off run.
- [ ] `test_default_off_no_report` — same run WITHOUT the flag → no
  `coverage_matrix.json` written, behavior identical.
- [ ] `test_matrix_build_error_does_not_change_rc` — monkeypatch
  `coverage_matrix_adapter.build_matrix` to raise; with `--use-matrix` the audit
  still returns its normal code and prints the "skipped" line.

Verification:
- [ ] `python3 -m pytest tests/test_coverage_audit_matrix.py -q` pass.
- [ ] `python3 -m pytest -q` → full suite still green (430 + new).

Acceptance: flag default-off path byte-for-byte unchanged; existing
test_coverage_audit.py untouched and green; matrix path writes the report.

## Rollback
Revert the coverage_audit.py hunk (flag + helper + guarded call) and delete
scripts/coverage_matrix_adapter.py + the two new test files. With `--use-matrix`
never passed, the feature is dormant even before rollback.
