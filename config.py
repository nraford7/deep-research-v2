"""Authoritative data model and built-in registry for the deep-research dispatcher.

Defines the ``Provider`` and ``AgentType`` dataclasses, built-in provider spec
templates, built-in agent types, the default provider↔agent pairing, and default
pricing.  The TOML/.env loader and assignment engine in later modules consume these
definitions to materialise runtime instances.
"""

import os
import random
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Provider:
    name: str
    api_type: str                       # "openai" | "anthropic" | "gemini" | "cli"
    api_key: str
    model: str
    base_url: str | None = None
    max_tokens: int = 32768
    capabilities: tuple[str, ...] = ()
    pricing: dict | None = None         # {"in","out", optional "reasoning","searches_per_run","search_per_k"}
    fallback_models: tuple[str, ...] = ()
    max_concurrency: int | None = None
    command: str | None = None          # CLI binary name/path for cli providers
    extra_args: tuple[str, ...] = ()   # opt-in extra CLI flags (e.g. to enable web search)


@dataclass(frozen=True)
class AgentType:
    name: str
    strategy: str
    system_prompt: str
    provider: str | None = None         # explicit mapping override
    requires_web_search: bool = False


# --- System prompts ---

_GENERIC_SYS = ("You are a deep research analyst producing comprehensive, fact-checked, "
                "evidence-based research reports with full citations. Produce the COMPLETE "
                "report in a single response. Do NOT ask for confirmation or suggest splitting "
                "into parts. Write the full report now.")
_REALTIME_SYS = ("You are a deep research analyst. Use your web search capabilities extensively "
                 "to find and cite real, verifiable sources. Every claim must have a citation.")
_CONTRARIAN_SYS = ("You are a deep research analyst producing comprehensive, fact-checked, "
                   "evidence-based research reports with full citations. Challenge conventional "
                   "narratives where evidence warrants it.")

# --- Strategy prompts (originally lifted from dispatch.py's STRATEGIES; this is now the source of truth) ---

_STRATEGY_ACADEMIC = """Academic Deep Dive — focus on the most-cited academic papers, NBER/SSRN working papers,
journal articles, university research, think tank publications, and review articles.
Find the canonical authors in the field. Follow citation chains. Identify theoretical
frameworks and empirical debates. Structure as a literature review with theoretical
underpinnings and empirical findings."""

_STRATEGY_PRACTITIONER = """Practitioner & Explainer — focus on practical, applied, how-it-works sources.
Industry white papers, consulting reports, trade publications, professional guides,
technical documentation, methodology documents, company reports. Find the best
explainers and how-to guides. Include data tables, process descriptions, and
real-world examples. Structure for a practitioner audience."""

_STRATEGY_REALTIME = """Real-Time Web Intelligence — focus on current, up-to-date information.
Search extensively for recent sources (last 1-3 years). Find recent news articles,
government reports, regulatory filings, press releases, current data, recent
conference proceedings. Identify what has changed recently, current controversies,
recent policy changes, emerging trends. Verify current figures and statistics.
Structure around current state and recent developments."""

_STRATEGY_GREY_LITERATURE = """Grey Literature & Primary Sources — focus on primary documents and original data.
Government reports, international organization publications (UN, World Bank, IMF, OECD),
NGO reports, official datasets, legal documents, treaties, standards, congressional
testimony, regulatory dockets. Find the PRIMARY source behind secondary claims.
If a paper cites a government report, find the report. Structure around documentary
evidence and original-source citations."""

_STRATEGY_CONTRARIAN = """Contrarian & Cross-Disciplinary Analysis — challenge conventional narratives.
Search for dissenting academic views, minority positions in policy debates,
cross-disciplinary insights (e.g., complexity science applied to markets, network
theory applied to supply chains), unconventional data sources, and perspectives
from outside the mainstream Western institutional framework. Find what the other
research strategies are likely to miss. Structure around alternative framings,
overlooked evidence, and underrepresented perspectives."""


# --- Built-in registries ---

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {
    "academic": AgentType(
        name="academic",
        strategy=_STRATEGY_ACADEMIC,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "practitioner": AgentType(
        name="practitioner",
        strategy=_STRATEGY_PRACTITIONER,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "real-time": AgentType(
        name="real-time",
        strategy=_STRATEGY_REALTIME,
        system_prompt=_REALTIME_SYS,
        requires_web_search=True,
    ),
    "grey-literature": AgentType(
        name="grey-literature",
        strategy=_STRATEGY_GREY_LITERATURE,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "contrarian": AgentType(
        name="contrarian",
        strategy=_STRATEGY_CONTRARIAN,
        system_prompt=_CONTRARIAN_SYS,
        requires_web_search=False,
    ),
}

DEFAULT_PAIRING: dict[str, str] = {
    "academic": "claude",
    "practitioner": "chatgpt",
    "real-time": "perplexity",
    "grey-literature": "gemini",
    "contrarian": "grok",
}

# Spec templates keyed by built-in provider name.  The loader materialises them into
# runtime Provider instances once an API key is resolved from the environment, which
# is why api_key isn't stored here (and why Provider has no consumer in this file).
BUILTIN_PROVIDER_SPECS: dict[str, dict] = {
    "claude": {
        "api_type": "anthropic",
        # The previous default `claude-opus-4-20250514` was retired and now 404s.
        # Pinned to a current Opus; override in TOML as model IDs drift.
        "model": "claude-opus-4-8",
        "env_key": "ANTHROPIC_API_KEY",
        "max_tokens": 128000,
        "capabilities": [],
        "pricing": {"in": 15.0, "out": 75.0},
    },
    "chatgpt": {
        "api_type": "openai",
        "env_key": "OPENAI_API_KEY",
        "model": "gpt-4.1",
        "max_tokens": 32768,
        "capabilities": [],
        "pricing": {"in": 2.0, "out": 8.0},
    },
    "perplexity": {
        "api_type": "openai",
        "env_key": "PERPLEXITY_API_KEY",
        "base_url": "https://api.perplexity.ai",
        "model": "sonar-deep-research",
        "max_tokens": 128000,
        "capabilities": ["web_search"],
        "pricing": {"in": 2.0, "out": 8.0, "reasoning": 3.0, "searches_per_run": 50, "search_per_k": 5.0},
    },
    "gemini": {
        "api_type": "gemini",
        "env_key": "GOOGLE_API_KEY",
        "model": "gemini-2.5-pro",
        "fallback_models": ["gemini-2.5-flash", "gemini-2.0-flash"],
        "max_tokens": 65536,
        "capabilities": [],
        "pricing": {"in": 1.25, "out": 10.0},
    },
    "grok": {
        "api_type": "openai",
        "env_key": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "model": "grok-3-latest",
        "max_tokens": 128000,
        "capabilities": [],
        "pricing": {"in": 3.0, "out": 15.0},
    },
}

# Convenience reference consumed by the cost-estimation module.
DEFAULT_PRICING = {name: spec["pricing"] for name, spec in BUILTIN_PROVIDER_SPECS.items()}


class ConfigError(Exception):
    pass


def _has_web_search(provider):
    return "web_search" in provider.capabilities


def assign(agents, providers, seed=0, existing=None):
    """Return (assignments {agent_type: provider}, warnings). `existing` short-circuits (resume)."""
    if existing is not None:
        return dict(existing), []
    if not providers:
        raise ConfigError("No providers available — set at least one API key or define a [providers.*] block.")
    warnings = []
    assignments = {}
    unassigned = []

    # 1. explicit mappings
    for name, at in agents.items():
        if at.provider is not None:
            if at.provider not in providers:
                raise ConfigError(f"Agent '{name}' maps to undefined provider '{at.provider}'.")
            if at.requires_web_search and not _has_web_search(providers[at.provider]):
                raise ConfigError(f"Agent '{name}' requires web search but provider "
                                  f"'{at.provider}' lacks the 'web_search' capability.")
            assignments[name] = at.provider
        else:
            unassigned.append(name)

    # 2. built-in default pairing
    still = []
    for name in unassigned:
        target = DEFAULT_PAIRING.get(name)
        if target and target in providers:
            at = agents[name]
            if not at.requires_web_search or _has_web_search(providers[target]):
                assignments[name] = target
                continue
        still.append(name)
    unassigned = still

    # 3. web-search agents -> a web-search-capable provider, preferring perplexity
    searchers = [p for p in providers.values() if _has_web_search(p)]
    searchers.sort(key=lambda p: (p.name != "perplexity", p.name))
    still = []
    for name in unassigned:
        if agents[name].requires_web_search and searchers:
            assignments[name] = searchers[0].name
        else:
            still.append(name)
    unassigned = still

    # 4. seeded round-robin fill (deterministic), warning if a web-search agent lands on a plain provider
    pool = sorted(providers)
    rng = random.Random(seed)
    rng.shuffle(pool)
    for i, name in enumerate(sorted(unassigned)):
        chosen = pool[i % len(pool)]
        assignments[name] = chosen
        if agents[name].requires_web_search and not _has_web_search(providers[chosen]):
            warnings.append(f"Agent '{name}' needs live web search but no web_search-capable "
                            f"provider is configured; '{chosen}' will return knowledge-cutoff results.")
    return assignments, warnings


def resolve_assignments(agents, providers, seed=0, prior_assignments=None):
    """assign(), but transparently recompute if a resumed map is stale
    (references a missing provider or omits a now-active agent)."""
    assignments, warnings = assign(agents, providers, seed=seed, existing=prior_assignments)
    stale = (any(name not in assignments for name in agents)
             or any(p not in providers for p in assignments.values())
             or any(name not in agents for name in assignments))
    if prior_assignments is not None and stale:
        assignments, warnings = assign(agents, providers, seed=seed, existing=None)
        warnings = list(warnings) + ["resume assignments stale (provider/agent set changed); recomputed."]
    return assignments, warnings


def default_toml_paths():
    """Return the list of existing TOML config paths in standard locations."""
    candidates = [Path.home() / ".config" / "deep-research" / "config.toml", Path("deep-research.toml")]
    return [p for p in candidates if p.exists()]


def load_env_files(paths=(Path.home() / ".env", Path(".env")), env=None):
    """Merge KEY=VAL lines from .env files into a dict (mirrors old dispatch.py loader)."""
    env = dict(os.environ if env is None else env)
    for ep in paths:
        ep = Path(ep)
        if not ep.exists():
            continue
        for line in ep.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip("'\"")
                if k and v and k not in env:  # skip blanks/empties; don't override keys already set
                    env[k] = v
    return env

def _provider_from_table(name, t, env):
    if t.get("api_type") == "cli":
        if "command" not in t:
            raise ConfigError(f"provider '{name}' with api_type='cli' missing required field: command")
        if shutil.which(t["command"]) is None:
            return None  # binary not installed — skip gracefully
        return Provider(
            name=name, api_type="cli", api_key="", model=t.get("model", ""),
            base_url=t.get("base_url"), max_tokens=t.get("max_tokens", 32768),
            capabilities=tuple(t.get("capabilities", ())), pricing=t.get("pricing"),
            fallback_models=tuple(t.get("fallback_models", ())),
            max_concurrency=t.get("max_concurrency"),
            command=t["command"],
            extra_args=tuple(t.get("extra_args", ())),
        )
    missing = {"api_type", "model"} - t.keys()
    if missing:
        raise ConfigError(f"provider '{name}' missing required field(s): {', '.join(sorted(missing))}")
    if "api_key" in t:
        key = t["api_key"]
    elif "api_key_env" in t:
        key = env.get(t["api_key_env"], "")
    else:
        key = ""
    if not key:
        return None
    return Provider(
        name=name, api_type=t["api_type"], api_key=key, model=t["model"],
        base_url=t.get("base_url"), max_tokens=t.get("max_tokens", 32768),
        capabilities=tuple(t.get("capabilities", ())), pricing=t.get("pricing"),
        fallback_models=tuple(t.get("fallback_models", ())),
        max_concurrency=t.get("max_concurrency"),
    )

def _builtin_provider(name, env):
    spec = BUILTIN_PROVIDER_SPECS[name]
    key = env.get(spec["env_key"], "")
    if not key:
        return None
    return Provider(
        name=name, api_type=spec["api_type"], api_key=key, model=spec["model"],
        base_url=spec.get("base_url"), max_tokens=spec.get("max_tokens", 32768),
        capabilities=tuple(spec.get("capabilities", ())), pricing=spec.get("pricing"),
        fallback_models=tuple(spec.get("fallback_models", ())),
    )

def load_defaults(toml_paths):
    """Read the [defaults] table (provider roles like utility/synthesis) from the TOML
    paths; later paths override earlier. Returns {} if no [defaults] anywhere."""
    out = {}
    for path in toml_paths:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        out.update(data.get("defaults") or {})
    return out


def pick_provider(providers, role, defaults, fallback=("claude-sub", "claude", "chatgpt")):
    """Resolve a provider for a one-off role (e.g. 'utility'): the [defaults][role]
    provider if configured, else the first available provider in `fallback`, else any
    available provider, else None."""
    if not providers:
        return None
    name = defaults.get(role)
    if name and name in providers:
        return providers[name]
    for fb in fallback:
        if fb in providers:
            return providers[fb]
    return next(iter(providers.values()), None)


def load_config(toml_paths, env):
    providers = {n: p for n in BUILTIN_PROVIDER_SPECS if (p := _builtin_provider(n, env))}
    agents = dict(BUILTIN_AGENT_TYPES)
    for path in toml_paths:                          # later paths override earlier
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        for name, t in (data.get("providers") or {}).items():
            p = _provider_from_table(name, t, env)
            if p is not None:
                providers[name] = p
        for name, t in (data.get("agents") or {}).items():
            base = agents.get(name)
            agents[name] = AgentType(
                name=name,
                strategy=t.get("strategy", base.strategy if base else ""),
                system_prompt=t.get("system_prompt", base.system_prompt if base else _GENERIC_SYS),
                provider=t.get("provider", base.provider if base else None),
                requires_web_search=t.get("requires_web_search",
                                          base.requires_web_search if base else False),
            )
    return providers, agents
