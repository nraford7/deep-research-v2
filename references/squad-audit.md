# Step 1.5 — Coverage audit ("the squad")

> Bundled procedure of the deeper-research skill. This IS Step 1.5 — deeper-research runs it inline through the selected adapter's native subagent mechanism; it is **not** a separate skill and is never run standalone. Read `runtime-adapters.md` first and use one adapter for the whole run. Persona files live in `references/squad-personas/`. Runs automatically after citation-chase and the gate pass.

A viewpoint-diverse coverage audit. The stock `coverage_audit.py` asks ONE model
"what is missing?", so the gap list inherits that one model's blind spots. This
skill keeps that model's ONE genuine strength — a dutiful walk of every technique
the scope names — as a cheap mechanical first pass (Stage A.5), then adds four
isolated reader personas for the depth gaps a checklist can't see, and an
adversarial per-gap verifier so no false gap burns budget. It reuses
deeper-research's own ledger-charged fill machinery unchanged.

**Why both a checklist AND a panel.** Head-to-head on the goal-elicitation corpus
(2026-08-26), the stock single-model pass BEAT the persona panel on plain
scope-checklist coverage: it flagged laddering, GROW, Socratic, and the
Locke/Latham provenance problem — all named in scope — while the personas skipped
those "boring" items to chase their signature gaps, and the cap dropped one real
one. The panel in turn beat the script on depth (missing base rates, comparative
effect sizes, the philosophical foundation) and killed a false positive the
script would have spent money on. The two are COMPLEMENTARY: the checklist pass is
mechanical breadth, the panel is judgment-laden depth. This skill runs both.

**Recipe** (agent-studio): Artifact/skill review row. Vary by review dimension,
4 generators + 1 verifier, parallel isolated, combine = dedup + severity-rank.
This REPLACES Step 1.5 of the deeper-research pipeline; do not also run
`coverage_audit.py` in the same round. Everything downstream (Round 2 onward)
is unchanged.

## Preconditions (hard)

1. A deeper-research run dir exists (`research/[slug]/`) with `round1/` populated.
2. `evidence_gate.py` has exited `0`. Never audit a corpus the gate rejects.
3. `citation_chase.py` has returned `0` (run the squad after the graph fill, as
   Step 1.5 normally runs, so personas see the enlarged corpus).
4. `EXA_API_KEY` set (fills need it; the panel itself does not).

## Stage A: Corpus digest (orchestrator, no subagents)

Build ONE shared digest all five subagents receive, from:
- `research/[slug]/scope.json` (topic, scope, sub-questions),
- every `round1/brief_*.md` (titles, urls, dates, one-liners),
- `round1/evidence_manifest.json` (slice names + counts),
- the file LIST of `round1/sources/` (names only, not contents).

Cap the digest at ~25k words; if the briefs exceed it, keep titles + urls +
dates and drop brief prose, never drop whole slices. The digest must state the
run's scope verbatim: every persona bounds its queries to it.

## Stage A.5: Scope-checklist pass (orchestrator or ONE single model, mechanical)

This is the cheap breadth pass — the stock script's one real strength, kept. It is
MECHANICAL, not a panel: no personas, no debate. It exists because specialist
personas reliably skip the "boring" scope-named items to chase their signature
gaps, and a single dutiful scan catches them.

1. **List every named item in scope.** Read `scope.json` and extract every
   technique, method, framework, theory, or topic the scope names EXPLICITLY
   (e.g. for goal elicitation: motivational interviewing, Socratic questioning,
   clean language, laddering / means-end chains, GROW, requirements elicitation,
   "why stated goals diverge"). This is a literal extraction from the scope text,
   not a creative act.
2. **Check each against the corpus.** For each named item, scan the briefs, the
   `sources/` file list, and (where cheap) grep the source texts for substantive
   coverage. Classify each: COVERED / ABSENT / THIN-OR-BLOG-ONLY (present only in
   a low-tier blog, not primary/peer-reviewed material).
3. **Emit a checklist gap for every ABSENT or THIN-OR-BLOG-ONLY item**, each with
   one scope-bounded query, in the same gap format the personas use. Default
   severity HIGH when the item is named in scope and wholly ABSENT (a definitional
   coverage failure); MED when THIN-OR-BLOG-ONLY.
4. **Sibling sweep (breadth into unnamed space).** After the named-item walk, also
   enumerate the canonical SIBLING techniques/methods/frameworks in this domain
   that the scope did NOT name but a competent domain expert would expect
   alongside the named ones (e.g. for goal elicitation: appreciative inquiry,
   solution-focused brief therapy, jobs-to-be-done, OKRs). Check each against the
   corpus the same way. Emit a `[sibling]` gap for any that is ABSENT and clearly
   in-domain, default severity MED (a named-in-scope absence outranks an unnamed
   sibling). This is the ONE breadth a checklist alone misses — the item the scope
   forgot to ask for. Keep it disciplined: siblings must be genuinely in-domain,
   not adjacent-field sprawl; when unsure whether a sibling is in-scope, mark it
   LOW and let Popper's verification and the user decide.

These checklist gaps enter Stage C merge alongside the persona gaps, tagged
`[checklist]`, and — unlike persona depth gaps — are **exempt from the Stage-C
cap** (see Stage C). A scope-named absent technique is never dropped for space.

Run this pass as a single model call (orchestrator itself, or one non-persona
subagent). If the scope names nothing enumerable (rare), record "no discrete
scope-named items" and skip to Stage B0.

## Stage B0: Meet the squad (cast card, skippable)

Before dispatching anyone, show the user a one-screen cast card: one line per
member with name, stance, what it hunts, and one signature "never":

```
UMBERTO ECO     · academic reviewer  · hunts missing canonical works, uncited schools of thought, cross-disciplinary gaps · never treats recency as a substitute for canonical weight
W. EDWARDS DEMING · practitioner     · hunts missing costs, failure modes, the gap between benchmarks and real-world      · never accepts a published benchmark as real-world evidence
CICERO          · policy reader      · hunts missing statutes, governance structures, jurisdiction splits                 · never cites journalism when the rule's own text is retrievable
NATE SILVER     · quant skeptic      · hunts missing base rates, denominators, selection effects, untracked forecasts     · never accepts a number without its denominator
KARL POPPER     · verifier           · tries to DISPROVE every gap before any money is spent                              · never confirms without attempting a genuine refutation
```

The four staff members are the squad's permanent hires.

**Topic-matched consultant (proactive, recommend before dispatch).** The four
staff seats (academic / practitioner / policy / quant) are structurally blind to
some topic-specific gap classes. When the topic matches one of the triggers
below, PROACTIVELY propose the matching consultant as a recommended 5th generator
in the cast card (the user still accepts or declines) — do not wait for the user
to notice the blind spot:

- **Clinical / therapy / counseling / behavior-change / coaching topics**
  (motivational interviewing, psychotherapy, CBT, health-behavior, goal
  elicitation as clinical practice) → recommend **William R. Miller** (clinician,
  interpretation), on retainer at the selected adapter's roster:
  `~/.agents/agent-roster/william-miller-clinician.md` for Codex or
  `~/.claude/agent-roster/william-miller-clinician.md` for Claude.
  He hunts the in-session process gaps the staff miss: change/sustain-talk
  evidence, fidelity measurement, and evocation-backfire conditions. Verified on
  the goal-elicitation smoke test (2026-08-26): the staff-only panel missed
  exactly this class; a clinician consultant was pulling real weight in the prior
  run.

When a consultant is recommended and accepted, it is a generating lens like any
other: isolated subagent, same preamble, same 5-gap cap, its gaps merged and
verified with the rest. If no trigger matches, skip the recommendation silently.

Then use the selected host's supported interactive question flow: "Want to meet
your squad before they start?" If no such mechanism exists, ask in ordinary chat;
skip this optional question in headless runs.
- **Run as-is (Recommended)** -- dispatch immediately (with any topic-matched
  consultant already recommended above included, unless the user drops them).
- **Meet the team** -- show each full persona file (stance, process,
  constraints, backstory), then re-offer this question.
- **Renegotiate a hire** -- the user directs the change in their own words
  ("less intense", "more like X"); rebuild the persona with the agent-studio
  five-element template, re-run its grep lint, and save the variant to
  `research/[slug]/round1/squad_personas/` (run-local; never overwrite the
  skill's defaults unless the user says "make it permanent"). A staff seat can
  also be fully RECAST this way: a new character hired against the same job
  description.
- **Bring in a consultant** -- the topic needs a specialist no staff seat
  covers. FIRST check the selected adapter's retainer roster (`~/.agents/agent-roster/`
  for Codex; `~/.claude/agent-roster/` for Claude) for a
  past hire who fits. Otherwise write a one-line job description and present
  2-3 contrasting candidates: real famous people, historical figures, or
  well-known fictional characters who fit it (each: name, why they fit, what
  their package brings, one risk). The user hires one for this run
  (temp hire); afterwards offer to keep them on the selected adapter's roster.
  Every hire is an interpretation of the
  figure or character, never the real person, labeled as such in the file.

SKIP this stage silently when: the run is headless/non-interactive, the user
said "just go" / "skip the cast" / similar, or this same cast was already
confirmed earlier in the session. A renegotiated, recast, or consultant member
is a generating lens like any other: isolated subagent, same preamble, same
5-gap cap, and it plays its part: it speaks in its character's name and voice
while its Output format stays binding.

## Stage B: The panel (4 isolated subagents, parallel where slots allow)

Dispatch one fresh native subagent per persona through the selected adapter. Run
them concurrently only up to available host slots, then batch the remaining prompts
without changing personas, prompts, or isolation. Each prompt is, in order:
1. The SUBAGENT PROMPT PREAMBLE below, verbatim.
2. The full text of ONE persona file from `references/squad-personas/`:
   `umberto-eco-academic.md`, `w-edwards-deming-practitioner.md`,
   `cicero-policy.md`, `nate-silver-skeptic.md`.
3. The task: "For the scope below, name expected-but-absent coverage a competent
   reader of YOUR kind would miss. At most 5 gaps, in your persona's Output
   format, each with one scope-bounded Exa query."
4. The Stage-A digest.

Never run the panel as persona swaps inside one shared context: that is the
maximally-colluding anti-pattern. One fresh subagent per persona, every time.
No lens sees another lens's output.

### SUBAGENT PROMPT PREAMBLE (verbatim, every generating lens)

```
You are a single, isolated lens on this question. Rules:
1. Reason ONLY from your assigned persona/stance below. Do not adopt a neutral
   "balanced" voice.
2. Do NOT anticipate, accommodate, or pre-agree with any other lens. You cannot see
   them and must not imagine a consensus.
3. Return your own genuine view even if you suspect it is the minority position —
   the minority view is exactly what this panel exists to capture.
4. NEVER use WebFetch. If you must read a page, use `curl -sL <url>` and read the
   raw text.
5. End with a one-line "Dissent I would defend:" stating the point you would hold
   even if outvoted.
```

## Stage C: Merge (orchestrator): dedup + severity-rank, never blend

1. Pool all gap lines: the Stage-A.5 `[checklist]` gaps PLUS every persona gap.
2. De-duplicate by MEANING, not wording: two gaps naming the same absent
   coverage collapse into one line that keeps the higher severity, the better
   query, and BOTH proposers' names. A checklist gap and a persona gap naming the
   same absent item collapse into one (keep the `[checklist]` tag so it stays
   cap-exempt).
3. Rank: severity first (HIGH > MED > LOW), then breadth (a gap named by two
   proposers outranks a same-severity gap named by one).
4. **Cap = scope-named checklist gaps are exempt; everything else competes at 8.**
   Every `[checklist]` gap (scope-NAMED, absent) is ALWAYS kept — it is cheap and
   definitional, and dropping a technique the scope named by name is the exact
   failure this pass exists to prevent. Everything else — the persona DEPTH gaps
   AND the `[sibling]` gaps (in-domain but UNnamed by the scope) — competes for a
   cap of **8** (`SQUAD_MAX_DEPTH_GAPS`), ranked by severity then breadth. Siblings
   are speculative (the scope did not ask for them), so they earn their slot on
   merit like a depth gap, not by exemption. Log every gap dropped at the cap in
   `round1/squad_audit.md`: a silent cap reads as full coverage when it is not.
   The retrieval ledger's exit-21 (Stage F) and the 2-round ceiling (Stage G) are
   the real backstops on total spend, not the cap.

This is a union with dedup, NOT a consensus step. Never drop a gap because only
one persona raised it: single-lens gaps are the reason the panel exists.

## Stage D: Adversarial verification (1 subagent per merged gap, batched by slots)

For EACH merged gap, dispatch one fresh verifier subagent (batched to available host
slots): the full text of
`references/squad-personas/karl-popper-verifier.md`, the one gap, and the Stage-A digest. The
verifier tries to REFUTE the gap (already covered in the corpus, or out of
scope) and returns `REFUTED: <evidence>` or `CONFIRMED: <refutation tried>`.

- REFUTED gaps are dropped and logged with the refutation.
- CONFIRMED gaps proceed to the fill.
- Half-covered gaps (the escalation case) proceed with the query narrowed to
  the truly absent half.

The verifier receives one gap at a time so a plausible neighbor cannot lend it
credibility. Verifiers never add or reword gaps.

## Stage E: Artifacts

Write both, before any Exa spend:
1. `round1/coverage_gaps.md`: the confirmed gaps + queries, in the stock format
   (`# Coverage audit, gaps (round N)`, numbered gap + query) so downstream
   readers see the artifact they expect.
2. `round1/squad_audit.md`: the full panel record: the Stage-A.5 checklist
   (every scope-named item + COVERED/ABSENT/THIN verdict), each persona's raw gap
   lines and dissent line, the merge table (checklist + persona gaps, dedup
   noted), every REFUTED gap with its refutation, every capped-out depth gap.

## Stage F: Fill (reuse deeper-research's machinery, unchanged)

For each confirmed gap, in order of rank:

```bash
python3 scripts/slice_search.py \
  --add-slice gap_<kebab-slug> --query "<the gap's query>" \
  --run-dir research/[slug] --topic "<topic>"
```

Exit-code semantics are the pipeline's own: `21` = ledger cap tripped: STOP
filling, surface it, never retry silently (prior fills stay on disk). Then:

```bash
python3 scripts/fetch_fulltext.py --run-dir research/[slug]
python3 scripts/evidence_gate.py --run-dir research/[slug]   # must exit 0
```

Cost: under Codex or Claude Code, the panel + verifiers can use native
subscription-backed subagents at $0 API cost. Under the generic fallback, they may use
a metered configured provider and must be charged accordingly. Only fills touch the Exa
ledger: worst case $0.04 x 8 = $0.32.

## Stage G: Second round (optional, hard cap 2 rounds)

If Round 1 confirmed >=1 gap, you MAY re-run Stages A-F once over the enlarged
corpus. Stop immediately when: zero confirmed gaps, or round 2 completes, or
exit 21. Never loop past 2 rounds.

## Report (co-report rule: quality AND diversity, never one scalar)

End with BOTH notes, always:
- **Quality note**: gaps proposed / merged / confirmed / refuted / filled, and
  the gate's final verdict.
- **Coverage/diversity note**: which personas contributed confirmed gaps. If ALL
  confirmed gaps came from one persona, say so: the panel added no diversity
  this run and that is a finding, not a failure to hide. Quote each persona's
  "Dissent I would defend:" line in `squad_audit.md`.

**Performance review (end of run).** One short review per member: gaps kept vs
refuted, the dissent they defended, what they missed. A member with zero kept
gaps is reported plainly, with a judgment on whether the SEAT (not the
character) fits this topic class, which is the recast signal. For each
consultant/temp hire, end with ONE question: keep them on the selected adapter's roster
(`~/.agents/agent-roster/<name>.md` for Codex; `~/.claude/agent-roster/<name>.md` for
Claude), appending a `## Track record` line: date,
topic, gaps kept, one-line verdict) or let them go. Staff reviews accumulate in
`squad_audit.md` only.

## Hard rules carried from agent-studio

- Strict isolation for generating lenses; verifier sees one finding at a time.
- Never persona-swap in one shared context.
- Never naive-mean-blend; the combine is union + dedup + severity-rank.
- NO WebFetch anywhere, orchestrator or subagent; `curl -sL` for raw pages.
- Judgment mode: no demographics, no invented authority; personas focus
  attention, they do not add capability.

## Failure modes

| Failure | Prevention |
|---|---|
| Panel run as persona swaps in one context | Stage B: one fresh native subagent per persona; batch only when slots require it |
| Personas skip "boring" scope-named techniques to chase signature gaps | Stage A.5: mechanical scope-checklist pass flags every named-but-absent item |
| A real scope-named gap dropped for space | Stage C: `[checklist]` gaps are cap-exempt; only persona depth gaps compete for the cap |
| Consensus flattening drops a single-lens gap | Stage C: union + dedup only; single-proposer gaps survive to verification |
| False gap burns ledger budget | Stage D: per-gap adversarial refutation before any Exa spend |
| Silent cap reads as full coverage | Stage C/E: every dropped or capped gap logged in squad_audit.md |
| Ledger runaway | Stage F: stock exit-21 semantics; cap 8 gaps/round, 2 rounds |
| Double audit | This skill REPLACES coverage_audit.py for the round; never run both |
