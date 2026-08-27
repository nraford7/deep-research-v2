# Codex and Claude Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make deeper-research use the invoking Codex or Claude subscription for primary reasoning while preserving a genuinely different-family adversary.

**Architecture:** Detect the host once in the provider layer, materialize a host-affine CLI subscription provider, and make adversary selection relative to the selected synthesizer family. Keep orchestration portable through a runtime-adapter reference loaded by `SKILL.md`, while leaving retrieval and evidence processing unchanged.

**Tech Stack:** Python 3.11+, pytest, TOML, Agent Skills Markdown/YAML, Codex CLI, Claude Code CLI.

**Spec:** `docs/superpowers/specs/2026-08-27-codex-claude-compat-design.md`

## Global Constraints

- The canonical clone is `/Users/noahraford/Projects/deeper-research`.
- Codex discovery is `/Users/noahraford/.agents/skills/deeper-research`; Claude discovery is `/Users/noahraford/.claude/skills/deeper-research`; both resolve to the canonical clone.
- Codex host markers take precedence when both Codex and Claude environment markers are present.
- Explicit TOML provider and `[defaults]` choices remain authoritative.
- Primary reasoning uses the invoking host's native subscription-backed agents; Exa retrieval and optional OpenAI embeddings are not relabeled as subscription-backed.
- An independent adversary must have a family different from the actual synthesizer family.
- Do not push changes upstream.

---

### Task 1: Host-Affine Subscription Provider and Adversary Selection

**Files:**
- Modify: `config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_run_config.py`

**Interfaces:**
- Produces: `detect_host(env: Mapping[str, str] | None = None) -> str | None`.
- Produces: implicit providers named `codex-sub` or `claude-sub` from `load_config(toml_paths, env)` when the active host CLI exists.
- Produces: `_resolve_adversary(chain, providers, synthesizer_name)` that excludes the selected synthesizer's actual family.
- Preserves: explicit TOML entries override an implicit provider with the same name.

- [ ] **Step 1: Write failing host/provider tests**

Add tests that monkeypatch `shutil.which` and assert:

```python
def test_detect_host_prefers_codex_markers():
    assert config.detect_host({"CODEX_SESSION_ID": "c", "CLAUDECODE": "1"}) == "codex"

def test_load_config_adds_codex_subscription_provider(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda command: f"/bin/{command}")
    providers, _ = config.load_config([], {"CODEX_SESSION_ID": "c"})
    assert providers["codex-sub"].command == "codex"
    assert providers["codex-sub"].family == "openai"
```

Also assert the analogous Claude provider, no implicit provider for an unknown host, and explicit TOML override behavior.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -m pytest -q tests/test_config.py tests/test_run_config.py`

Expected: failures because `detect_host` and implicit subscription providers do not exist, and same-family selection is still hardcoded to Anthropic.

- [ ] **Step 3: Implement minimal runtime/provider behavior**

Add immutable host specs for Codex and Claude. Keep environment detection pure when an `env` mapping is supplied. Insert the implicit provider before TOML loading so an explicit table replaces it. Update `pick_provider` to prefer the detected host subscription when no explicit default is configured.

Change adversary resolution to obtain the synthesizer provider, read its `family`, and skip any candidate with that same family. If the synthesizer is absent, retain the provider name and a conservative family inferred from the selected host.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python3 -m pytest -q tests/test_config.py tests/test_run_config.py`

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py tests/test_run_config.py
git commit -m "feat: select subscription provider by host runtime"
```

### Task 2: Safe Codex CLI Subscription Calls

**Files:**
- Modify: `llm.py`
- Modify: `tests/test_llm.py`

**Interfaces:**
- Preserves: `_cli_argv_and_input(provider, system_prompt, user_prompt) -> tuple[list[str], str]`.
- Codex argv contract: `codex exec --ephemeral --sandbox read-only --skip-git-repo-check -`, plus optional `--model` and configured extra arguments.
- Preserves: API and gateway credential scrubbing in `_complete_cli`.

- [ ] **Step 1: Update the existing argv test first**

Change the Codex assertion to require the exact safety flags and terminal stdin marker:

```python
assert argv2[:2] == ["codex", "exec"]
assert "--ephemeral" in argv2
assert argv2[argv2.index("--sandbox") + 1] == "read-only"
assert "--skip-git-repo-check" in argv2
assert argv2[-1] == "-"
```

Retain the assertion that system and user prompts are combined in stdin.

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `python3 -m pytest -q tests/test_llm.py::test_cli_argv_builder_claude_and_codex`

Expected: failure because the safety flags and stdin marker are absent.

- [ ] **Step 3: Implement the Codex argv contract**

Build Codex arguments in this order: binary, `exec`, optional `--model`, configured `extra_args`, `--ephemeral`, `--sandbox`, `read-only`, `--skip-git-repo-check`, `-`. Do not change Claude argument construction.

- [ ] **Step 4: Run focused and complete LLM tests**

Run: `python3 -m pytest -q tests/test_llm.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add llm.py tests/test_llm.py
git commit -m "fix: harden codex subscription subprocess calls"
```

### Task 3: Cross-Runtime Skill Orchestration and Documentation

**Files:**
- Create: `references/runtime-adapters.md`
- Create: `agents/openai.yaml`
- Modify: `SKILL.md`
- Modify: `references/squad-audit.md`
- Modify: `README.md`
- Modify: `config.toml.example`
- Modify: `dispatch.py`
- Test: behavioral evaluation plus `quick_validate.py`

**Interfaces:**
- `SKILL.md` requires reading `references/runtime-adapters.md` at invocation and using one selected adapter for the whole run.
- The adapter defines host questions, native subagent dispatch, role-based model choice, concurrency batching, roster locations, CLI provider names, and headless batch commands.
- `agents/openai.yaml` exposes the skill in Codex with implicit invocation enabled.

- [ ] **Step 1: Record the baseline behavioral failures**

Preserve the pre-edit evaluator findings: Codex invocation attempts Claude `AskUserQuestion`/Agent semantics, lacks `codex-sub`, hardcodes Opus/Sonnet and `claude -p`, may exceed four-agent concurrency, and evaluates independence only against Anthropic.

- [ ] **Step 2: Write the runtime adapter reference**

Define three modes: Codex, Claude, and generic fallback. Codex uses native subagents, batches fan-out to available slots, uses strongest/balanced role labels, stores retainers under `~/.agents/agent-roster`, and uses `codex exec` for headless work. Claude retains Agent behavior, Claude model aliases, `~/.claude/agent-roster`, and `claude -p`. Generic fallback uses configured CLI/API providers and must not claim subscription use.

- [ ] **Step 3: Route SKILL and squad instructions through the adapter**

Replace the top-level Claude-only prerequisite and Round 2/3 language with host-native wording. Replace exact `AskUserQuestion` requirements with host-supported interactive questioning plus chat fallback. Keep strict isolation, union/dedup, and independent-family rules. Permit concurrency batching without persona-swapping when the host has fewer slots than lenses.

- [ ] **Step 4: Update operator documentation and examples**

Document installation via the shared canonical clone and symlinks. Provide both Codex and Claude invocation examples. Add `codex-sub` and `claude-sub` examples in `config.toml.example`, while explaining auto-detection. Replace the Claude-only batch recipe with two host-specific recipes. Correct `dispatch.py` output so the squad is labeled default and `coverage_audit.py` fallback-only.

- [ ] **Step 5: Add Codex UI metadata**

Create:

```yaml
interface:
  display_name: "Deeper Research"
  short_description: "Retrieval-first, evidence-gated deep research"
  default_prompt: "Use $deeper-research to produce a deeply sourced research bible on this topic."
policy:
  allow_implicit_invocation: true
```

- [ ] **Step 6: Validate skill structure and rerun behavioral evaluation**

Run: `python3 /Users/noahraford/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`

Expected: validation succeeds. Run an isolated evaluator against the edited skill and confirm it selects Codex native agents/`codex-sub` under Codex while preserving Claude native agents/`claude-sub` under Claude.

- [ ] **Step 7: Commit**

```bash
git add SKILL.md README.md config.toml.example dispatch.py references/runtime-adapters.md references/squad-audit.md agents/openai.yaml
git commit -m "docs: add codex and claude runtime adapters"
```

### Task 4: Canonical Installation and End-to-End Verification

**Files:**
- Move repository to: `/Users/noahraford/Projects/deeper-research`
- Create symlink: `/Users/noahraford/.agents/skills/deeper-research`
- Create symlink: `/Users/noahraford/.claude/skills/deeper-research`

**Interfaces:**
- Both discovery paths resolve through `realpath` to `/Users/noahraford/Projects/deeper-research`.
- Git branch remains `codex-compat`; remote remains `https://github.com/nraford7/deeper-research.git`.

- [ ] **Step 1: Run the complete test suite**

Run: `python3 -m pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 2: Run skill validation from the repository root**

Run: `python3 /Users/noahraford/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`

Expected: success.

- [ ] **Step 3: Move the clean working clone and create discovery links**

Move the existing checkout to `/Users/noahraford/Projects/deeper-research`, then create the Claude and Codex symlinks. Resolve and compare both links with `realpath`; neither may point elsewhere.

- [ ] **Step 4: Run a minimal Codex subscription smoke test**

Using the project's `config.Provider(name="codex-sub", api_type="cli", command="codex", family="openai")`, call `llm.call_model` with a prompt requesting exactly `CODEX_SUBSCRIPTION_OK`. Ensure `OPENAI_API_KEY` is absent from the subprocess environment and assert that token appears in the returned text.

- [ ] **Step 5: Re-run tests and validation through the canonical path**

Run from `/Users/noahraford/Projects/deeper-research`:

```bash
python3 -m pytest -q
python3 /Users/noahraford/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
git status --short --branch
```

Expected: tests and validation pass; the branch is `codex-compat`; only intentional plan/spec commits and implementation commits are present.

- [ ] **Step 6: Commit installation documentation adjustments if verification exposed any**

Only if verification required a repository documentation correction, commit that correction with:

```bash
git add README.md SKILL.md references/runtime-adapters.md
git commit -m "docs: clarify shared local installation"
```
