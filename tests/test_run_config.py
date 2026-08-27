import textwrap

import pytest

import config


# --- provider family metadata -------------------------------------------------

def test_provider_has_family_default_other():
    p = config.Provider(name="x", api_type="openai", api_key="k", model="m")
    assert p.family == "other"


def test_builtin_provider_families():
    env = {
        "ANTHROPIC_API_KEY": "sk-a",
        "OPENAI_API_KEY": "sk-o",
        "GOOGLE_API_KEY": "sk-g",
        "XAI_API_KEY": "sk-x",
    }
    providers, _ = config.load_config(toml_paths=[], env=env)
    assert providers["claude"].family == "anthropic"
    assert providers["chatgpt"].family == "openai"
    assert providers["gemini"].family == "google"
    assert providers["grok"].family == "xai"


def test_toml_provider_family_respected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [providers.deepseek]
        api_type = "openai"
        api_key  = "sk-ds"
        model    = "deepseek-v4-pro"
        family   = "deepseek-house"
    '''))
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert providers["deepseek"].family == "deepseek-house"


def test_toml_provider_family_missing_defaults_other(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [providers.deepseek]
        api_type = "openai"
        api_key  = "sk-ds"
        model    = "deepseek-v4-pro"
    '''))
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert providers["deepseek"].family == "other"


def test_cli_toml_provider_family(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [providers.sub]
        api_type = "cli"
        command  = "sh"
        family   = "anthropic"
    '''))
    providers, _ = config.load_config(toml_paths=[p], env={})
    assert providers["sub"].family == "anthropic"


# --- RunConfig defaults -------------------------------------------------------

def _all_keys():
    return {
        "ANTHROPIC_API_KEY": "sk-a",
        "OPENAI_API_KEY": "sk-o",
        "GOOGLE_API_KEY": "sk-g",
        "XAI_API_KEY": "sk-x",
    }


def test_run_config_defaults_no_run_table():
    rc = config.load_run_config(toml_paths=[], env=_all_keys())
    assert rc.mode == "slices"
    assert rc.max_retrieval_usd == 1.0
    assert rc.min_evidence_total == 10
    assert rc.min_nonempty_slices == 2
    assert rc.adversary_chain == ["grok", "chatgpt", "gemini"]


def test_run_config_default_slice_roster():
    rc = config.load_run_config(toml_paths=[], env=_all_keys())
    assert set(rc.slices) == {
        "publication", "news", "institutional", "financial", "personal-site",
    }
    pub = rc.slices["publication"]
    assert pub.category is not None and pub.include_domains is None
    assert pub.query == "{topic}"
    assert pub.enabled is True

    news = rc.slices["news"]
    assert news.category is not None and news.enabled is True
    assert news.query == "{topic} latest developments"

    inst = rc.slices["institutional"]
    assert inst.include_domains is not None and inst.category is None
    assert inst.enabled is True

    assert rc.slices["financial"].enabled is False
    assert rc.slices["personal-site"].enabled is False
    ps = rc.slices["personal-site"]
    assert ps.include_domains is not None and ps.category is None


# --- RunConfig from TOML ------------------------------------------------------

def test_run_config_reads_run_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [run]
        mode = "slices"
        max_retrieval_usd = 2.5
        min_evidence_total = 20
        min_nonempty_slices = 3
        adversary = ["chatgpt", "gemini"]
    '''))
    rc = config.load_run_config(toml_paths=[p], env=_all_keys())
    assert rc.max_retrieval_usd == 2.5
    assert rc.min_evidence_total == 20
    assert rc.min_nonempty_slices == 3
    assert rc.adversary_chain == ["chatgpt", "gemini"]


def test_run_config_invalid_mode_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[run]\nmode = "legacy"\n')
    with pytest.raises(ValueError):
        config.load_run_config(toml_paths=[p], env=_all_keys())


def test_run_config_custom_slices(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [slices.mine]
        query = "{topic} custom"
        category = "research paper"
        enabled = true

        [slices.domains]
        query = "{topic} gov"
        include_domains = ["example.gov"]
    '''))
    rc = config.load_run_config(toml_paths=[p], env=_all_keys())
    assert set(rc.slices) == {"mine", "domains"}
    assert rc.slices["mine"].category == "research paper"
    assert rc.slices["mine"].include_domains is None
    assert rc.slices["mine"].enabled is True
    assert rc.slices["domains"].include_domains == ("example.gov",)   # tuple: frozen + hashable
    assert rc.slices["domains"].category is None
    assert rc.slices["domains"].enabled is True   # default


def test_slice_spec_is_hashable_and_frozen():
    # tuple domains make SliceSpec truly immutable + hashable (no shared-mutable leak).
    rc = config.load_run_config(toml_paths=[], env=_all_keys())
    inst = rc.slices["institutional"]
    assert isinstance(inst.include_domains, tuple)
    hash(inst)  # must not raise


def test_run_config_slice_xor_violation_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [slices.bad]
        query = "{topic}"
        category = "news"
        include_domains = ["example.com"]
    '''))
    with pytest.raises(ValueError):
        config.load_run_config(toml_paths=[p], env=_all_keys())


# --- adversary selection ------------------------------------------------------

def test_adversary_excludes_the_actual_openai_synthesizer_family():
    providers = {
        "codex-sub": config.Provider("codex-sub", "cli", "", "", command="codex", family="openai"),
        "chatgpt": config.Provider("chatgpt", "openai", "k", "m", family="openai"),
        "claude": config.Provider("claude", "anthropic", "k", "m", family="anthropic"),
    }
    adversary, warning = config._resolve_adversary(
        ["chatgpt", "claude"], providers, "codex-sub")
    assert adversary == "claude"
    assert warning is None


def test_adversary_uses_host_family_when_synthesizer_is_absent(monkeypatch):
    monkeypatch.setattr(config, "detect_host", lambda env=None: "codex")
    providers = {
        "chatgpt": config.Provider("chatgpt", "openai", "k", "m", family="openai"),
        "claude": config.Provider("claude", "anthropic", "k", "m", family="anthropic"),
    }
    adversary, warning = config._resolve_adversary(
        ["chatgpt", "claude"], providers, "missing")
    assert adversary == "claude"
    assert warning is None

def test_adversary_picks_first_available_non_anthropic(tmp_path):
    # chain names codex (not configured) then grok (configured) -> picks grok
    p = tmp_path / "c.toml"
    p.write_text('[run]\nadversary = ["codex", "grok"]\n')
    rc = config.load_run_config(toml_paths=[p], env={"XAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-a"})
    assert rc.adversary == "grok"
    assert rc.adversary_warning is None


def test_adversary_all_anthropic_warns_and_falls_back(tmp_path):
    # Only claude (anthropic) configured; chain entries either unconfigured or anthropic.
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent('''
        [run]
        adversary = ["claude"]
        [providers.claude-extra]
        api_type = "cli"
        command  = "sh"
        family   = "anthropic"
    '''))
    rc = config.load_run_config(toml_paths=[p], env={"ANTHROPIC_API_KEY": "sk-a"})
    assert rc.adversary_warning is not None
    # falls back to the synthesizer provider name (anthropic)
    assert rc.adversary == rc.synthesizer


def test_adversary_skips_unconfigured_chain_entries(tmp_path):
    # default chain grok/chatgpt/gemini, but only OpenAI/chatgpt is configured:
    # it cannot count as an independent review of an OpenAI synthesis run.
    rc = config.load_run_config(toml_paths=[], env={"OPENAI_API_KEY": "sk-o"})
    assert rc.adversary == "chatgpt"
    assert rc.adversary_warning is not None


def test_adversary_default_chain_picks_grok_when_all_keys():
    # default chain ["grok", ...] with every key set -> grok (first non-anthropic) wins
    rc = config.load_run_config(toml_paths=[], env=_all_keys())
    assert rc.adversary == "grok"
    assert rc.adversary_warning is None


def test_adversary_empty_chain_falls_back_to_default(tmp_path):
    # an explicitly-empty [run] adversary must reset to the default chain, never stay empty
    p = tmp_path / "c.toml"
    p.write_text('[run]\nadversary = []\n')
    rc = config.load_run_config(toml_paths=[p], env=_all_keys())
    assert rc.adversary_chain == ["grok", "chatgpt", "gemini"]
    assert rc.adversary == "grok"


def test_deeper_research_config_env_override(tmp_path):
    p = tmp_path / "override.toml"
    p.write_text('[run]\nmax_retrieval_usd = 7.0\n')
    env = dict(_all_keys())
    env["DEEPER_RESEARCH_CONFIG"] = str(p)
    # toml_paths deliberately empty; env override must supply the source
    rc = config.load_run_config(toml_paths=[], env=env)
    assert rc.max_retrieval_usd == 7.0
