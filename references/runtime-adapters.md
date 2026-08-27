# Runtime adapters

At the start of every deeper-research invocation, select exactly one adapter and
use it for the whole run. Do not mix host tools, model names, roster paths, or
subscription providers across adapters.

| Active host | Adapter | Subscription provider | Native delegation | Retainer roster | Headless command |
|---|---|---|---|---|---|
| Codex | Codex | `codex-sub` | Codex native subagents | `~/.agents/agent-roster/` | `codex exec` |
| Claude Code | Claude | `claude-sub` | Claude `Agent` subagents | `~/.claude/agent-roster/` | `claude -p` |
| Any other host | Generic fallback | configured provider only | Host-supported delegation, if any | configured/local convention | configured CLI or API provider |

`config.py` detects Codex before Claude when both environment marker families are
present. Explicit TOML provider names and `[defaults]` selections remain
authoritative. An explicit `[defaults].synthesis` selects the configured provider
that actually runs synthesis; only when it is absent does synthesis use the selected
host's native subagent. The actual executor's family governs adversary selection.
One-off utility calls otherwise prefer the selected host's subscription provider. A
generic fallback must never claim that a configured CLI/API provider uses a Codex or
Claude subscription.

## Shared orchestration rules

- Use the host's native interactive-question mechanism when it exists. If it is
  unavailable, ask in ordinary chat; for headless work, skip optional framing
  rather than waiting for input.
- Assign models by role: **strongest available** for synthesis and integration;
  **balanced/cheaper** for scoping, checklist work, squad lenses, and fix passes;
  **different family** for the adversary. Do not substitute a provider name or
  model alias for the role requirement.
- Every generating lens and every per-gap verifier gets a fresh, isolated
  context. Pool results by union, deduplicate by meaning, and never persona-swap
  or consensus-blend.
- A host with fewer available subagent slots than the requested fan-out must
  batch the same isolated prompts through its available slots. Batching is not
  permission to combine personas or change their prompts.
- Compare the adversary provider family to the family of the **actual synthesis
  executor**, not to a hardcoded vendor. An unavailable different-family provider is
  fail-closed: do not claim independent review. A same-family critique may be retained
  only when it is explicitly labeled non-independent.

## Codex adapter

Use Codex native subagents for the coverage panel, synthesis, integration, and
fix pass when no explicit `[defaults].synthesis` executor is configured. Launch only
as many concurrent subagents as the host makes available; queue the remaining isolated
prompts in batches. Use strongest-available and balanced/cheaper Codex model choices by
role rather than Claude model aliases.

Read and update retainers only under `~/.agents/agent-roster/`. For script-driven
text-only calls, select `codex-sub`. Its safe direct-call form is:

```bash
codex exec --ephemeral --sandbox read-only --skip-git-repo-check -
```

Pass the prompt on standard input. The read-only form is for text-only utility calls.
A headless full-pipeline run must set `--sandbox workspace-write` from its own run
directory and use no broader write root; it still retains every evidence gate and
output constraint.

## Claude adapter

Use Claude Code `Agent` subagents with Claude's native model aliases when no explicit
`[defaults].synthesis` executor is configured. Choose the strongest available Claude
alias for synthesis/integration and a balanced alias for bounded roles. If the host has
fewer slots than the panel needs, dispatch the same isolated agent prompts in batches.

Read and update retainers only under `~/.claude/agent-roster/`. For direct
script-driven text calls, select `claude-sub` and use `claude -p` with explicit allowed
tools. Claude's CLI does not make the allowed-tool list a path-scoped write sandbox, so
a full pipeline must run inside a pre-approved contained runner whose writable mount is
only that run directory. Claude native agents and subscription behavior remain the
default under Claude Code when no synthesis executor override is configured.

## Generic fallback adapter

Use only configured CLI/API providers and whatever native delegation the host
actually supports. If no native subagent capability exists, use the documented
single-model fallback and label the limitation. Keep role-based selection,
isolation where delegation exists, and the different-family adversary rule. Do
not refer to `codex-sub`, `claude-sub`, a Codex roster, a Claude roster, or a
subscription unless that capability is actually present.

## Headless batches

One question still means one fully isolated run directory and ledger. Use the selected
adapter's headless command, cap concurrent runs to available capacity, and keep each
run's logs separate. Codex batches run from the run directory with
`--sandbox workspace-write`; Claude batches use explicit allowed tools inside a
pre-approved contained runner whose writable mount is that directory; generic batches
use the configured provider command/API. Headless runs skip optional interactive
framing, but never bypass retrieval, evidence, coverage, citation, or
independent-adversary gates.
