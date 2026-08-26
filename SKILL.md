---
name: deep-research
description: Use when the user needs comprehensive, fact-checked, evidence-based research on any topic. Triggers on requests for deep research, literature reviews, comprehensive reports, or evidence-based analysis. Runs domain scoping, Exa retrieval slices with an evidence gate, question-driven deepening, synthesis, integration, mechanical citation verification, and a grounded adversary pass to produce a single authoritative reference document.
---

# Deep Research

Retrieval-first deep research. Round 1 fetches a real evidence corpus with Exa
search slices (plus a free OpenAlex/Semantic Scholar academic anchor), a hard
**evidence gate** refuses to synthesize over a thin corpus, a **coverage auditor** then
names and fills the gaps a competent reader would expect, and every later round
reasons over that fetched evidence — not the model's memory. Question-driven
deepening chases root-cause / consequence / gap questions, a mechanical verifier
resolves every citation, and a **different-provider adversary** tries to refute
the draft. The output is a single, fact-checked, fully-cited reference document —
a "Research Bible."

There is ONE pipeline. No model fleet, no `mode` flag other than `slices`.

## Prerequisites

**Exa key — required for retrieval:**
```
EXA_API_KEY          # Round-1 slices + Round-2.5 deep-reasoning (the retrieval engine)
```
Without it, retrieval scripts exit `20`.

**LLM keys — set whichever you have.** Reasoning rounds (synthesis, integration,
adversary) run on the Claude Code session's own subagents first; the metered
providers below are used when a script or subagent needs a direct API call:
```
ANTHROPIC_API_KEY    # Claude (synthesis / integration default)
OPENAI_API_KEY       # ChatGPT (gpt-4.1)
GOOGLE_API_KEY       # Gemini 2.5 Pro
XAI_API_KEY          # Grok (grok-3-latest)
SEMANTIC_SCHOLAR_KEY # Optional — raises rate limit for the academic anchor / lit_search
CONTACT_EMAIL        # Optional — joins OpenAlex/Crossref "polite pool"
```

**Four built-in providers** — `claude`, `chatgpt`, `gemini`, `grok`. Each carries a
`family` (`anthropic`, `openai`, `google`, `xai`) used for adversary selection (see
[Provider family + adversary](#provider-family--adversary-selection)). Providers can
also be defined in TOML for any OpenAI-compatible endpoint, or as a `cli` subscription
provider at $0 (see [Provider/Agent Config](#provideragent-config-toml)).

**Python packages:**
```bash
pip install -r requirements.txt
```

## Architecture: the rounds

```
Round -1   FRAME            (default-on, skippable) coach the umbrella question +
              ↓             sub-questions + scope before any spend — AskUserQuestion
Round 0    SCOPE            scope.py → scope.json (domain + source priorities)
              ↓
Round 1    RETRIEVE         slice_search.py → Exa slices (full text) + academic anchor
              ↓             fetch_fulltext.py → download full text of EVERY source (keep longest; save raw)
              ↓             evidence_gate.py → MUST pass (exit 0) before any synthesis
              ↓             citation_chase.py → one-hop citation graph fill (co-citation + citing works), re-gate
              ↓             coverage audit → deep-research-squad skill (DEFAULT: checklist + panel + verify) — coverage_audit.py is the single-model fallback — name expected-but-absent coverage, fill, re-gate
Round 2    SYNTHESIZE       compare the corpus; emit the six EXACT headers (4 feed buckets)
              ↓
Round 2.5  DEEPEN           deepen_questions.py → root-cause / consequence / gap answers
              ↓
Round 3    INTEGRATE        planners + parallel section subagents + dedup_bib
              ↓
Round 4    GATE + ADVERSARY verify_citations.py (+ ⚠ Unresolved links) → refute adversary
              ↓
Round 5    RERUN (targeted) slice_search --only-slice / deepen --single-question → re-integrate
              ↓
EXPORT     BibTeX + claims.jsonl + refresh the project-wide semantic index
```

## Stage 0: Framing (default-on, skippable)

A badly scoped question wastes the whole run: the framing step is the single
highest-leverage moment in the pipeline. Before Round 0, coach the user through
turning a raw ask into a sharp, researchable umbrella question with explicit
sub-questions and scope. This is a SHORT structured dialog, not a wall of text.

**Run it by default.** Skip ONLY when one of these holds:
- The invocation already carries a well-formed umbrella question AND scope
  (e.g. `args` include an explicit question plus sub-threads, as when another
  skill or a prior chat already did the framing).
- The user passed a skip signal: `just go`, `skip framing`, `no framing`,
  `trust the model`, `--no-frame`, or similar.

**When you run it, use ONE `AskUserQuestion` call with ≤3 questions:**

1. **Umbrella question.** Restate the user's raw ask as a candidate umbrella
   question and offer it as the recommended option, plus 1-2 alternative framings
   (broader / narrower / different angle). This shows the user the question you
   will actually research and lets them correct it in one tap.
2. **Sub-questions / focus.** Propose 3-6 sub-questions the run will deepen, as a
   multi-select so the user can drop or keep each. Include an "add your own" via
   the free-text option.
3. **Scope dials.** One multi-select covering the choices that change retrieval:
   time window (e.g. last 2 years vs. all), academic-vs-practitioner weighting,
   geography/language, and depth (single-session vs. exhaustive). Offer sensible
   defaults as the recommended option.

Then reflect the confirmed framing back in one line and proceed to Round 0,
threading the umbrella question into `--topic` and the sub-questions + scope into
`--scope`. Do NOT spend any retrieval budget before this reflection.

If `AskUserQuestion` is unavailable (headless / cron / non-interactive run), skip
Stage 0 silently and fall back to the raw topic: never block an unattended run
waiting on input.

## Round 0 — Scope

Classify the topic into a domain and propose source priorities. `--use-llm` is
optional (refines with the configured utility provider; falls back silently to
rule-based scoping if none is available or the call fails).

```bash
python3 scripts/scope.py \
  --topic "Your topic" \
  --scope "What to cover, subtopics, depth, time period" \
  --output research/[slug]/scope.json \
  --use-llm     # optional
```

`--use-llm` resolves the provider via `config.pick_provider` on the `[defaults].utility`
role (TOML), else the first available of `claude-sub`, `claude`, `chatgpt`, else any
configured provider. `scope.py` writes both a markdown scope file and the sibling
`.json` used downstream. Domain and freshness *retrieval* controls live on
`slice_search.py` (`--fresh-since`), not on `scope.py`.

## Round 1 — Exa retrieval slices + evidence gate

**Step 1.1 — print the command sequence.** `dispatch.py` is a thin guide (a
slices-only orchestrator, not a runner). It validates the mode, preflights the Exa
key, and prints the ordered Round-1 sequence:

```bash
python3 dispatch.py \
  --topic "Your topic" \
  --scope "Your scope" \
  --run-dir research/[slug] \
  --max-retrieval-usd 1        # optional; threads into the slice_search command
```

- `--mode` defaults to `slices`. **Any other value → exit `2`** (there is no legacy path).
- `EXA_API_KEY` unset → exit `20`.

The printed sequence is `scope.py → slice_search.py → evidence_gate.py`, each line
runnable as-printed (topic, run-dir, and the retrieval cap are threaded through).

**Step 1.2 — retrieve.** `slice_search.py` fires one Exa `/search` per ENABLED slice,
tiers + dedupes results, and writes `round1/slice_<name>.jsonl`, `round1/brief_<name>.md`,
and `round1/evidence_manifest.json`. It also writes a free OpenAlex/Semantic Scholar
academic anchor (`slice_anchor.jsonl`, $0 — never ledgered).

Each Exa slice now also requests **full page/PDF text** (`contents.text`, capped at
`DR_TEXT_MAX_CHARS`, default 12k). That text is spilled to `round1/sources/<file>.txt`
and each jsonl row carries `text_path` + `text_chars` — so synthesis reads the whole
document, not a highlight snippet. Set `DR_TEXT_MAX_CHARS=0` to fall back to
highlights-only.

```bash
python3 scripts/slice_search.py \
  --run-dir research/[slug] \
  --topic "Your topic" \
  --max-retrieval-usd 1 \
  --fresh-since 2024-01-01 \   # optional; adds startPublishedDate to the news slice
  --resume                     # optional; skip slices whose jsonl already parses
```

Default slice roster: `publication`, `news`, `institutional` ON; `financial`,
`personal-site` OFF. Each slice sets EITHER an Exa `category` OR an
`include_domains` allowlist — never both (XOR). Per-slice **fail-open**: one HTTP
or parse error writes an empty slice + a notice and the run continues (exit 0) —
thin evidence is the gate's job, not this script's.

**Exit codes on the retrieval path:**
- `20` — `EXA_API_KEY` unset.
- `21` — retrieval ledger cap exceeded. **Surface this; do NOT retry.** Prior slices'
  files stay intact; raise `--max-retrieval-usd` (or free budget) and `--resume`.
- `22` — from the gate (below): the corpus is too thin.

**Step 1.3 — the evidence gate (mandatory before ANY synthesis).**

```bash
python3 scripts/evidence_gate.py --run-dir research/[slug]
```

The gate RECOMPUTES metrics straight from the jsonl (the manifest is derived and
NOT trusted). It exits `0` only when ALL hold: `global_unique ≥ min_evidence_total`
(default 10), non-empty slices `≥ min_nonempty_slices` (default 2), and every row
re-validates (non-empty `url` and `tier`). Otherwise it prints a per-slice diagnosis
and exits `22`.

**On exit 22:** the corpus is thin — do NOT synthesize. Fix it: broaden the scope /
enable more slices in TOML, re-run `slice_search.py --resume`, and re-gate. Only when
the gate returns `0` may Round 2 begin.

**Step 1.4 — full-text download (recommended).** This pass attempts a direct
full-text download of **every** source — not just the ones Exa left thin —
resolving an **open-access PDF via OpenAlex** for DOI/academic rows and fetching
plain PDFs and pages as-is. For each row it keeps whichever text is **longer**,
the Exa snippet or the document it fetches itself, so a source is only left at its
snippet when the document is genuinely unreachable (paywall / 404 / no OA copy).
Extracted text (pypdf for PDFs, tag-strip for HTML) goes to `round1/sources/<file>.txt`
and the row's `text_path`/`text_chars`; the **original downloaded file** is also
saved alongside as `round1/sources/<file>.pdf|.html` and recorded on the row as
`raw_path`.

```bash
python3 scripts/fetch_fulltext.py \
  --run-dir research/[slug]
```

Every fetch goes through the SSRF-hardened, IP-pinned path reused from
`verify_citations` (redirects re-vetted per hop); per-source **fail-open**; `$0` —
never ledgered; **no WebFetch** (raw bytes read directly). Writes
`round1/fulltext_manifest.json` (attempted / fetched / by-method / failures; a
`kept-existing` failure means the fetch landed but the Exa snippet was already as
long or longer). Requires `pypdf` (in `requirements.txt`); without it, PDF rows
skip gracefully. Set `CONTACT_EMAIL` (in `~/.env`) so OpenAlex OA-PDF lookups use
the polite pool and resolve more open-access documents.

**Step 1.4b · citation chase (run after the gate passes, before the coverage audit).**
This does evidence-grounded graph fill FIRST, so the model-memory coverage auditor then
reasons over the enlarged corpus. It walks one hop out of the Round-1 seeds purely from
the citation graph, never from an LLM: BACKWARD co-citation (works many seeds reference in
common) plus a small FORWARD pass (newer works that cite the strongest seeds). New works
are de-duped against the corpus, written to `round1/slice_citation.jsonl`, then
full-text-fetched and re-gated.

```bash
python3 scripts/citation_chase.py \
  --run-dir research/[slug] \
  --topic "Your topic"
```

**Fail-CLOSED exit codes (read them before proceeding).** Exit `0` means the chase RAN:
it either expanded the corpus or found nothing new after dedupe · in both cases proceed.
A NONZERO exit means the chase could NOT complete, and you must NOT proceed as if
expansion succeeded: surface the code and resolve it before the coverage audit.
- `40`: every OpenAlex request failed (network unreachable) · the chase could not run.
- `41`: no seed yielded a resolvable OpenAlex id or references · nothing to chase.
- `22`: the re-gate found the corpus STILL too thin / a row failed re-validation.
Any nonzero code means the enlarged corpus is unverified: surface it, resolve it (restore
network, broaden the seed corpus, re-fetch), and only continue to the coverage audit once
the chase returns `0`.

**Step 1.5 · coverage audit (run after the gate passes).** The gate asks "is the
corpus thick enough?"; this asks a different question: "for THIS scope, what coverage a
competent reader would expect is still absent?"

**DEFAULT — use the `deep-research-squad` skill.** Every run performs Step 1.5 via the
`deep-research-squad` skill, not the single-model script. Invoke it now (it takes this
same run dir). The squad runs a cheap mechanical scope-checklist pass (every technique
the scope names + a disciplined sibling sweep for in-domain methods the scope forgot),
THEN four isolated reader personas for the depth gaps a checklist can't see, merges
(scope-named gaps cap-exempt), and adversarially verifies every gap with a per-gap
refuter BEFORE any Exa spend. Head-to-head on a real corpus it beat the single-model
script on depth and killed a false positive the script would have paid to fill; the
checklist pass keeps the script's one advantage (dutiful named-technique breadth). The
squad writes the SAME `round1/coverage_gaps.md` artifact this pipeline expects, plus
`round1/squad_audit.md`, and reuses `slice_search.py --add-slice` for the fills — so
everything downstream (Round 2 onward) is unchanged. In an interactive run the squad
shows a skippable cast card; in headless/cron runs it skips that silently and still runs.

**FALLBACK — the single-model script** (`coverage_audit.py`), for when the squad
genuinely cannot be dispatched (e.g. a constrained subprocess with no subagent
capability). It enumerates expected-but-absent coverage with ONE model, pairs each gap
with one scope-bounded Exa query, fires each as an ad-hoc gap slice, re-gates, and asks
whether gaps remain — no checklist stage, no adversarial verification, so it can spend
budget on a false gap. Use it only when the squad is unavailable:

```bash
python3 scripts/coverage_audit.py \
  --run-dir research/[slug] \
  --topic "Your topic" \
  --max-audit-rounds 2         # optional; ceiling on fill-and-re-audit loops
```

Both paths write only `round1/coverage_gaps.md` (each gap + its query) and the gap slices
their fills produce: ZERO Bible prose. Naming and filling gaps is the whole job; synthesis
is Round 2's. NEVER run both in the same round.

**Fail-CLOSED exit codes (read them before proceeding).** Exit `0` means the audit RAN
and coverage is adequate: only then is coverage verified. A NONZERO exit means the audit
could not complete OR the corpus is still inadequate, and the orchestrator must NOT
proceed to synthesis as if coverage was verified:
- `30`: no LLM provider configured (audit could not run).
- `31`: the audit model call raised (audit could not run).
- `32`: the model reply's gap-list would not parse (audit could not run).
- `21`: a gap fetch tripped the retrieval cap; `coverage_gaps.md` is written first, then
  the audit stops. Raise `--audit-usd` (or free budget) and re-run.
- `22`: the re-gate found the corpus STILL too thin / a row failed re-validation.
Any of these means coverage is unverified: surface the code, resolve it (add a provider,
retry the model, raise the cap, broaden and re-fetch), and only continue to Round 2 once
the audit returns `0`.

## Round 2 — Synthesis

Dispatch **one synthesis subagent** to read the entire Round-1 corpus (all
`round1/slice_*.jsonl` + `brief_*.md` + the anchor) and produce a comparison. For any
row with a `text_path`, **read that `round1/sources/<file>.txt` full-text file** and
reason over the document itself — the highlights are only an index into it, not the
evidence. It runs
on the Claude Code session's own subagent (subscription, $0) when available, else on a
metered Anthropic call bounded by `--max-cost-usd`.

**The synthesizer MUST emit these EXACT markdown headers** — `deepen_questions.py`
parses them verbatim and a typo silently empties a bucket:

```
## Comparison
## Surprises
## Openings
## New Questions
## Root Cause Questions
## Consequence Questions
```

Write the synthesis to `research/[slug]/round2/synthesis.md`.

- `## Comparison` — where the corpus agrees / overlaps / disagrees, with the sources.
- `## Surprises` — findings that cut against the obvious prior.
- `## Openings` — promising directions the corpus only gestures at.
- `## New Questions`, `## Root Cause Questions`, `## Consequence Questions` — the
  question buckets Round 2.5 chases (gap / root-cause / consequence).

### Field map: how this field is organized

Produce ONE near-top Bible section that maps the field: the **mainstream** position
vs the **heterodox** ones, and what is **settled** vs **genuinely contested**. This
orients the reader before the detail arrives: which camps exist, where the center of
gravity sits, and which questions are actually open.

**HARD RULE (this is the safety property).** EVERY structural claim in the field map
must be EITHER:
- **(a) cited to retrieved evidence:** the same `[Author, Year]` / source attribution
  every other claim carries, OR
- **(b) emitted inside a fenced editorial block** delimited by
  `<!-- editorial:background -->` … `<!-- /editorial -->`.

There is NO "no evidence exists, so assert it anyway" path. An uncited
mainstream/heterodox or settled/contested partition asserted as bare prose is not
acceptable output. If you cannot cite a partition and will not fence it as editorial
background, do NOT emit it.

**What actually enforces this.** `lint_background.py` is a numeric tripwire INSIDE the
fences only: it flags an **uncited** quantity (number, date, rate, share) — one in a
fenced sentence that carries no `[Author, Year]` citation marker — and it inspects
nothing else: it does NOT read unfenced prose, and a *cited* quantity inside a fence
passes. So the lint alone cannot catch an uncited partition dropped into bare prose.
The enforcement MECHANISM that catches an uncited empirical claim before it reaches the
Bible is the Round-4 refute-mode ADVERSARY: an LLM refute pass that reads the actual prose
(fenced and unfenced) and refutes unsupported claims, backed by author discipline in
following the cited-or-fenced rule above. This is a review-based check, not a mechanical
proof: the lint is the numeric tripwire inside the fences, the adversary is the
reader-facing pass over the prose.

The map's interpretive/opinion claims follow the `### Source authority` rules below:
attribute each camp to its named source · show the SPREAD of views · never rank camps
by source `tier`.

### Source authority: how to weigh evidence

Before weighing sources for any claim, CLASSIFY the claim: **factual/quantitative**
(a number, date, measurement, event) vs **interpretive/opinion** (a judgment,
forecast, framing). Weigh sources differently per class.

- **Quantitative/factual claims** — prefer higher-provenance sources and claim
  quality: error bars, sample size, replication, primary over secondary. Read the
  tags (`tier`, `institution`, `stance`, `replication`). FLAG explicitly any number
  that rests only on a tier-0 source or an `unverified` institution: name the
  weakness, do not launder the figure into the prose.
- **Interpretive/opinion claims** — do NOT rank by source `tier`. Attribute the view
  to its named source. Where sources disagree, present the SPREAD of views rather
  than resolving by authority.
- **Prestige as a prior, not a trump** — treat prestige signals in the tags (author
  `h-index`, `established` institution) as a credibility PRIOR for NEW or low-cited
  work only: it lifts a fresh result off the floor but NEVER overrides contradicting
  evidence or a failed/absent `replication`.
- **Read the transparent tags** — distinguish `named` (curated-recognized)
  institutions from `unverified` ones. An `established` institution is not
  necessarily neutral: a `stance: advocacy` tag means the view carries a known
  agenda, so attribute it, do not treat it as disinterested `stance: research`.

## Round 2.5 — Deepening

Ingest the Round-2 headers, split questions into three buckets (root-cause /
consequence / gap — where gap = `## New Questions` then `## Openings`), allocate up to
**3 per bucket, cap 9 total**, and fire one Exa `deep-reasoning` call per allocated
question.

```bash
python3 scripts/deepen_questions.py \
  --run-dir research/[slug] \
  --round2-file research/[slug]/round2/synthesis.md \
  --max-retrieval-usd 1        # optional
```

Writes `round2_5/answer_NN_<bucket>.md` (each with a terminal `## Sources` block) and
`round2_5/coverage.json` (questions asked vs answered, per bucket). Ledger-capped like
Round 1: a per-question fail-open skips + continues (exit 0); a cap breach writes
`coverage.json` FIRST, then exits `21`.

## Round 3 — Integration

Read four inputs and **explain the field, position by position** — the Round-1 briefs, the
Round-1 retrieved **full texts** (`round1/sources/*.txt`, the actual documents, not just
the highlight briefs), the Round-2 synthesis (`round2/synthesis.md`, which carries the
field map), and the Round-2.5 answers. The job of this round is to TEACH the material, not
to compress it into a cited outline: reconstruct the arguments in full and make the
disagreements legible. Preserve every citation, every `[as of: <date>]` and
`[confidence: …]` tag, and every unique finding; present differing figures as
`[disputed: …]`, never a silent average — but do this in service of explanation, cutting
abstract connective prose before ever cutting a specific argument or example.

**The unit of the report is the position, not the topic bucket.** The section planner
creates **one section per major position / argument / school** the corpus supports, not
an arbitrary set of topic headings. Dispatch section-planner subagents + a reconciler,
then **one integration subagent per section** in parallel (each ≤ ~40k words of input).

**Deep-explainer template — every integration subagent fills all four parts, in full
prose, for its position:**
1. **The claim.** State the position plainly: what it asserts about the question.
2. **The argument.** Reconstruct the reasoning premises → conclusion — WHY a reasonable
   person holds it, not just that they do. Walk the steps; do not gesture at them.
3. **The strongest objection AND the reply.** Name the most serious challenge the
   literature raises, then the position's best answer to it. This is the live
   disagreement, written out — never a bare "critics disagree."
4. **How it differs from rival positions.** The specific contrast(s) that separate this
   position from its neighbours — the fault line, not a restatement.

**Depth targets.** Aim for roughly **2–4k words per position section** — enough to make
the argument and its objection genuinely understandable. The report **scales with the
literature: there is NO global length cap.** A field with a dozen live positions yields a
long report; that is correct, not bloat. When a section subagent must choose between a
shorter section and keeping a specific argument or example, it **keeps the argument** and
trims connective prose instead. Final integration note: **expand, don't summarize** — the
failure mode this round exists to prevent is a thin enriched outline.

> **Input-budget note (with the raised text cap).** `DR_TEXT_MAX_CHARS` now defaults to
> ~40k chars (~6.5k words) per source, so a handful of very long primary sources can crowd
> a single section subagent's ≤ ~40k-words input window. The section planner should watch
> per-section input budget and, when a few sources are very long, split the section or
> select the most relevant passages rather than letting two or three documents starve the
> rest.

Carry the **field map** through from `round2/synthesis.md`: keep the near-top
mainstream-vs-heterodox / settled-vs-contested section, and hold its HARD RULE: every
partition either cited to retrieved evidence or fenced in
`<!-- editorial:background -->` … `<!-- /editorial -->`. An uncited partition asserted
as bare prose is not acceptable output. The enforcement mechanism is the Round-4
refute-mode adversary reading the prose (an LLM refute pass, review-based, not a
mechanical proof; the lint only scans for uncited numbers inside fences), backed by author
discipline in following the rule.

**Section subagents: background is fenced-only.** A section subagent MAY add
background / definitional / mechanistic / historical context to orient the reader, but
ONLY inside a fenced `<!-- editorial:background -->` … `<!-- /editorial -->` block
(emit it via `render_background`, `scripts/background.py`). A fenced block MAY carry
empirical or quantitative substance — a number, date, measurement, rate, share, or
named-study finding — **but every such quantity must be CITED**: it must sit in a
sentence carrying an `[Author, Year]` citation marker. An **uncited** quantity inside a
fence is a violation (the lint catches it). Framing prose that belongs to the corpus but
sits outside a fence is uncited output, not background.

### Source authority: how to weigh evidence

Carry the Round-2 discipline through integration. Before weighing sources for any
claim, CLASSIFY it: **factual/quantitative** (a number, date, measurement, event) vs
**interpretive/opinion** (a judgment, forecast, framing). Weigh differently per class.

- **Quantitative/factual claims** — prefer higher-provenance sources and claim
  quality: error bars, sample size, replication, primary over secondary. Read the
  tags (`tier`, `institution`, `stance`, `replication`). FLAG explicitly any number
  that rests only on a tier-0 source or an `unverified` institution: mark it, do not
  present it as settled.
- **Interpretive/opinion claims** — do NOT rank by source `tier`. Attribute the view
  to its named source. Where sources disagree, present the SPREAD of views in the
  section rather than resolving by authority.
- **Prestige as a prior, not a trump** — treat prestige signals in the tags (author
  `h-index`, `established` institution) as a credibility PRIOR for NEW or low-cited
  work only: it lifts a fresh result off the floor but NEVER overrides contradicting
  evidence or a failed/absent `replication`.
- **Read the transparent tags** — distinguish `named` (curated-recognized)
  institutions from `unverified` ones. An `established` institution is not
  necessarily neutral: a `stance: advocacy` tag means the view carries a known
  agenda, so attribute it, do not treat it as disinterested `stance: research`.

Build the master bibliography deterministically (beats LLM dedup):

```bash
python3 scripts/dedup_bib.py research/[slug]/round1/brief_*.md \
  --output research/[slug]/sections/bibliography.md
```

Then assemble the sections into a single draft and run a cross-section consistency
auditor over the whole document (contradictions, orphaned citations, redundancy).

## Round 4 — Mechanical gate + adversary

**Step 4.1 — mechanical citation verification.**

```bash
python3 scripts/verify_citations.py research/[slug]/sections/ \
  --output research/[slug]/round4/citation-verification.md \
  --check-urls
```

Resolves every `[Author, Year]` and bibliography entry against **OpenAlex** and
**Crossref** (free, no key) — DOI resolution for academic works — and flags
unresolved / weak-match / orphaned cites. `--check-urls` runs the **three-state,
SSRF-hardened** link probe. Output carries a `## ⚠ Unresolved links` section with
three sub-lists — NOT a binary "dead URLs" list:

- **Unresolved (page gone / unreachable)** — 404/410 or DNS/connection failure.
- **Indeterminate (could not confirm)** — auth wall, blocked method, rate limit,
  server error, TLS problem, timeout, oversize body, or a policy rejection
  (non-globally-routable host). Every link is *kept and flagged*, never deleted.
- **Truncation note** — URLs beyond the per-run probe cap (60), left unchecked.

Optional companions: `classify_sources.py` (tier / quality score) and
`lit_search.py --compare-bib` (canonical works missing from the bibliography).

**Background-block lint (orchestrator-run checklist step).** Before the adversary,
lint every fenced editorial block for UNCITED quantities:

```bash
python3 scripts/lint_background.py research/[slug]/sections/
```

Exit `0` = clean; exit `1` = at least one fenced sentence names a quantity with NO
`[Author, Year]` citation, and the offending blocks are printed (a *cited* quantity in a
fence passes and is never flagged). The orchestrator MUST resolve each flagged **uncited**
quantity one of three ways: **(a)** cite it, retrieve a source and add the `[Author, Year]`
in the same sentence (it may then stay inside the fence — cited substance is allowed), or
rewrite it as normal cited prose; **(b)** keep the block fenced but rephrase it to remove
the quantity (drop the number, keep the qualitative framing); or **(c)** cut the claim.
NEVER move the quantity out of the fence into unfenced prose without a citation: that
creates the exact uncited-empirical-claim state the cited-or-fenced rule forbids. This
is a hard checklist gate, not an advisory.

**Fenced background stays fenced (nothing mechanical promotes it).** Background /
editorial blocks stay FENCED permanently: they are NEVER auto-cited and NEVER
auto-promoted. Nothing mechanical promotes a memory claim into cited prose. If the author
or the Round-4 adversary judges a fenced claim important enough to belong in the Bible as
a normal cited statement, the ONLY paths are BY HAND: either find a real retrieved
citation and rewrite the claim as normal cited prose carrying that `[Author, Year]`, or
cut it. A fenced claim with no retrieved citation stays inside its fence or is dropped:
it is never lifted out unsupported.

**Step 4.2 — refute-mode adversary (a DIFFERENT provider family).** Dispatch one
adversary subagent whose job is to *refute* the draft — find unsupported claims, weak
attributions, and figures the corpus does not support. It must run on a provider whose
`family` differs from the synthesizer's (`anthropic`) so the critique is genuinely
independent. Selection walks the configured `adversary` chain (default
`grok → chatgpt → gemini`) and picks the first configured, available provider whose
family ≠ `anthropic`; if none qualify it warns and falls back to the synthesizer (see
[below](#provider-family--adversary-selection)). Feed the adversary the section files
plus `round4/citation-verification.md`; write its report to `round4/factcheck.md`.

**Shard the adversary by section for long reports.** A single refute pass over a
15–40k-word report is too shallow to spot-check every quote and attribution — the deep
per-position sections (Round 3) produce reports well past that. So for any report over
**~12k words**, dispatch **one refute-adversary per section group** rather than one pass
over the whole document, each cross-checking ITS sections' quotes and attributions
against the full-text store (`round1/sources/*.txt`) and writing a shard report
(`round4/factcheck_<group>.md`, merged into `round4/factcheck.md`). **The
different-provider-family rule holds per shard:** every shard still runs on a provider
whose `family` ≠ `anthropic`. (This session, sharding into two adversaries caught a
fabricated block quote with a fake-precise citation that a single whole-document pass
skimmed past.)

**Adversary mandate: police every editorial:background block.** The adversary MUST
scrutinize EVERY `<!-- editorial:background -->` … `<!-- /editorial -->` block for
wrong or contested claims, and specifically for comparative / superlative claims that
the mechanical lint cannot catch (it only flags numbers). Examples the adversary owns:
"doubled", "an order of magnitude", "more common than", "the largest", "unprecedented".
This adversary pass is the backstop for exactly those non-numeric empirical claims: the
lint cannot see them, so the adversary must.

**Step 4.3 — fix pass.** Correct the flagged errors and reassemble the final document.

## Round 5 — Targeted rerun (optional)

If the adversary or the gate exposes a weak spot, rerun narrowly, then re-integrate.

```bash
# Re-fetch one slice with a sharper query:
python3 scripts/slice_search.py --run-dir research/[slug] --topic "Your topic" \
  --only-slice institutional --query "Your topic — specific sub-question"

# Or deepen exactly one question in a chosen bucket:
python3 scripts/deepen_questions.py --run-dir research/[slug] \
  --single-question "The specific open question" --bucket gap   # or root_cause | consequence
```

Re-run the gate / verifier over the affected section and update the draft. Cap at 2
iterations to avoid loops.

## Export

```bash
python3 scripts/export.py \
  --sections research/[slug]/sections/ \
  --bibliography research/[slug]/sections/bibliography.md \
  --output-dir research/[slug]/export/

# Refresh the project-wide semantic index over every topic's Bible (bundled;
# skips with a notice + exits 0 if search deps / OPENAI_API_KEY absent):
python3 scripts/search.py index
```

## Cost table

Retrieval is metered against a per-run **ledger** (default cap **$1**, `--max-retrieval-usd`).
Each Exa call is pre-charged at fee × retry-multiplier = **$0.02 × 2 = $0.04** worst-case
(one automatic retry), then reconciled from the response's actual `costDollars.total`.

| Leg | Calls (worst case) | Per-call | Worst-case total |
|---|---|---|---|
| Round 1 slices | up to 5 Exa slices (3 on by default) | $0.04 | ≈ $0.20 |
| Round 1 academic anchor | 1 (OpenAlex + S2) | $0.00 | $0.00 |
| Round 2.5 deepening | up to 9 questions | $0.04 | ≈ $0.36 |
| **Retrieval subtotal** | | | **≈ $0.56 (< $1 cap)** |

Metered LLM legs (synthesis / integration / adversary, when not run on a $0
subscription subagent) are covered separately by `--max-cost-usd` on those calls;
`scripts/cost.py` estimates them. Exit `21` on any retrieval cap breach — surface it,
raise the cap, and `--resume`; never silently retry.

## No-WebFetch rule (applies to every subagent in this pipeline)

**Never use WebFetch — in the orchestrator, in subagents, or in a CLI subprocess
provider.** WebFetch returns an AI-generated *summary* of the page, dropping exact
figures, dates, author names, and quoted wording — the very things citation
verification depends on. Fetch the raw page instead:

```bash
curl -sL "$URL" -o /tmp/page.html     # raw page, no summarization layer
grep -in "search term" /tmp/page.html # locate the claim in the actual text
```

A claim is verified only when the raw text supports it — never on a fetched summary.

## Provider family + adversary selection

Four built-in provider families:

| Provider | Family |
|---|---|
| `claude` (Anthropic) | `anthropic` |
| `chatgpt` (OpenAI) | `openai` |
| `gemini` (Google) | `google` |
| `grok` (xAI) | `xai` |

TOML providers set their own `family` (default `"other"`). **The adversary must differ
in family from the synthesizer** (which runs on `anthropic`) so its refutation is
independent. `config.load_run_config()` resolves it: it walks the configured `adversary`
chain (default `["grok", "chatgpt", "gemini"]`), skips entries that name no configured
provider, and returns the first available provider whose family ≠ `anthropic`. If none
qualify it emits `adversary_warning` and falls back to the synthesizer's own provider —
configure any non-anthropic provider (or a `cli` one) for a genuinely independent
adversary.

## Provider/Agent Config (TOML)

- **Providers** — LLM engines. `api_type` (`openai`/`anthropic`/`gemini`/`cli`),
  `api_key` or `api_key_env`, `base_url` (OpenAI-compatible endpoints), `model`,
  `max_tokens`, `capabilities`, `pricing`, `family`, `fallback_models`, `max_concurrency`.
- **`[run]` + `[slices.*]` tables** — the Round-1 slice roster, the retrieval cap
  (`max_retrieval_usd`), gate thresholds (`min_evidence_total`, `min_nonempty_slices`),
  and the `adversary` chain. Omit them to accept the code defaults.
- **`[defaults]` table** — names providers for one-off calls (`utility` → Round-0 scoping).

**Config discovery** (later overrides earlier): `~/.config/deep-research/config.toml`,
then `./deep-research.toml`. `DEEP_RESEARCH_CONFIG` (env) overrides the search path for
the `[run]`/`[slices]` tables. Copy `config.toml.example` and fill in inline keys; both
TOML paths are gitignored.

> **Model-ID drift warning:** Provider model IDs change. For example, DeepSeek legacy
> IDs `deepseek-reasoner` and `deepseek-chat` retire 2026-07-24 in favour of
> `deepseek-v4-*`; GLM model IDs also shift. Always verify the current ID in the
> provider's docs. `max_tokens` must stay within each model's output cap — exceeding it
> causes a 400 error.

### CLI / subscription providers (`api_type = "cli"`)

A provider can run a local CLI (`claude -p`, `codex exec`) authenticated via your SSO
subscription (Claude Pro/Max, ChatGPT) — no per-token cost. `call_cli` scrubs
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the subprocess env so the CLI uses
subscription auth. To let a `claude` cli provider search the live web:

```toml
[providers.claude-sub]
api_type     = "cli"
command      = "claude"
extra_args   = ["--allowedTools", "WebSearch", "Bash(curl:*)"]  # search + curl-only fetch; no Edit/Write; no WebFetch
capabilities = ["web_search"]
family       = "anthropic"
```

`Bash(curl:*)` permits only curl — WebFetch is deliberately excluded (see the No-WebFetch
rule). `--dangerously-skip-permissions` also enables live search but additionally
enables Bash/Edit/Write in your cwd — avoid it for unattended subprocesses.

## When to Use

- User asks for deep research, comprehensive analysis, or an evidence-based report.
- User says `/deep-research [topic]`.
- User needs a literature review, state-of-knowledge summary, or authoritative reference.
- Any research task where accuracy, citation quality, and completeness outweigh speed.

## Benchmark quickstart

A single end-to-end run on a known topic (needs `EXA_API_KEY`; ≈ $0.65 of retrieval,
under the $1 cap):

```bash
TOPIC="grid-scale battery storage economics 2024–2026"
RUN=research/grid-battery
python3 scripts/scope.py --topic "$TOPIC" --scope "LCOE trends, chemistry mix, capacity buildout, policy drivers" --output "$RUN/scope.json"
python3 scripts/slice_search.py --run-dir "$RUN" --topic "$TOPIC" --max-retrieval-usd 1
python3 scripts/evidence_gate.py --run-dir "$RUN"        # must exit 0 before synthesis
python3 scripts/fetch_fulltext.py --run-dir "$RUN"       # download full text of every source (keep longest; save raw)
python3 scripts/citation_chase.py --run-dir "$RUN" --topic "$TOPIC"   # one-hop citation-graph fill, re-gate (fail-closed: 0 ran · 40 OpenAlex unreachable · 41 no resolvable seeds · 22 still-thin → surface, don't proceed as if expansion succeeded)
python3 scripts/coverage_audit.py --run-dir "$RUN" --topic "$TOPIC"   # name + fill expected-but-absent coverage, re-gate
python3 scripts/fetch_fulltext.py --run-dir "$RUN"       # SECOND pass: pull full text for the gap-slice sources the audit just added (fail-open · $0 · never ledgered)
# → Round 2 synthesis subagent (emit the six EXACT headers) → round2/synthesis.md
python3 scripts/deepen_questions.py --run-dir "$RUN" --round2-file "$RUN/round2/synthesis.md"
# → Round 3 integration → sections/ → Round 4:
python3 scripts/verify_citations.py "$RUN/sections/" --output "$RUN/round4/citation-verification.md" --check-urls
python3 scripts/lint_background.py "$RUN/sections/"      # numeric tripwire inside fenced editorial blocks
# → refute adversary (non-anthropic family) → fix pass → export.
```

## Execution Checklist

When `/deep-research [topic]` is invoked:

0. **Stage 0 framing** — unless the ask is already well-framed or the user passed a
   skip signal, run ONE `AskUserQuestion` (≤3 questions: umbrella question,
   sub-questions, scope dials); reflect the confirmed framing back before any spend.
   Skip silently in headless/non-interactive runs.
1. **Round 0** — `scope.py`; capture `scope.json`.
2. **Round 1 retrieve** — run `dispatch.py` to print the runnable sequence, then
   `slice_search.py`.
3. **Evidence gate** — `evidence_gate.py`; **exit 0 required** before any synthesis
   (exit 22 → fix corpus + `--resume` + re-gate; exit 21 → raise cap, surface, resume).
4. **Citation-graph fill:** `citation_chase.py --run-dir … --topic …`; one-hop co-citation +
   citing-works expansion, then re-gate. Fail-closed exits: 0 = ran; 40 = OpenAlex unreachable;
   41 = no resolvable seeds; 22 = still thin. On any non-zero, surface the code, do NOT proceed
   as if expansion succeeded.
5. **Coverage audit (DEFAULT: the `deep-research-squad` skill):** invoke `deep-research-squad`
   on this run dir — checklist + sibling sweep + persona panel + per-gap adversarial verify,
   then fills via `slice_search.py --add-slice`, re-gate. Writes the same `round1/coverage_gaps.md`.
   FALLBACK (squad undispatchable): `coverage_audit.py --run-dir … --topic …`. Never run both.
   **Then repeat `fetch_fulltext.py --run-dir …`** — a SECOND full-text pass so the
   gap-slice sources the audit just added are pulled to full text too, not left as
   snippets (the first pass ran before the audit doubled the corpus). Fail-open, $0,
   never ledgered.
6. **Round 2 synthesis** — one subagent; emit the six EXACT headers to `round2/synthesis.md`.
7. **Round 2.5 deepening** — `deepen_questions.py --round2-file …`.
8. **Round 3 integration** — read round1 briefs + `sources/*.txt` + `round2/synthesis.md`
   + round2.5 answers; planners + parallel section subagents + `dedup_bib.py`; assemble + audit.
9. **Round 4** — `verify_citations.py --check-urls` (+ `classify_sources.py`,
   `lit_search.py --compare-bib`) → `lint_background.py research/[slug]/sections/`
   (fix/re-fence flagged blocks) → refute adversary (non-anthropic family) → fix pass.
   Wrap the verifier in the stall watchdog: `python3 scripts/watched.py --stale-secs 300 -- python3 scripts/verify_citations.py …`.
10. **Round 5 (optional)** — `slice_search --only-slice` / `deepen --single-question`; re-integrate.
11. **Export** — `export.py`, then `search.py index`.
12. **Report** — file location, stats, citation-resolution rate, and the index summary line.

## Common Failure Modes

| Failure | Prevention |
|---|---|
| Synthesis over a thin corpus | `evidence_gate.py` exit 22 blocks it; fix + resume + re-gate |
| Runaway retrieval spend | Per-run ledger + `--max-retrieval-usd`; exit 21 surfaces, never retries |
| Silent bucket loss in deepening | Round 2 MUST emit the six EXACT headers (a typo empties a bucket) |
| Fabricated / unresolvable citations | `verify_citations.py` resolves every cite against OpenAlex/Crossref |
| Dead or spoofed links | Three-state SSRF-hardened probe → `## ⚠ Unresolved links` |
| Echo-chamber review | Adversary forced onto a non-anthropic family |
| Bibliography skewed to blogs/wikis | `classify_sources.py` quality score |
| Major canonical works missing | `lit_search.py --compare-bib` |
| Partial retrieval failure | `slice_search.py --resume` skips slices whose jsonl parses |
| WebFetch hallucination | No-WebFetch rule — curl the raw page, grep/read the real text |
| Silent stall in a network script | `CappedRetry` bounds Retry-After sleeps (30s); wrap long runs in `scripts/watched.py` (kills on stale output, exit 99) |

## Stall watchdog

Any long-running network step (`slice_search.py`, `fetch_fulltext.py`,
`citation_chase.py`, `coverage_audit.py`, `verify_citations.py`) can go quiet if
a server misbehaves in a way the retry caps don't cover. `scripts/watched.py`
wraps a command, streams its output through unchanged, and kills the process
group if output goes stale, turning a silent hang into a loud exit `99`:

```bash
python3 scripts/watched.py --stale-secs 300 -- \
  python3 scripts/verify_citations.py research/[slug]/sections/ \
  --output research/[slug]/round4/citation-verification.md --check-urls
```

On exit `99`: diagnose (rate limiting? network down?), then rerun — the child's
own exit code passes through on normal completion, so the wrapper is safe as a
default for every long network step. Keep `--stale-secs` ABOVE the wrapped
command's legitimate quiet period (capped retries can be silent ~120s; the 300s
default clears that). The watchdog signal is output *freshness*, never content:
a healthy-looking progress counter with a stale mtime IS the stall signature.
