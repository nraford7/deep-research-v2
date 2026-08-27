# Codex and Claude Compatibility Design

## Goal

Make `deeper-research` a single shared skill that uses the invoking host's subscription-backed agents for ordinary reasoning: Codex agents and `codex exec` under Codex, Claude agents and `claude -p` under Claude Code.

## Installation

The canonical clone will live at `/Users/noahraford/Projects/deeper-research`. Both runtime discovery locations will point to it:

- `/Users/noahraford/.agents/skills/deeper-research` for Codex.
- `/Users/noahraford/.claude/skills/deeper-research` for Claude Code.

The existing Claude checkout is clean and matches upstream commit `3e7a703`. It will be moved, not deleted or recloned, so its Git history and remote remain intact.

## Runtime Selection

`config.py` will detect the active host from environment markers. Codex markers take precedence when both families of variables are present. The result is one of `codex`, `claude`, or `None`.

When the corresponding CLI is installed, configuration loading will expose one implicit subscription provider:

- `codex-sub`: `api_type = "cli"`, `command = "codex"`, `family = "openai"`.
- `claude-sub`: `api_type = "cli"`, `command = "claude"`, `family = "anthropic"`.

Explicit TOML providers may override these names. A nonempty explicit `[defaults]` selection remains authoritative: if its named provider is unavailable, configuration fails rather than selecting a fallback. Otherwise, one-off utility calls prefer the active host's subscription provider before metered API providers.

## Native Orchestration

The skill will load `references/runtime-adapters.md` at invocation and select exactly one host adapter.

- Codex uses native Codex subagents for the coverage panel, synthesis, integration, and fix pass. It batches work when the host concurrency limit is smaller than the requested fan-out.
- Claude uses the existing Agent/subagent behavior.
- Interactive questions use the host's supported input mechanism; ordinary chat questions are the fallback. Headless runs skip interactive framing.
- Model policy is expressed by role (`strongest available`, `balanced/cheaper`, and `independent family`) rather than hardcoded `opus` and `sonnet`. Host-specific examples may still name valid runtime commands.

Direct, script-driven LLM calls use the active host's CLI subscription provider. `codex exec` runs ephemerally, reads its prompt from stdin, skips the Git-repository requirement, and uses a read-only sandbox because these calls only return text. Codex child processes cannot inherit API/routing overrides (`OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, or `OPENAI_BASE_URL`). Runtime validation limits Codex `extra_args` to `--strict-config` and `--color {auto,always,never}`; the provider model field remains the only model override.

## Independent Adversary

Adversary selection compares provider family against the family of the actual synthesizer. Families must differ, and OpenAI ↔ xAI is additionally excluded in either direction because of common-corpus risk. Grok retains the accurate `xai` family value, but under Codex/OpenAI synthesis neither Grok nor ChatGPT qualifies; Anthropic, Google, or another independent family may qualify. Under Claude/Anthropic synthesis, the existing non-Anthropic rule remains the resulting behavior.

An unavailable independent adversary is a fail-closed condition for claiming independent review. The run may surface the missing provider and stop or clearly label a critique that fails the independence policy as non-independent, but it must not silently report the adversary invariant as satisfied.

## Scope Boundaries

- Exa remains the required retrieval engine and retains its existing ledger and charges.
- OpenAI embeddings for the optional semantic index remain API-key-backed and are not presented as subscription-backed.
- The core retrieval, evidence-gate, full-text, citation, and export algorithms are unchanged.
- No upstream push or pull request is part of this local adaptation.

## Verification

Verification must include:

1. Failing tests against the untouched behavior for Codex host detection, subscription-provider preference, actual-family adversary selection, and safe `codex exec` arguments.
2. Focused tests passing after each implementation change.
3. The full Python suite.
4. Codex skill validation with `quick_validate.py`.
5. A minimal live `codex exec` subscription smoke test that returns a fixed short response without an API key in its subprocess environment.
6. Filesystem checks proving both runtime discovery paths resolve to the canonical clone.
