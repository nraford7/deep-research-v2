# deeper-research

Retrieval-first deep research for Codex and Claude Code. It scopes a topic, fetches a real evidence corpus with Exa search slices, and reasons only over what it found, producing a fact-checked, fully-cited "Research Bible" plus BibTeX and a machine-readable claims file. A hard evidence gate refuses thin material, and an independent adversary tries to refute the draft before you trust it.

## What it does

Most LLM research is one model, one pass, hallucinated citations. This is retrieval-first: fetch the evidence, gate on it, reason over it, never over the model's memory.

```
Round 0    Domain scoping — classify topic, propose source priorities
Round 1    Exa retrieval slices + a free OpenAlex/Semantic Scholar academic anchor
             → evidence gate: MUST pass before any synthesis (thin corpus is refused)
Round 2    Synthesis over the fetched corpus — six exact question-bucket headers
Round 2.5  Question-driven deepening — root-cause / consequence / gap (Exa deep-reasoning)
Round 3    Section planners + reconciler → slot-batched isolated integration agents + dedup bibliography
Round 4    Mechanical citation verification (Crossref/OpenAlex) + three-state SSRF-hardened
             link probe + a refute-mode adversary on a different provider family + fix pass
Round 5    (optional) Targeted rerun of a single slice or question, then re-integrate
Index      Refresh a project-wide semantic index over every topic's Bible (bundled)
Output     Hub-and-spoke Research Bible + BibTeX + claims.jsonl + provenance
             + searchable semantic index spanning every topic in the project
```

## Lineage: inspired by deep-research, rebuilt evidence-first

deeper-research began as [deep-research](https://github.com/nraford7/deep-research) and owes it the whole idea. The original runs five research strategies in parallel (academic, practitioner, real-time, grey-literature, contrarian), each driven by a different model provider, then triangulates their outputs on the principle that a fabricated citation rarely survives across three independent models. It works, and it still runs.

But its starting point is the models themselves. Each agent answers from what its provider already knows, its own model memory, and leans on web search to fill the gaps. Triangulation catches many fabrications, yet the architecture still lets a plausible-but-wrong claim through whenever several models share the same bias or lean on the same secondary source. Memory-first research inherits the model's hallucinations and the model's blind spots.

Picture a niche recent regulation. Five models trained on overlapping data, all past their cutoff, agree on a clause that was never enacted, and triangulation ratifies the fabrication precisely *because* they agree. Retrieval-first breaks that failure at the root. If no retrieved primary document contains the clause, the gate has nothing to synthesize from and the claim never reaches the page. Agreement stops being evidence; a fetched source becomes the evidence.

So we rebuilt it from the ground up around a different first principle: **fetch the evidence before you reason over it.** deeper-research (this repo, version 2) retrieves a real document corpus with Exa search slices in Round 1, refuses to synthesize until a hard evidence gate confirms the corpus is thick enough, and reasons only over what was actually fetched. A claim traces to a source that exists, because the source was retrieved first, not recalled.

### What stayed the same

The shared skill runs natively in Codex and Claude Code, producing the same deliverable (a fact-checked, fully-cited "Research Bible" plus BibTeX and a machine-readable claims file) through the same later-stage machinery:

- Round 0 domain scoping that injects source priorities per field
- Mechanical citation verification against OpenAlex and Crossref (free, no key)
- A source-tier audit, a missing-literature check, and a refute adversary forced through an independent-family policy
- Disagreement preserved as a `[disputed: ...]` tag, never a silent average
- Bundled project-wide semantic search across every Bible
- The same TOML provider configuration and the shared lineage of helper scripts

### What changed

| | deep-research (v1) | deeper-research (v2) |
|---|---|---|
| First principle | Model memory, then web search | Retrieved evidence, then reasoning |
| Round 1 | Five model agents generate reports in parallel | Exa retrieval slices fetch a real corpus |
| Hallucination defense | Cross-model triangulation (agreement) | Evidence gate plus claim-to-source traceability |
| Thin material | No gate: models paper over the gap | Hard refusal (exit 22) |
| Bias exposure | Shared across models that happen to agree | Reduced: reasoning is bound to fetched sources |
| Deepening | Iterative passes on weak sections | Root-cause / consequence / gap questions via Exa deep-reasoning |

The trade is deliberate. v1's fleet casts a wide net from model knowledge and moves fast, which still suits quick breadth-first scoping or fast-moving topics where the freshest signal outruns any index. v2 binds every claim to fetched evidence, which costs a retrieval pass but pays off wherever a confident wrong answer is expensive: the case this rebuild was made for.

See `SKILL.md` for the full architecture, prompt templates, and failure modes.

## What's new vs. a one-shot LLM

- **Retrieval-first** — Round 1 fetches a real evidence corpus with Exa search slices (plus a free OpenAlex/Semantic Scholar academic anchor); later rounds reason over the fetched evidence, not the model's memory
- **Hard evidence gate** — synthesis is refused (exit 22) unless the corpus clears minimum unique-source and non-empty-slice thresholds and every row re-validates
- **Question-driven deepening** — root-cause / consequence / gap questions (3/3/3, cap 9) chased with Exa `deep-reasoning`
- **Ledger-capped retrieval** — a per-run money ledger (default $1) pre-charges each Exa call and reconciles the actual; a cap breach exits 21, never silently retries
- **Domain scoping** — classifies topic before Round 1, injects domain-specific source priorities (PubMed for medicine, NBER for economics, arXiv for tech, etc.)
- **Date stamping** — every time-sensitive claim carries `[as of: <date>]`
- **Confidence tagging** — high-stakes claims carry `[confidence: high/medium/low]`
- **Mechanical citation verification** — resolves every `[Author, Year]` against OpenAlex and Crossref (free, no key)
- **Three-state SSRF-hardened link probe** — `--check-urls` reports unresolved / indeterminate-with-reason / truncated, never a naive dead-URL binary
- **Independent-family adversary** — a refute-mode pass uses a different provider family and also excludes OpenAI ↔ xAI because of common-corpus risk
- **Source tier audit** — scores bibliography quality (peer-reviewed vs blog vs wiki)
- **Missing-literature check** — compares against OpenAlex top-N to flag canonical works absent from the bibliography
- **BibTeX + JSONL export** — machine-readable downstream consumption
- **Bundled semantic search** — the engine is bundled in this repo (`vendor/semantic_search/`); after each run, one project-wide index over every topic's Bible makes the whole research library searchable by meaning. Opt-in deps (`pip install -r requirements-search.txt`); skips gracefully (exit 0 + notice) if deps or `OPENAI_API_KEY` are absent — never breaks a run.

## Install

```bash
# 1. Keep one canonical clone shared by both runtimes
git clone https://github.com/nraford7/deeper-research.git /Users/noahraford/Projects/deeper-research

# 2. Expose that exact clone to both skill discovery locations
ln -s /Users/noahraford/Projects/deeper-research ~/.agents/skills/deeper-research
ln -s /Users/noahraford/Projects/deeper-research ~/.claude/skills/deeper-research

# 3. Install Python dependencies once
pip install -r /Users/noahraford/Projects/deeper-research/requirements.txt

# 4. Set whichever API keys you have
cp /Users/noahraford/Projects/deeper-research/.env.example ~/.env
# edit ~/.env and fill in keys

# 5. (Optional) Enable bundled semantic search over your research
pip install -r /Users/noahraford/Projects/deeper-research/requirements-search.txt
# needs OPENAI_API_KEY; without this step search just skips gracefully
```

Retrieval needs `EXA_API_KEY`. The skill auto-detects subscription providers only when
the matching active-host marker and CLI are both present; a missing LLM key narrows
direct-call options. Unless `[defaults].synthesis` explicitly selects a configured
**Round 2** executor, Codex or Claude Code uses that host's native subscription-backed
subagent for synthesis. Coverage work, integration, and fixes remain host-native.

## Use

In Codex:

```
$deeper-research [your topic and scope]
```

In Claude Code:

```
/deeper-research [your topic and scope]
```

The skill walks the agent through every round. Or invoke the dispatcher and helper scripts directly (retrieval-first — Round 1 fetches the corpus, the gate must pass before synthesis):

```bash
RUN=research/cbdc
TOPIC="Central bank digital currencies"

# 1. Domain scoping
python3 scripts/scope.py --topic "$TOPIC" \
  --scope "Design, adoption, monetary-policy implications" \
  --output "$RUN/scope.json" --use-llm

# 2. Print the Round-1 command sequence (dispatch.py is a slices-only guide, not a runner)
python3 dispatch.py --topic "$TOPIC" \
  --scope "Design, adoption, monetary-policy implications" \
  --run-dir "$RUN" --max-retrieval-usd 1

# 3. Round 1 retrieval — Exa slices + free academic anchor
python3 scripts/slice_search.py --run-dir "$RUN" --topic "$TOPIC" --max-retrieval-usd 1

# 4. Evidence gate — MUST exit 0 before any synthesis (exit 22 = thin corpus)
python3 scripts/evidence_gate.py --run-dir "$RUN"

# 4a. Citation chase: one-hop citation-graph fill (co-citation + citing works),
#     then re-gate. Automatically cascades OpenAlex → Semantic Scholar. If both
#     fail it returns 0 in explicit degraded mode; inspect citation_chase_status.json
#     and carry graph_verified=false into the final report. Exit 22 remains fail-closed.
python3 scripts/citation_chase.py --run-dir "$RUN" --topic "$TOPIC"

# 4b. Coverage audit: name + fill expected-but-absent coverage, then re-gate.
#     DEFAULT is the bundled squad procedure (references/squad-audit.md), which the
#     SKILL dispatches as subagents inline; the script below is the single-model
#     FALLBACK for when subagents aren't available.
#     Exit 0 = coverage verified; a NONZERO exit (30 no provider, 31 LLM error,
#     32 bad JSON, 21 cap, 22 still thin) means coverage is UNVERIFIED: do NOT
#     proceed to synthesis, surface and resolve it.
python3 scripts/coverage_audit.py --run-dir "$RUN" --topic "$TOPIC"   # fallback

# → Round 2 synthesis subagent → $RUN/round2/synthesis.md (six exact headers)

# 5. Round 2.5 deepening — root-cause / consequence / gap
python3 scripts/deepen_questions.py --run-dir "$RUN" --round2-file "$RUN/round2/synthesis.md"

# → Round 3 integration → $RUN/sections/

# 6. Bibliography dedup (after integration)
python3 scripts/dedup_bib.py "$RUN"/round1/brief_*.md \
  --output "$RUN/sections/bibliography.md"

# 7. Round 4 mechanical verification (+ three-state link probe) → then a refute adversary
python3 scripts/verify_citations.py "$RUN/sections/" \
  --output "$RUN/round4/citation-verification.md" --check-urls
python3 scripts/classify_sources.py "$RUN/sections/bibliography.md" \
  --output "$RUN/round4/tier-report.md"
python3 scripts/lit_search.py --topic "CBDC monetary policy" --limit 50 \
  --compare-bib "$RUN/sections/bibliography.md" \
  --output "$RUN/round4/missing-lit.md"
# Background-block lint: numeric tripwire inside fenced editorial blocks; exit 0 = clean,
# exit 1 = a fenced block names a quantity (fix/re-fence it) before the refute adversary.
python3 scripts/lint_background.py "$RUN/sections/"

# 8. Export + refresh the project-wide semantic index (bundled; over every topic's Bible)
python3 scripts/export.py --sections "$RUN/sections/" \
  --bibliography "$RUN/sections/bibliography.md" --output-dir "$RUN/export/"
python3 scripts/search.py index
```

### Searching what you've researched

Semantic search is **bundled** — no separate install. Add the optional deps once:

```bash
pip install -r requirements-search.txt   # sqlite-vec + apsw; openai is already in base
```

Then search the whole research library by meaning (the wrapper targets
`research/.semantic-index.db` for you):

```bash
python3 scripts/search.py "central bank digital currency risks"
python3 scripts/search.py "CBDC risks" --topic cbdc    # scope to one topic
```

Re-running `$deeper-research` (Codex) or `/deeper-research` (Claude Code) on the same topic updates that topic's entries in
place; a new sub-topic just joins the index. Both are incremental — only changed
sections re-embed. If the deps or `OPENAI_API_KEY` are missing, indexing/search
print a one-line notice and exit 0 — core research is unaffected.

## Model selection by role (cost)

Only two rounds need a top-tier model; the rest are bounded or mechanical. The
subagent dispatch accepts a `model:` override, so pick per role instead of running the
whole pipeline on one model (the **Tier 1 hybrid**):

| Role | Model |
|---|---|
| Synthesis (Round 2) | strongest available — sets the field-map frame; a single call |
| Integration sections (Round 3) | strongest available — the prose you read, and the biggest token sink |
| Squad audit, fix-pass, scoping, glue | balanced/cheaper |
| Refute adversary (Round 4) | independent family; OpenAI ↔ xAI is excluded |

Use the active runtime's balanced/cheaper role for orchestration and elevate synthesis +
integration to its strongest available role. An explicit `[defaults].synthesis` instead
selects the configured Round-2 executor; its family governs the adversary. Codex and
Claude aliases remain host-specific; the shared policy never assumes one vendor's names.
Full table and the headless note: *Model selection by role* in `SKILL.md`.

## Batch: parallel headless runs over many questions

You don't need a session per question. Point the pipeline at a **list of questions** —
a chapter of research questions, or a file with one question per line — and fan out one
headless session per question. Each is fully isolated (its own context, Exa ledger, and
`research/<slug>/` folder), bounded by an operator-set ceiling and responsive to
observed host or API capacity pressure.

Use the tested adaptive runner instead of hand-tuning a fixed shell fan-out. It starts
at two workers, adds one worker after each healthy 60-second window, and stops at eight:

```bash
python3 "$HOME/.agents/skills/deeper-research/scripts/batch_research.py" \
  questions.txt --adapter codex --output-root research
```

On a failed worker that reports a capacity/concurrency error or HTTP 429, the runner
halves the target and pauses new launches for 120 seconds. Active research is never
killed, and ordinary evidence/budget failures are never retried automatically. Override
the policy only when needed:

```bash
python3 "$HOME/.agents/skills/deeper-research/scripts/batch_research.py" \
  questions.txt --adapter codex --initial-concurrency 2 --max-concurrency 8 \
  --stability-window 60 --backoff-seconds 120
```

Use `--dry-run` to inspect every pending command without launching workers. Completed
directories containing `RESEARCH-BIBLE.md` are skipped; distinct questions with similar
names receive stable hashed directories. Logs live under `research/_batch/logs/`.

Claude uses the same scheduler but still requires a pre-approved containment wrapper:

```bash
python3 "$HOME/.claude/skills/deeper-research/scripts/batch_research.py" \
  questions.txt --adapter claude --output-root research \
  --claude-runner "/absolute/path/to/contained-runner"
```

The wrapper receives the complete `claude` invocation as arguments plus
`DEEPER_RESEARCH_SKILL_ROOT` and `DEEPER_RESEARCH_RUN_DIR` in its environment. It must
mount the skill root read-only, make only the run directory writable, and then execute
the supplied command. `--allowedTools` alone does not path-scope Claude writes.

Monitor with `tail -f research/_batch/logs/*.log` or run the script with `--help` for all
controls. Cost still scales linearly (~$0.5–0.8 Exa per question plus LLM legs); adaptive
concurrency changes throughput, not per-question budgets or evidence gates.

## API keys

| Env var | Purpose | Get a key |
|---|---|---|
| `EXA_API_KEY` | Exa retrieval (Round 1 slices + Round 2.5 deepening) — **required** | https://dashboard.exa.ai |
| `ANTHROPIC_API_KEY` | Claude | https://console.anthropic.com |
| `OPENAI_API_KEY` | ChatGPT | https://platform.openai.com |
| `GOOGLE_API_KEY` | Gemini | https://aistudio.google.com/apikey |
| `XAI_API_KEY` | Grok | https://console.x.ai |
| `SEMANTIC_SCHOLAR_KEY` | Optional — raises rate limit on `lit_search.py` | https://www.semanticscholar.org/product/api |
| `CONTACT_EMAIL` | Optional — joins OpenAlex/Crossref "polite pool" | — |

The dispatcher reads from `~/.env` and `./.env` automatically. Or export them in your shell.

Providers can also be defined in TOML for arbitrary OpenAI-compatible endpoints (`api_type = "openai"` with a `base_url`) — DeepSeek direct, OpenRouter, Fireworks, xAI, and similar services all work this way. Copy `config.toml.example` to `./deeper-research.toml` or `~/.config/deeper-research/config.toml` and fill in inline keys. TOML config augments env keys — built-in providers still activate from env vars. Both TOML paths are gitignored.

`config.py` is the single control point for provider resolution in the shipped scripts
(Round 0 + Round 1). The optional `[defaults]` TOML table lets you name a provider for
one-off calls: `[defaults].utility` controls which provider `scope.py --use-llm` uses
for Round 0 scoping. An explicit `[defaults].synthesis` selects the provider that
actually runs Round 2; when absent, the active host's native subagent is used. Every
nonempty explicit role selection is authoritative: if the named provider is
unavailable, configuration errors instead of silently choosing another executor. The
actual synthesis executor's family governs adversary selection. Families must differ,
and OpenAI ↔ xAI is additionally excluded in either direction; Grok still retains its
accurate `xai` family metadata.

Providers can also be local CLI tools (`api_type = "cli"`) — `codex-sub` and
`claude-sub` are auto-detected only when their active-host marker and matching CLI are
present, and explicit TOML entries may override either. Those host-native providers can
use the corresponding subscription; generic configured CLIs may be metered or use other
credentials and must not be described as subscription-backed. `codex-sub` uses a
text-only `codex exec` call with `--ephemeral`, `--sandbox read-only`, and
`--skip-git-repo-check`; Claude uses `claude -p` with explicit least-privilege allowed
tools. Codex direct calls remove `OPENAI_API_KEY`, `CODEX_API_KEY`,
`CODEX_ACCESS_TOKEN`, and `OPENAI_BASE_URL` from the child environment so stored
subscription authentication cannot be replaced by API routing. Codex `extra_args` are
limited to `--strict-config` and `--color {auto,always,never}`; set the model through
the provider's `model` field. Claude `extra_args` remain unchanged. To enable live web
search on a Claude CLI provider, set
`extra_args = ["--allowedTools", "WebSearch", "Bash(curl:*)"]` (search + curl-only
fetching; no Edit/Write, no other shell commands — WebFetch is banned pipeline-wide
because it returns an AI summary of the page rather than raw text) and add
`capabilities = ["web_search"]`. Full pipeline writes require the contained-runner
boundary described in the batch recipe. See `config.toml.example` for the full syntax
and host-specific examples.

> **OpenRouter vs direct APIs:** OpenRouter's value is reaching model lineages you can't get direct (Kimi, GLM, Microsoft, etc.). For models available direct (DeepSeek, Anthropic), the provider's own API is cheaper. All are `api_type = "openai"` providers with a `base_url`.

> **Model-ID drift warning:** Provider model IDs change over time (e.g. DeepSeek legacy IDs `deepseek-chat`/`deepseek-reasoner` retire 2026-07-24). Always verify current IDs on the provider's site. `max_tokens` must not exceed each model's output cap.

**Free APIs (no key required):** OpenAlex, Crossref, Semantic Scholar (low rate).

For adaptive headless batches, `scripts/batch_research.py` accepts either one question
per line or `existing-slug<TAB>question` to resume exact named run directories.

## Helper scripts

| Script | Purpose |
|---|---|
| `scripts/scope.py` | Domain classification + source priority recommendations (rule-based + optional configured LLM) |
| `scripts/slice_search.py` | Round-1 retrieval: Exa search slices + a free academic anchor; ledger-capped; writes jsonl briefs + manifest |
| `scripts/evidence_gate.py` | Refuse synthesis over a thin corpus — exit 0 if thick enough and every row re-validates, else exit 22 |
| `scripts/citation_chase.py` | Post-gate one-hop citation-graph fill with a tested OpenAlex → Semantic Scholar cascade. If both providers fail, writes `citation_chase_status.json` with `graph_verified=false` and continues in explicit degraded mode; exit 22 remains fail-closed. |
| `scripts/coverage_audit.py` | Single-model **fallback** coverage auditor (the default is the bundled squad procedure in `references/squad-audit.md`): name expected-but-absent coverage, fill each gap with a scope-bounded Exa slice, re-gate. Fail-closed: exit 0 = coverage verified, nonzero (30/31/32/21/22) = unverified, do not synthesize |
| `scripts/lint_background.py` | Round-4 numeric tripwire inside fenced editorial blocks: exit 0 clean, exit 1 if a fenced block names a quantity |
| `scripts/deepen_questions.py` | Round 2.5 deepening: root-cause / consequence / gap questions answered with Exa deep-reasoning |
| `scripts/cost.py` | Cost estimator + retrieval fee table |
| `scripts/verify_citations.py` | Resolve every citation against OpenAlex + Crossref; flag unresolved / weak matches / orphans; `--check-urls` runs a three-state SSRF-hardened link probe |
| `scripts/dedup_bib.py` | DOI-normalized + fuzzy-title bibliography merge with audit log |
| `scripts/classify_sources.py` | Tier classifier (peer-reviewed / institutional / book / news / blog / wiki) + quality score |
| `scripts/lit_search.py` | Query OpenAlex + Semantic Scholar; optionally compare against finished bibliography to flag missing canonical works |
| `scripts/export.py` | Emit BibTeX (`bibliography.bib`) + JSONL (`claims.jsonl`) from final Bible |
| `scripts/search.py` | Bundled semantic search: `index` builds the project-wide index over all Bibles; a positional query searches it (`--topic` scopes to one topic). Skips gracefully without deps/key. |

## Output

```
research/
├── .semantic-index.db             ← Project-wide semantic index (all topics; git-ignored)
└── <topic-slug>/
    ├── README.md                  ← The hub: index, exec summary, key findings
    ├── sections/
    │   ├── 01-<name>.md           ← Integrated topic sections (each 8k–20k words)
    │   ├── 02-<name>.md
    │   └── bibliography.md        ← Deduplicated master bibliography
    ├── export/
    │   ├── bibliography.bib       ← BibTeX
    │   └── claims.jsonl           ← Inline citations with surrounding sentence
    ├── round4/
    │   ├── citation-verification.md  ← Mechanical OpenAlex/Crossref resolution
    │   ├── tier-report.md            ← Source quality breakdown
    │   ├── missing-lit.md            ← Canonical works absent
    │   ├── factcheck-*.md            ← Adversarial fact-check reports
    │   └── fix-log.md
    └── round0..round5/            ← Provenance preserved
```

## Tests

Minimal regression tests for the parser functions (citation regex, bibliography
parsers, dedup, BibTeX key, DOI normalization, source classification):

```bash
python3 tests/test_parsers.py
# or
python3 -m pytest tests/
```

## Recent reliability fixes (2026-06)

Hardening from a large real run (a ~28k-word, 130+-source Research Bible):

- **Long Anthropic reports stream.** `_complete_anthropic` now uses the streaming API, so
  large-`max_tokens` reports no longer trip the SDK's 10-minute non-streaming guard (which
  previously killed the academic agent outright).
- **GPT-5 / o-series support.** `_complete_openai` sends `max_completion_tokens` for
  `gpt-5*`/`o1`/`o3`/`o4` models (which reject `max_tokens`) and `max_tokens` for everything
  else (gpt-4.x, Grok). You can now set a GPT-5 model as a provider without a 400.
- **Bibliography parser is format-tolerant.** `extract_bibliography` (used by
  `verify_citations.py` and `dedup_bib.py`) now matches any heading that *contains*
  "bibliography/references/works cited/sources" (e.g. `# Master Bibliography`), keeps deeper
  `### Category` subheadings inside the bibliography instead of truncating at the first one,
  and drops inner heading lines before splitting entries. Previously a categorized
  LLM-generated bibliography parsed as a single entry, falsely orphaning every inline cite.
- **Richer source-tier heuristics.** `classify_sources.py` recognizes arXiv/preprints (own
  tier), the major NLP/ML/HCI venues (EMNLP, ACL, NAACL, NeurIPS, ICLR, ICML, AAAI, CHI, COLM,
  TACL, the ACL Anthology) and journals (PNAS, Science Advances, EPJ Data Science, PLOS, JAIR,
  Cognitive Science, etc.), plus broader book-publisher patterns ("University of X Press" and
  common scholarly/trade imprints) — so a scholarly corpus no longer scores as low-tier.
- **`lit_search.py` null-safety.** Guards a null OpenAlex `primary_location.source` that raised
  `AttributeError` and aborted the missing-literature check.
- **Stale model ID.** Provider model identifiers drift and can retire. Verify the
  current ID or host alias, then override that provider in TOML if needed.

## License

MIT — see `LICENSE`.

## Credits

Built for use inside Codex and Claude Code as a shared skill. Adapt freely.
