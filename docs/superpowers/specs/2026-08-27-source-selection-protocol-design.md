# Source-Selection Protocol — Design Spec

*deeper-research pipeline · 2026-08-27 · branch `feat/source-selection-protocol`*

## Problem statement

The pipeline decides *which sources enter the corpus* through a chain of scripts
(`scope.py` → `slice_search.py` → `coverage_audit.py` → `citation_chase.py`).
Today that decision has three structural weaknesses:

1. **Saturation is measured against a fixed map.** If the must-cover map (from
   `scope.py` / the model-authored Research Bible) is blind to a territory, the
   retrieval slices converge and the audit declares "enough" while a whole region
   is missing. Saturation measures *convergence*, not *completeness*.
2. **Ranking has no explicit contract.** Sources are admitted by the evidence
   gate but there is no recorded, testable rule for *why* a source is in, and no
   guard against source authority (incumbency) silently outranking unique
   coverage — which is exactly the value a research task exists to find.
3. **Saturation counts documents, not findings.** Ten papers reporting one result
   (or all deriving from one primary) count as ten, inflating apparent coverage;
   and a down lane (e.g. the citation-graph index rate-limiting out, a known
   failure mode on this machine) silently lowers the completeness bar instead of
   flagging the run partial.

There is no single object that ties ranking, the stop decision, and the coverage
audit together, so each is implemented and reasoned about separately.

## Success criteria (measurable)

A new, **offline, pure-logic** module `scripts/coverage_matrix.py` that:

1. Models a **coverage matrix** whose cells are `(subtopic, lens)` pairs, and
   which is the single object read by ranking, saturation, and audit.
2. **Ranks** a candidate source **lexicographically** on the key
   `(open_cells_filled, relevance, authority)`, sorted **descending on each
   component** (a *stable* sort), best-first. `open_cells_filled` is counted
   against the matrix's *current* open-cell set at scoring time; ties on that
   count break by relevance, then authority — regardless of *which* cells are
   filled. Authority is thus a tiebreaker/floor, never a primary driver.
   Required test: a high-authority source on an already-filled cell must lose to
   a zero-authority source that fills an open cell.
3. Applies **per-slice age-adjusted citation weight** by the exact formula
   `weight = citation_count * exp(sign * ln2 * age_years / half_life_years)`,
   where `sign = +1` when `older_is_better` (canonical slice: weight doubles
   every `half_life_years` of age) and `sign = -1` otherwise (evidence slice:
   weight halves every `half_life_years`). Pure function of its inputs; unit-
   tested against fixed numeric triples.
4. Requires a **typed inclusion reason** on every admitted source
   (`ANCHOR | EVIDENCE | UNIQUE_COVERAGE | DISSENT | UPDATE`); rejects admission
   without one. Enables greppable audits ("no `DISSENT` source for cell X"). The
   reason is a *label only* — `admit` does NOT validate reason-against-coverage
   (a `UNIQUE_COVERAGE` source is not required to actually fill an open cell);
   this is deliberate, so no enforcement is expected.
5. Deduplicates to the **claim/finding space by primary source** (`primary_id`,
   a **required non-null** field): saturation and single-source audits count
   distinct primaries, not documents. A source that is itself primary sets
   `primary_id == id`.
6. Computes a **two-condition stop** via an explicit round state machine:
   - a round is **dry** iff `nominate_cell` was called **zero** times during it;
   - `end_round()` **increments** the dry-round counter if the round was dry,
     else **resets it to 0**;
   - `nominate_cell` for a cell **already present is idempotent** (no re-count,
     and it does NOT mark the round non-dry — only a genuinely new cell does);
   - `status()` returns `SATURATED` iff `dry_rounds >= K` (default `K=2`) AND
     `empty_cells()` is empty; otherwise `OPEN`.
   A **growing map**: any source may nominate a new cell (even one outside the
   required set); admitting a new cell mid-run is what keeps saturation honest.
7. **Gates saturation on required-lane availability**, evaluated **before** the
   saturation test: if a lane marked required (default: `citation_graph`) never
   ran, `status()` returns `PARTIAL`, never `SATURATED`, regardless of cell
   coverage. Precedence: lane-gate → (saturation test → `SATURATED`/`OPEN`).
8. **Audit reads**, all returning lists in a **deterministic stable order**
   (sorted by a fixed key, never raw set/dict iteration order): `empty_cells()`
   (required cells with zero admitted sources) and `single_primary_cells()`
   (cells whose admitted sources resolve to exactly one distinct `primary_id`).
9. Ships with `tests/test_coverage_matrix.py` — fully offline (no network, no
   subprocess), all passing.

Out of scope for this run (SPEC'd, built later): wiring the matrix into
`scope.py` (emit cells), `slice_search.py` (attach lane + inclusion reason +
per-slice age weight), and `coverage_audit.py` (drive gap-fills from empty
cells). Section "Integration (deferred)" defines those seams.

## Proposed approach

One module, one dataclass-driven core, no I/O:

- `Lens`, `InclusionReason` — enums.
- `Cell(subtopic, lens)` — hashable identity.
- `Source(id, primary_id, relevance, citation_count, age_years, authority,
  lane, inclusion_reason, cells)` — a candidate/admitted source.
- `SliceWeighting(half_life_years, older_is_better)` — per-slice age policy;
  `age_adjusted_weight(source)` returns citation weight scaled by an exponential
  age factor whose direction flips on `older_is_better`.
- `CoverageMatrix`:
  - `require_cells(...)`, `nominate_cell(...)` (growing map; tracks dry rounds),
  - `admit(source)` (enforces typed reason; raises on missing),
  - `score(candidate)` → a lexicographic sort key `(open_cells_filled,
    relevance, authority)` for ranking,
  - `rank(candidates)` → sorted best-first,
  - `end_round()` (advances the dry-round counter; nomination during the round
    resets it),
  - `status()` → `OPEN | PARTIAL | SATURATED` per criteria 6–7,
  - `empty_cells()`, `single_primary_cells()` → audit reads,
  - `primaries()` → distinct `primary_id` set (claim-space view).

Determinism: no clocks, no randomness, no network — age is passed in as
`age_years` (caller stamps it), so tests are reproducible and the module is a
pure function of its inputs.

## Edge cases / invariants (each becomes a named test)

- **Duplicate cell nomination** — idempotent: re-nominating an existing cell
  neither adds a cell nor makes the round non-dry.
- **Zero-cell source** — a source covering no matrix cell is **admitted**
  (e.g. an `EVIDENCE`/`DISSENT` source with no cell yet); it scores
  `open_cells_filled == 0` and participates in `primaries()` but not in cell
  audits.
- **Source covering a non-required cell** — admitted and counted for that cell's
  audit reads; but `SATURATED` depends only on the **required** cells being
  covered. The growing map may promote nominated cells; required-vs-nominated is
  tracked so `empty_cells()` reports required cells only.
- **Empty matrix `status()`** — zero required cells: returns `OPEN` (never
  vacuously `SATURATED`) until `K` dry rounds have elapsed; and `PARTIAL` first
  if a required lane has not run. This trap is tested explicitly.
- **Deterministic reads** — `rank`, `empty_cells`, `single_primary_cells`,
  `primaries` all return stably-ordered results so list-equality tests are not
  order-flaky.

## Alternatives considered

- **Extend `coverage_audit.py` in place instead of a new module.** Rejected:
  that script is an LLM-driven, subprocess-shelling, network-touching loop; the
  ranking/saturation *logic* deserves a pure, offline, unit-testable core that
  the audit script can later call. Mixing them would make the logic untestable
  without mocks and couple it to the LLM loop.
- **Weighted-sum scoring (relevance·w1 + authority·w2 + coverage·w3).** Rejected:
  a sum lets authority outvote unique coverage; the whole point is that a
  low-authority source uniquely filling an open cell beats the Nth prestige
  source on a filled one. Lexicographic ordering encodes that non-negotiably.
- **Single-condition saturation ("no new sources").** Rejected: convergence is
  not completeness; without the growing-map + required-lane conditions a blind
  map or a dead lane reads as "done."

## Blast radius / rollback

- **Additive only.** One new module + one new test file. No existing script,
  config, or test is modified in this run. Nothing imports the new module yet.
- **Rollback:** delete the two files (or revert the branch). Zero effect on the
  live pipeline, which never calls the module until the deferred integration.

## Integration (deferred — spec, not built here)

- `scope.py`: emit the initial `(subtopic, lens)` cell set from the scoped
  domain map; seed `CoverageMatrix.require_cells`.
- `slice_search.py`: tag each retrieved row with `lane`, a typed
  `inclusion_reason`, and the slice's `SliceWeighting`; call `admit`.
- `coverage_audit.py`: replace/augment the LLM "what's missing" prompt input
  with `empty_cells()` + `single_primary_cells()`; feed `--add-slice` fills from
  those; consult `status()` (with required-lane gating) for the stop decision.
- A `DISSENT` lane (disconfirming-evidence slice) and per-lane query-vocabulary
  expansion are specified as future slices feeding `admit`.

## Open questions

- Default required-lane set — `citation_graph` only, or also a `canonical`
  meta-document lane? (Defaulting to `citation_graph`; configurable.)
- Where "authority" and "primary_id" come from at retrieval time (OpenAlex
  fields vs. heuristic) — an integration-time concern, not a blocker for the
  pure core.
