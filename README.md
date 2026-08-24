# deep-research

Retrieval-first deep research — a real evidence corpus fetched with Exa search slices, a hard evidence gate that refuses to synthesize over thin material, question-driven deepening, mechanical citation verification, and a different-provider adversary. A Claude Code skill that scopes the domain, retrieves a fetched corpus (Exa slices + a free academic anchor), synthesizes over that evidence, chases root-cause / consequence / gap questions, integrates by topic section, verifies every citation against OpenAlex/Crossref, and lets an independent adversary try to refute the draft. Produces a fact-checked, fully-cited "Research Bible" plus BibTeX and a machine-readable claims file.

## What it does

Most LLM research is one model, one pass, hallucinated citations. This is retrieval-first: fetch the evidence, gate on it, reason over it — never over the model's memory.

```
Round 0    Domain scoping — classify topic, propose source priorities
Round 1    Exa retrieval slices + a free OpenAlex/Semantic Scholar academic anchor
             → evidence gate: MUST pass before any synthesis (thin corpus is refused)
Round 2    Synthesis over the fetched corpus — six exact question-bucket headers
Round 2.5  Question-driven deepening — root-cause / consequence / gap (Exa deep-reasoning)
Round 3    Section planners + reconciler → parallel integration agents + dedup bibliography
Round 4    Mechanical citation verification (Crossref/OpenAlex) + three-state SSRF-hardened
             link probe + a refute-mode adversary on a different provider family + fix pass
Round 5    (optional) Targeted rerun of a single slice or question, then re-integrate
Index      Refresh a project-wide semantic index over every topic's Bible (bundled)
Output     Hub-and-spoke Research Bible + BibTeX + claims.jsonl + provenance
             + searchable semantic index spanning every topic in the project
```

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
- **Different-family adversary** — a refute-mode pass forced onto a provider family that differs from the synthesizer's
- **Source tier audit** — scores bibliography quality (peer-reviewed vs blog vs wiki)
- **Missing-literature check** — compares against OpenAlex top-N to flag canonical works absent from the bibliography
- **BibTeX + JSONL export** — machine-readable downstream consumption
- **Bundled semantic search** — the engine is bundled in this repo (`vendor/semantic_search/`); after each run, one project-wide index over every topic's Bible makes the whole research library searchable by meaning. Opt-in deps (`pip install -r requirements-search.txt`); skips gracefully (exit 0 + notice) if deps or `OPENAI_API_KEY` are absent — never breaks a run.

## Install

```bash
# 1. Clone into your Claude skills directory
git clone https://github.com/nraford7/deep-research-v2.git ~/.claude/skills/deep-research

# 2. Install Python deps
pip install -r ~/.claude/skills/deep-research/requirements.txt

# 3. Set whichever API keys you have
cp ~/.claude/skills/deep-research/.env.example ~/.env
# edit ~/.env and fill in keys

# 4. (Optional) Enable bundled semantic search over your research
pip install -r ~/.claude/skills/deep-research/requirements-search.txt
# needs OPENAI_API_KEY; without this step search just skips gracefully
```

Retrieval needs `EXA_API_KEY`. The skill auto-detects which LLM keys are set and only calls providers you've configured; a missing LLM key just narrows the reasoning options — synthesis and integration prefer the Claude Code session's own $0 subagent when available.

## Use

In Claude Code:

```
/deep-research [your topic and scope]
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
#     then re-gate. Exit 0 = ran (expanded or nothing new); a NONZERO exit (40
#     OpenAlex unreachable, 41 no resolvable seeds, 22 still thin) means it could
#     not complete: do NOT proceed as if expansion succeeded, surface and resolve.
python3 scripts/citation_chase.py --run-dir "$RUN" --topic "$TOPIC"

# 4b. Coverage audit: name + fill expected-but-absent coverage, then re-gate.
#     Exit 0 = coverage verified; a NONZERO exit (30 no provider, 31 LLM error,
#     32 bad JSON, 21 cap, 22 still thin) means coverage is UNVERIFIED: do NOT
#     proceed to synthesis, surface and resolve it.
python3 scripts/coverage_audit.py --run-dir "$RUN" --topic "$TOPIC"

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

Re-running `/deep-research` on the same topic updates that topic's entries in
place; a new sub-topic just joins the index. Both are incremental — only changed
sections re-embed. If the deps or `OPENAI_API_KEY` are missing, indexing/search
print a one-line notice and exit 0 — core research is unaffected.

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

Providers can also be defined in TOML for arbitrary OpenAI-compatible endpoints (`api_type = "openai"` with a `base_url`) — DeepSeek direct, OpenRouter, Fireworks, xAI, and similar services all work this way. Copy `config.toml.example` to `./deep-research.toml` or `~/.config/deep-research/config.toml` and fill in inline keys. TOML config augments env keys — built-in providers still activate from env vars. Both TOML paths are gitignored.

`config.py` is the single control point for provider resolution in the shipped scripts (Round 0 + Round 1). The optional `[defaults]` TOML table lets you name a provider for one-off calls: `[defaults].utility` controls which provider `scope.py --use-llm` uses for Round 0 scoping — including a subscription provider at $0 per call — instead of a hardcoded API key.

Providers can also be local CLI tools (`api_type = "cli"`) — for example `claude -p` or `codex exec` — which authenticate via your SSO subscription (Claude Pro/Max, ChatGPT) with no per-token API cost. To enable live web search on a `claude` cli provider, set `extra_args = ["--allowedTools", "WebSearch", "Bash(curl:*)"]` (search + curl-only fetching; no Edit/Write, no other shell commands — WebFetch is banned pipeline-wide because it returns an AI summary of the page rather than the raw text) and add `capabilities = ["web_search"]` — this makes it eligible for the `real-time` agent type at **$0 API cost**. `--dangerously-skip-permissions` also works but additionally enables Bash/Edit/Write; avoid it for unattended subprocesses. See `config.toml.example` for the full syntax and a diverse multi-provider example.

> **OpenRouter vs direct APIs:** OpenRouter's value is reaching model lineages you can't get direct (Kimi, GLM, Microsoft, etc.). For models available direct (DeepSeek, Anthropic), the provider's own API is cheaper. All are `api_type = "openai"` providers with a `base_url`.

> **Model-ID drift warning:** Provider model IDs change over time (e.g. DeepSeek legacy IDs `deepseek-chat`/`deepseek-reasoner` retire 2026-07-24). Always verify current IDs on the provider's site. `max_tokens` must not exceed each model's output cap.

**Free APIs (no key required):** OpenAlex, Crossref, Semantic Scholar (low rate).

## Helper scripts

| Script | Purpose |
|---|---|
| `scripts/scope.py` | Domain classification + source priority recommendations (rule-based + optional Claude) |
| `scripts/slice_search.py` | Round-1 retrieval: Exa search slices + a free academic anchor; ledger-capped; writes jsonl briefs + manifest |
| `scripts/evidence_gate.py` | Refuse synthesis over a thin corpus — exit 0 if thick enough and every row re-validates, else exit 22 |
| `scripts/citation_chase.py` | Post-gate one-hop citation-graph fill: backward co-citation + a small forward citing-works pass, deduped against the corpus, written to `slice_citation.jsonl`, then re-gated. Fail-closed: exit 0 = ran (expanded or nothing new), nonzero (40 OpenAlex unreachable, 41 no resolvable seeds, 22 still thin) = could not complete, do not proceed as if expansion succeeded |
| `scripts/coverage_audit.py` | Post-gate coverage auditor: name expected-but-absent coverage, fill each gap with a scope-bounded Exa slice, re-gate. Fail-closed: exit 0 = coverage verified, nonzero (30/31/32/21/22) = unverified, do not synthesize |
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

## Why retrieval-first, not memory-first

- **Fetched evidence, not recall** — Round 1 retrieves a real corpus with Exa slices + a free academic anchor; synthesis reasons over what was fetched, so a claim traces to a source that actually exists
- **Refusal beats confident thin answers** — the evidence gate blocks synthesis over a thin corpus (exit 22) instead of letting a model paper over the gap
- **Mechanical backstop** — `verify_citations.py` resolves every cite against OpenAlex/Crossref; what a draft invents, the resolver catches
- **Chase the questions, not just the topic** — deepening splits root-cause / consequence / gap questions and answers each with targeted Exa deep-reasoning
- **Independent critique** — the refute-mode adversary runs on a provider family that differs from the synthesizer's, so the review is not an echo chamber
- **Disagreement is signal** — differing figures become a `[disputed: ...]` tag, never a silent average

See `SKILL.md` for the full architecture, prompt templates, and failure modes.

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
- **Stale default model ID.** The built-in `claude` provider's retired
  `claude-opus-4-20250514` (now 404) is updated to a current Opus. Model IDs still drift —
  override per provider in TOML.

## License

MIT — see `LICENSE`.

## Credits

Built for use inside Claude Code as a slash-command skill. Adapt freely.
