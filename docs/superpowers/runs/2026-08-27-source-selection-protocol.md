# Run: source-selection-protocol
Instruction: /do-it — turn the refined research source-selection design (coverage-matrix schema, two-condition stop, lane-activation table, per-slice age-weighting, typed inclusion reasons, required-lane gating) into a spec + working core module + tests for the deeper-research pipeline. Scope: Spec + core module (`scripts/coverage_matrix.py`) + tests; integration into scope.py/slice_search.py/coverage_audit.py is SPEC'd not built. Target: ~/Projects/deeper-research, branch feat/source-selection-protocol.
Stage: done
Rung: light (start-floor light: default, BR0 REV0 NOV1 INT0 FC1 = 2)
Lens (mandatory every review pass): "Additive only — no existing file is modified"
Spec: docs/superpowers/specs/2026-08-27-source-selection-protocol-design.md   Plan: (pending)
Agency project: 01a04453-b0bd-79fe-ab5c-4d2010955932 (task 01a04453-cba4-7575-88c2-baa9e94662da eval: approve/92, completed)

## Scorecards
Pass 1 [spec]: 1B/5S/3C/0R · fixed -/- · velocity = (—→9, escalation no) · judge: n/a-light
Pass 1 [plan]: 0B/2S/2C/0R · fixed -/- · velocity = (—→4, escalation no) · judge: n/a-light
Pass 1 [code:coverage_matrix]: 0B/0S/2C/0R · fixed -/- · velocity = (—→2, escalation no) · judge: n/a-light

## Chunks
- [x] spec — review clean (1 pass; 1B/5S/3C all incorporated, none survived)
- [x] plan — review clean (1 pass; 0B/2S/2C folded into build, none survived)
- [x] coverage_matrix module + tests — built via Agency (eval approve/92), Stage-1 clean (0B/0S/2C, both cosmetics fixed). New suite 18/18; full repo suite 430 passed, no regressions.

## Notes
- Repo conventions: pytest, tests add ROOT to sys.path and `from scripts import <mod>`; offline tests (LLM monkeypatched, no network). Module lands in `scripts/`, test in `tests/`.
- Target repo is NOT cwd (cwd = /Users/noahraford/magic, not a git repo); user confirmed build+commit target = ~/Projects/deeper-research on new branch off main. do-it will NOT push to main.
- Commit 152e9dc on branch feat/source-selection-protocol, pushed to origin. PR not opened (per skill).
- docs/ is gitignored (.gitignore:44) but prior do-it artifacts under docs/superpowers/ are tracked → matched convention with `git add -f` for the 3 artifacts.
- Execution used Agency as mandated (project 01a04453…, task eval approve/92). Executor ran the composed task directly (Agency composes prompts, caller executes) — no deviation from policy.
- Light-rung roborev backstop enqueued: job 4943 (agent codex) on 152e9dc — async, handle findings later via /roborev-fix.
- No skip-temptations triggered; all mandatory artifacts written in order.
