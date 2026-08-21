# deep-research

Five research strategies run in parallel — each by a provider you configure — with domain scoping, adversarial cross-validation, and mechanical citation verification. A Claude Code skill that runs five agent types (academic, practitioner, real-time, grey-literature, contrarian) against the same topic with differentiated strategies, compares their outputs adversarially, integrates them by topic section, verifies every citation against OpenAlex/Crossref, and runs an optional iterative deepening pass. Produces a fact-checked, fully-cited "Research Bible" plus BibTeX and a machine-readable claims file.

## What it does

Most LLM research is one model, one pass, hallucinated citations. This is five research strategies in parallel — each via a configured provider — six rounds, mechanical citation resolution, and budget-aware execution.

```
Round 0  Domain scoping — classify topic, propose source priorities
Round 1  Five agent types research in parallel — each with a different strategy
         ├─ academic         (default: Claude)      → Academic deep dive (journals, NBER, SSRN)
         ├─ practitioner     (default: ChatGPT)     → Practitioner & explainer (industry, methodology)
         ├─ real-time        (default: Perplexity)  → Real-time web (current news, live citations)
         ├─ grey-literature  (default: Gemini)      → Grey literature & primary sources (govt, IGO, treaties)
         └─ contrarian       (default: Grok)        → Contrarian & cross-disciplinary (dissent, outside views)
Round 2  Adversarial comparison + citation-laundering detection + completeness map
Round 3  Three section planners + reconciler → parallel integration agents
Round 4  Mechanical citation verification (Crossref/OpenAlex) + source tier audit
         + missing-literature check + adversarial fact-check + fix pass
Round 5  (optional) Iterative deepening on weak sections, cap 2 iterations
Index    Refresh a project-wide semantic index over every topic's Bible (bundled)
Output   Hub-and-spoke Research Bible + BibTeX + claims.jsonl + provenance
         + searchable semantic index spanning every topic in the project
```

## What's new vs. a one-shot LLM

- **Domain scoping** — classifies topic before Round 1, injects domain-specific source priorities (PubMed for medicine, NBER for economics, arXiv for tech, etc.)
- **Date stamping** — every time-sensitive claim carries `[as of: <date>]`
- **Confidence tagging** — high-stakes claims carry `[confidence: high/medium/low]`
- **Cross-model support tags** — `[4/5 support]` shows how many models agree on a claim
- **Citation-laundering detection** — flags when N models cite the same secondary source as if it were N confirmations
- **Mechanical citation verification** — resolves every `[Author, Year]` against OpenAlex and Crossref (free, no key)
- **Source tier audit** — scores bibliography quality (peer-reviewed vs blog vs wiki)
- **Missing-literature check** — compares against OpenAlex top-N to flag canonical works absent from the bibliography
- **Multi-language search** — `--languages en,fr,de,zh` finds non-English primary sources
- **Cost gate + resume** — pre-flight estimate, `--max-cost-usd` hard cap, `--resume` recovers from partial failure
- **BibTeX + JSONL export** — machine-readable downstream consumption
- **Bundled semantic search** — the engine is bundled in this repo (`vendor/semantic_search/`); after each run, one project-wide index over every topic's Bible makes the whole research library searchable by meaning. Opt-in deps (`pip install -r requirements-search.txt`); skips gracefully (exit 0 + notice) if deps or `OPENAI_API_KEY` are absent — never breaks a run.

## Install

```bash
# 1. Clone into your Claude skills directory
git clone https://github.com/nraford7/deep-research.git ~/.claude/skills/deep-research

# 2. Install Python deps
pip install -r ~/.claude/skills/deep-research/requirements.txt

# 3. Set whichever API keys you have
cp ~/.claude/skills/deep-research/.env.example ~/.env
# edit ~/.env and fill in keys

# 4. (Optional) Enable bundled semantic search over your research
pip install -r ~/.claude/skills/deep-research/requirements-search.txt
# needs OPENAI_API_KEY; without this step search just skips gracefully
```

The skill auto-detects which keys are set. Missing keys = that model skipped, no failure. **One key works. Three or more is the sweet spot. Five is maximum.**

## Use

In Claude Code:

```
/deep-research [your topic and scope]
```

The skill walks the agent through all six rounds. Or invoke the dispatcher and helper scripts directly:

```bash
# 1. Domain scoping
python3 scripts/scope.py \
  --topic "Central bank digital currencies" \
  --scope "Design, adoption, monetary-policy implications" \
  --output research/cbdc/round0/scope.md \
  --use-llm

# 2. Pre-flight cost estimate
python3 dispatch.py --topic "..." --scope "..." \
  --output-dir research/cbdc/round1/ \
  --scope-file research/cbdc/round0/scope.json \
  --estimate-only

# 3. Round 1 dispatch with budget cap
python3 dispatch.py --topic "..." --scope "..." \
  --output-dir research/cbdc/round1/ \
  --scope-file research/cbdc/round0/scope.json \
  --languages en,zh \
  --max-cost-usd 50 \
  --resume

# 4. Bibliography dedup (after Round 3 integration)
python3 scripts/dedup_bib.py research/cbdc/round1/agent-*.md \
  --output research/cbdc/sections/bibliography.md

# 5. Round 4 mechanical verification
python3 scripts/verify_citations.py research/cbdc/sections/ \
  --output research/cbdc/round4/citation-verification.md --check-urls
python3 scripts/classify_sources.py research/cbdc/sections/bibliography.md \
  --output research/cbdc/round4/tier-report.md
python3 scripts/lit_search.py --topic "CBDC monetary policy" --limit 50 \
  --compare-bib research/cbdc/sections/bibliography.md \
  --output research/cbdc/round4/missing-lit.md

# 6. Export
python3 scripts/export.py \
  --sections research/cbdc/sections/ \
  --bibliography research/cbdc/sections/bibliography.md \
  --output-dir research/cbdc/export/

# 7. Refresh the project-wide semantic index (bundled; over every topic's Bible)
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
| `ANTHROPIC_API_KEY` | Claude | https://console.anthropic.com |
| `OPENAI_API_KEY` | ChatGPT | https://platform.openai.com |
| `PERPLEXITY_API_KEY` | Perplexity Deep Research | https://www.perplexity.ai/settings/api |
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
| `scripts/cost.py` | Cost estimator with budget gate |
| `scripts/verify_citations.py` | Resolve every citation against OpenAlex + Crossref; flag unresolved, weak matches, orphans, dead URLs |
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

## Why five strategies, not one

- **Hallucination triangulation** — a fake citation rarely appears in three reports from different providers
- **Mechanical backstop** — `verify_citations.py` resolves every cite against OpenAlex/Crossref; what agents invent, the resolver catches
- **Coverage** — each agent type has a different strategy and each provider has different blind spots; cross-section completeness map exposes them
- **Citation quality** — Perplexity finds live web sources, Gemini surfaces primary documents, Claude follows academic citation chains
- **Disagreement is signal** — when providers split on a figure, that becomes a `[disputed: ...]` tag, not a silent average
- **Citation laundering caught** — Round 2 flags when N agents cite the same secondary source as if it were N confirmations

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
  else (gpt-4.x, Perplexity, Grok). You can now set a GPT-5 model as a provider without a 400.
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
