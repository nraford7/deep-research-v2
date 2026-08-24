---
name: deep-research
description: Use when the user needs comprehensive, fact-checked, evidence-based research on any topic. Triggers on requests for deep research, literature reviews, comprehensive reports, or evidence-based analysis. Runs domain scoping, Exa retrieval slices with an evidence gate, question-driven deepening, synthesis, integration, mechanical citation verification, and a grounded adversary pass to produce a single authoritative reference document.
---

# Deep Research

Retrieval-first deep research. Round 1 fetches a real evidence corpus with Exa
search slices (plus a free OpenAlex/Semantic Scholar academic anchor), a hard
**evidence gate** refuses to synthesize over a thin corpus, and every later round
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
Round 0    SCOPE            scope.py → scope.json (domain + source priorities)
              ↓
Round 1    RETRIEVE         slice_search.py → Exa slices (full text) + academic anchor
              ↓             fetch_fulltext.py → read PDFs/pages Exa left thin (incl. OA papers)
              ↓             evidence_gate.py → MUST pass (exit 0) before any synthesis
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

**Step 1.4 — full-text fill (recommended).** Some sources come back thin: the
academic anchor is metadata-only, and Exa does not always render every PDF-backed
white paper or report. This pass reads those documents directly — resolving an
**open-access PDF via OpenAlex** for DOI/academic rows, fetching plain PDFs and
pages as-is — then extracts the text (pypdf for PDFs, tag-strip for HTML) into the
same `round1/sources/` store and updates each thin row's `text_path`/`text_chars`.

```bash
python3 scripts/fetch_fulltext.py \
  --run-dir research/[slug] \
  --min-chars 400        # rows with fewer stored chars get a direct-fetch attempt
```

Every fetch goes through the SSRF-hardened, IP-pinned path reused from
`verify_citations` (redirects re-vetted per hop); per-source **fail-open**; `$0` —
never ledgered; **no WebFetch** (raw bytes read directly). Writes
`round1/fulltext_manifest.json` (attempted / fetched / by-method / failures).
Requires `pypdf` (in `requirements.txt`); without it, PDF rows skip gracefully.

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

Read the Round-1 briefs + the Round-2.5 answers and integrate by topic section:
dispatch section-planner subagents + a reconciler, then **one integration subagent per
section** in parallel (each ≤ ~40k words of input). Preserve every citation, every
`[as of: <date>]` and `[confidence: …]` tag, and every unique finding; present
differing figures as `[disputed: …]`, never a silent average.

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

**Step 4.2 — refute-mode adversary (a DIFFERENT provider family).** Dispatch one
adversary subagent whose job is to *refute* the draft — find unsupported claims, weak
attributions, and figures the corpus does not support. It must run on a provider whose
`family` differs from the synthesizer's (`anthropic`) so the critique is genuinely
independent. Selection walks the configured `adversary` chain (default
`grok → chatgpt → gemini`) and picks the first configured, available provider whose
family ≠ `anthropic`; if none qualify it warns and falls back to the synthesizer (see
[below](#provider-family--adversary-selection)). Feed the adversary the section files
plus `round4/citation-verification.md`; write its report to `round4/factcheck.md`.

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
python3 scripts/fetch_fulltext.py --run-dir "$RUN"       # read PDFs/OA papers Exa left thin
# → Round 2 synthesis subagent (emit the six EXACT headers) → round2/synthesis.md
python3 scripts/deepen_questions.py --run-dir "$RUN" --round2-file "$RUN/round2/synthesis.md"
# → Round 3 integration → sections/ → Round 4:
python3 scripts/verify_citations.py "$RUN/sections/" --output "$RUN/round4/citation-verification.md" --check-urls
# → refute adversary (non-anthropic family) → fix pass → export.
```

## Execution Checklist

When `/deep-research [topic]` is invoked:

1. **Round 0** — `scope.py`; capture `scope.json`.
2. **Round 1 retrieve** — run `dispatch.py` to print the runnable sequence, then
   `slice_search.py`.
3. **Evidence gate** — `evidence_gate.py`; **exit 0 required** before any synthesis
   (exit 22 → fix corpus + `--resume` + re-gate; exit 21 → raise cap, surface, resume).
4. **Round 2 synthesis** — one subagent; emit the six EXACT headers to `round2/synthesis.md`.
5. **Round 2.5 deepening** — `deepen_questions.py --round2-file …`.
6. **Round 3 integration** — planners + parallel section subagents + `dedup_bib.py`; assemble + audit.
7. **Round 4** — `verify_citations.py --check-urls` (+ `classify_sources.py`,
   `lit_search.py --compare-bib`) → refute adversary (non-anthropic family) → fix pass.
8. **Round 5 (optional)** — `slice_search --only-slice` / `deepen --single-question`; re-integrate.
9. **Export** — `export.py`, then `search.py index`.
10. **Report** — file location, stats, citation-resolution rate, and the index summary line.

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
