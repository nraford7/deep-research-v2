import textwrap

import config

def test_builtin_agent_types_present():
    names = set(config.BUILTIN_AGENT_TYPES)
    assert names == {"academic", "practitioner", "real-time", "grey-literature", "contrarian"}
    rt = config.BUILTIN_AGENT_TYPES["real-time"]
    assert rt.requires_web_search is True
    assert rt.strategy.strip()
    assert "web search" in rt.system_prompt.lower()
    assert config.BUILTIN_AGENT_TYPES["academic"].requires_web_search is False

def test_default_pairing_and_specs():
    assert config.DEFAULT_PAIRING == {
        "academic": "claude", "practitioner": "chatgpt", "real-time": "perplexity",
        "grey-literature": "gemini", "contrarian": "grok",
    }
    perp = config.BUILTIN_PROVIDER_SPECS["perplexity"]
    assert "web_search" in perp["capabilities"]
    assert "searches_per_run" in perp["pricing"]
    assert config.BUILTIN_PROVIDER_SPECS["claude"]["api_type"] == "anthropic"
    assert config.BUILTIN_PROVIDER_SPECS["gemini"]["api_type"] == "gemini"

def test_default_pairing_covers_every_builtin_agent_type():
    assert set(config.DEFAULT_PAIRING) == set(config.BUILTIN_AGENT_TYPES)

def test_default_pairing_targets_exist_in_provider_specs():
    assert set(config.DEFAULT_PAIRING.values()) <= set(config.BUILTIN_PROVIDER_SPECS)


def test_load_config_no_toml_uses_builtins_from_env():
    providers, agents = config.load_config(toml_paths=[], env={"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"})
    assert set(providers) == {"claude", "chatgpt"}                 # only those with keys
    assert providers["claude"].api_key == "sk-a"
    assert set(agents) == set(config.BUILTIN_AGENT_TYPES)          # all 5 agent types always present

def test_load_config_toml_inline_and_env_ref(tmp_path):
    p = tmp_path / "deep-research.toml"
    p.write_text(textwrap.dedent('''
        [providers.deepseek]
        api_type = "openai"
        api_key = "sk-ds"
        base_url = "https://api.deepseek.com/v1"
        model = "deepseek-v4-pro"
        max_tokens = 4096
        capabilities = ["web_search"]
        max_concurrency = 2
        pricing = { in = 0.55, out = 2.19 }

        [providers.glm]
        api_type = "openai"
        api_key_env = "GLM_KEY"
        base_url = "https://open.bigmodel.cn/api/paas/v4"
        model = "glm-4.6"

        [agents.legal]
        provider = "glm"
        system_prompt = "Legal analyst."
        strategy = "Statutes and case law."
    '''))
    providers, agents = config.load_config(toml_paths=[p], env={"GLM_KEY": "sk-glm"})
    assert providers["deepseek"].api_key == "sk-ds"
    assert providers["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert providers["deepseek"].max_tokens == 4096
    assert providers["deepseek"].capabilities == ("web_search",)
    assert providers["deepseek"].max_concurrency == 2
    assert providers["deepseek"].pricing["out"] == 2.19
    assert providers["glm"].api_key == "sk-glm"                    # resolved from env ref
    assert "legal" in agents and agents["legal"].provider == "glm"
    assert agents["legal"].requires_web_search is False

def test_load_config_project_overrides_global(tmp_path):
    g = tmp_path / "global.toml"; pr = tmp_path / "project.toml"
    g.write_text('[providers.x]\napi_type="openai"\napi_key="g"\nmodel="m1"\n')
    pr.write_text('[providers.x]\napi_type="openai"\napi_key="p"\nmodel="m2"\n')
    providers, _ = config.load_config(toml_paths=[g, pr], env={})   # later wins
    assert providers["x"].api_key == "p" and providers["x"].model == "m2"

def test_load_config_gemini_fallback_models(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.g]\napi_type="gemini"\napi_key="k"\nmodel="gemini-2.5-pro"\nfallback_models=["gemini-2.5-flash"]\n')
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert providers["g"].fallback_models == ("gemini-2.5-flash",)

def test_load_env_files_parses_and_does_not_override(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("# comment\n\nFOO=bar\nQUOTED='quoted-val'\nDQUOTED=\"dq\"\nEXISTING=fromfile\n")
    result = config.load_env_files(paths=[envf], env={"EXISTING": "preset"})
    assert result["FOO"] == "bar"
    assert result["QUOTED"] == "quoted-val"
    assert result["DQUOTED"] == "dq"
    assert result["EXISTING"] == "preset"          # file does NOT override an already-set key


def _prov(name, **kw):
    kw.setdefault("api_type", "openai"); kw.setdefault("api_key", "k"); kw.setdefault("model", "m")
    return config.Provider(name=name, **kw)

def test_assign_explicit_mapping_wins():
    providers = {"glm": _prov("glm")}
    agents = {"academic": config.AgentType("academic", "s", "sp", provider="glm")}
    a, warns = config.assign(agents, providers, seed=0)
    assert a == {"academic": "glm"} and warns == []

def test_assign_default_pairing_when_available():
    providers = {n: _prov(n) for n in ["claude", "chatgpt", "perplexity", "gemini", "grok"]}
    providers["perplexity"] = _prov("perplexity", capabilities=("web_search",))
    a, _ = config.assign(config.BUILTIN_AGENT_TYPES, providers, seed=0)
    assert a == config.DEFAULT_PAIRING

def test_assign_single_provider_absorbs_all_five():
    providers = {"ds": _prov("ds", capabilities=("web_search",))}
    a, warns = config.assign(config.BUILTIN_AGENT_TYPES, providers, seed=0)
    assert set(a) == set(config.BUILTIN_AGENT_TYPES)
    assert set(a.values()) == {"ds"} and warns == []

def test_assign_realtime_prefers_web_search_provider():
    providers = {"plain": _prov("plain"), "searcher": _prov("searcher", capabilities=("web_search",))}
    agents = {"real-time": config.BUILTIN_AGENT_TYPES["real-time"]}
    a, warns = config.assign(agents, providers, seed=0)
    assert a == {"real-time": "searcher"} and warns == []

def test_assign_realtime_no_search_warns():
    providers = {"plain": _prov("plain")}
    agents = {"real-time": config.BUILTIN_AGENT_TYPES["real-time"]}
    a, warns = config.assign(agents, providers, seed=0)
    assert a == {"real-time": "plain"} and any("web search" in w.lower() for w in warns)

def test_assign_explicit_realtime_to_nonsearch_errors():
    providers = {"plain": _prov("plain")}
    agents = {"real-time": config.AgentType("real-time", "s", "sp", provider="plain", requires_web_search=True)}
    try:
        config.assign(agents, providers, seed=0); assert False, "expected error"
    except config.ConfigError:
        pass

def test_assign_missing_provider_errors():
    agents = {"academic": config.AgentType("academic", "s", "sp", provider="nope")}
    try:
        config.assign(agents, {"glm": _prov("glm")}, seed=0); assert False
    except config.ConfigError:
        pass

def test_assign_round_robin_deterministic():
    providers = {"a": _prov("a"), "b": _prov("b")}
    agents = {n: config.AgentType(n, "s", "sp") for n in ["t1", "t2", "t3", "t4"]}
    a1, _ = config.assign(agents, providers, seed=7)
    a2, _ = config.assign(agents, providers, seed=7)
    assert a1 == a2 and set(a1.values()) <= {"a", "b"}

def test_assign_resume_short_circuit_bypasses_ladder():
    providers = {"glm": _prov("glm")}
    agents = {"academic": config.BUILTIN_AGENT_TYPES["academic"]}
    a, warns = config.assign(agents, providers, seed=0, existing={"academic": "ghost"})
    assert a == {"academic": "ghost"} and warns == []


# Fix 11: empty value in .env must not be imported
def test_load_env_files_empty_value_excluded(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("FOO=bar\nEMPTY=\n")
    result = config.load_env_files(paths=[envf], env={})
    assert result["FOO"] == "bar"
    assert "EMPTY" not in result


# Fix 13: assign with empty providers must raise ConfigError
def test_assign_no_providers_errors():
    try:
        config.assign(config.BUILTIN_AGENT_TYPES, {}, seed=0)
        assert False, "expected ConfigError"
    except config.ConfigError:
        pass


def test_resolve_assignments_recomputes_on_stale_prior():
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    # prior map references a provider that no longer exists, and omits most agents
    prior = {"academic": "ghost"}
    assignments, warnings = config.resolve_assignments(agents, providers, seed=0, prior_assignments=prior)
    assert set(assignments) == set(agents)                  # full set, recomputed
    assert all(p in providers for p in assignments.values())  # no dangling provider
    assert any("stale" in w.lower() for w in warnings)

def test_resolve_assignments_keeps_valid_prior():
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",)),
                 "b": config.Provider("b", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    fresh, _ = config.resolve_assignments(agents, providers, seed=3, prior_assignments=None)
    again, warns = config.resolve_assignments(agents, providers, seed=99, prior_assignments=fresh)
    assert again == fresh and not any("stale" in w.lower() for w in warns)  # valid prior kept verbatim


def test_resolve_assignments_recomputes_on_removed_agent():
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",))}
    agents = {"academic": config.BUILTIN_AGENT_TYPES["academic"]}
    prior = {"academic": "a", "ghost-agent": "a"}   # ghost-agent no longer exists
    assignments, warnings = config.resolve_assignments(agents, providers, seed=0, prior_assignments=prior)
    assert set(assignments) == {"academic"} and any("stale" in w.lower() for w in warnings)

def test_resolve_assignments_empty_dict_prior_recomputes():
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    assignments, warnings = config.resolve_assignments(agents, providers, seed=0, prior_assignments={})
    assert set(assignments) == set(agents)   # {} prior is stale (missing all agents) -> recompute


def test_cli_provider_parsed_when_binary_present(tmp_path):
    # use a binary guaranteed to exist on PATH
    p = tmp_path / "c.toml"
    p.write_text('[providers.sub]\napi_type="cli"\ncommand="sh"\n')   # no model/key required
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert "sub" in providers
    assert providers["sub"].api_type == "cli" and providers["sub"].command == "sh"
    assert providers["sub"].api_key == ""

def test_cli_provider_skipped_when_binary_absent(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.ghost]\napi_type="cli"\ncommand="definitely-not-a-real-binary-xyz"\n')
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert "ghost" not in providers

def test_cli_provider_missing_command_errors(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.bad]\napi_type="cli"\n')   # no command
    try:
        config.load_config(toml_paths=[p], env={}); assert False, "expected ConfigError"
    except config.ConfigError:
        pass

def test_cli_provider_extra_args_parsed(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.sub]\napi_type="cli"\ncommand="sh"\nextra_args=["--allowedTools","WebSearch"]\n')
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert providers["sub"].extra_args == ("--allowedTools", "WebSearch")


def test_load_defaults_reads_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[defaults]\nutility = "kimi"\nsynthesis = "claude-sub"\n')
    d = config.load_defaults([p])
    assert d == {"utility": "kimi", "synthesis": "claude-sub"}

def test_load_defaults_missing_is_empty(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.x]\napi_type="openai"\napi_key="k"\nmodel="m"\n')
    assert config.load_defaults([p]) == {}

def test_load_defaults_later_path_overrides(tmp_path):
    g = tmp_path / "g.toml"; pr = tmp_path / "p.toml"
    g.write_text('[defaults]\nutility = "a"\n')
    pr.write_text('[defaults]\nutility = "b"\n')
    assert config.load_defaults([g, pr])["utility"] == "b"

def _p(name, **kw):
    kw.setdefault("api_type", "openai"); kw.setdefault("api_key", "k"); kw.setdefault("model", "m")
    return config.Provider(name=name, **kw)

def test_pick_provider_uses_default_when_available():
    providers = {"kimi": _p("kimi"), "claude": _p("claude")}
    got = config.pick_provider(providers, "utility", {"utility": "kimi"})
    assert got is not None and got.name == "kimi"

def test_pick_provider_falls_back_when_default_absent():
    # default names a provider that isn't configured -> fall through to fallback order
    providers = {"claude": _p("claude"), "chatgpt": _p("chatgpt")}
    got = config.pick_provider(providers, "utility", {"utility": "ghost"},
                               fallback=("claude-sub", "claude", "chatgpt"))
    assert got.name == "claude"

def test_pick_provider_any_when_no_default_no_fallback_match():
    providers = {"weird": _p("weird")}
    got = config.pick_provider(providers, "utility", {}, fallback=("claude", "chatgpt"))
    assert got.name == "weird"     # last resort: any available provider

def test_pick_provider_none_when_no_providers():
    assert config.pick_provider({}, "utility", {"utility": "kimi"}) is None

def test_default_toml_paths_returns_only_existing(tmp_path, monkeypatch):
    # project-local present, global absent
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deep-research.toml").write_text('[defaults]\nutility="x"\n')
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    paths = config.default_toml_paths()
    # default_toml_paths returns relative Path("deep-research.toml") for the local candidate
    assert any(p.resolve() == (tmp_path / "deep-research.toml").resolve() for p in paths)
    assert all(p.exists() for p in paths)
