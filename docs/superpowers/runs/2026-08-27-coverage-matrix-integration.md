# Run: coverage-matrix-integration
Instruction: /do-it "for the next steps to completion" — wire the coverage-matrix core (scripts/coverage_matrix.py) into the live scope → slice_search → coverage_audit chain. User-chosen approach: ADDITIVE ADAPTER, opt-in, default-safe — expose matrix-driven selection without changing default pipeline behavior; all 430 existing tests stay green. "Complete" = fully wired and runnable behind an opt-in flag, not the forced default. Target: ~/Projects/deeper-research, branch feat/source-selection-protocol (continues prior run).
Stage: committing
Rung: medium (start-floor medium: constraint-lens, BR1 REV1 NOV1 INT0 FC1 = 4)
Lens (mandatory every review pass): "ALL 430 existing tests stay green" · "default path (flag unset) byte-for-byte unchanged" · "NOT the forced default"
Fresheyes note: fresheyes.sh not installed in current plugins tree (only backups). Watchdog-Protocol fallback: independent MODEL kept for code review via roborev/codex (where independence matters most per rung rationale); spec/plan use independent-CONTEXT general-purpose subagents. Surfaced per protocol.
Spec: docs/superpowers/specs/2026-08-27-coverage-matrix-integration-design.md   Plan: (pending)
Agency project: —

## Scorecards
Pass 1 [spec]: 0B/5S/1C/0R · fixed -/- · velocity = (—→6, escalation no) · judge: n/a-medium
Pass 1 [plan]: 0B/4S/2C/0R · fixed -/- · velocity = (—→6, escalation no) · judge: n/a-medium
Pass 1 [code:integration]: 0B/1S/1C/0R · fixed -/- · velocity = (—→2, escalation no) · judge: n/a-medium

## Chunks
- [x] spec — independent review clean (0B/5S/1C; all 5 pinned as adapter contracts, none survived)
- [x] plan — independent review clean (0B/4S/2C; folded: dup _norm_key, host-match cells v1, --current-year arg, 3-success-return emit; none survived)
- [x] adapter module + tests — Agency eval approve/91; 12 offline tests
- [x] coverage_audit --use-matrix wiring + test — Agency eval approve/92; Step-4 Stage1 clean, Stage2 (adversarial) found 1 SUBSTANTIVE (broken-stderr in except handler could escape & change rc) → FIXED + regression test. Full suite 446 passed. Existing test_coverage_audit.py untouched (17/17).
- [ ] adapter module + helpers + tests — pending
- [ ] coverage_audit --use-matrix wiring + test — pending

## Notes
- Continues branch feat/source-selection-protocol from the prior run (coverage_matrix.py core already merged there).
- Constraint lens (expected): "existing default path untouched; all 430 tests stay green." Every edit is additive + default-off.
- Integration seams (from recon): slice rows in round1/slice_*.jsonl already carry {url, slice, tier, published_date, authority_tag}; scope.py emits subtopics/priorities; coverage_audit.py orchestrates the gap loop (the runnable wire-in point).
- docs/ is gitignored but docs/superpowers/ is force-added by convention (prior run).
