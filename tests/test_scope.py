from scripts import scope
import config, llm

_JSON = '{"primary_domain": "economics", "secondary_domains": ["law"], "priority_sources": ["NBER"], "weight_against": ["blogs"], "must_check": "BIS", "search_keywords": ["cbdc"]}'

def _prov(name="kimi"):
    return config.Provider(name, "openai", "k", "m")

def _no_call(*a, **k):
    raise AssertionError("llm.call_model should not have been called")

def test_llm_proposal_uses_configured_provider(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({"kimi": _prov()}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {"utility": "kimi"})
    def fake_call(provider, system, user):
        captured["provider"] = provider.name; captured["system"] = system; captured["user"] = user
        return _JSON
    monkeypatch.setattr(llm, "call_model", fake_call)
    out = scope.llm_proposal("CBDCs", "design + adoption", toml_paths=[tmp_path / "none.toml"])
    assert out["primary_domain"] == "economics"
    assert captured["provider"] == "kimi"
    assert "CBDCs" in captured["user"]

def test_llm_proposal_falls_back_when_no_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {})
    monkeypatch.setattr(llm, "call_model", _no_call)  # must NOT be called
    assert scope.llm_proposal("T", "S", toml_paths=[tmp_path / "none.toml"]) is None

def test_llm_proposal_handles_model_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({"kimi": _prov()}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {"utility": "kimi"})
    def boom(*a, **k): raise RuntimeError("api down")
    monkeypatch.setattr(llm, "call_model", boom)
    assert scope.llm_proposal("T", "S", toml_paths=[tmp_path / "none.toml"]) is None

def test_llm_proposal_strips_json_fences(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({"kimi": _prov()}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {"utility": "kimi"})
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: "```json\n" + _JSON + "\n```")
    out = scope.llm_proposal("T", "S", toml_paths=[tmp_path / "none.toml"])
    assert out["primary_domain"] == "economics"

def test_llm_proposal_config_error_falls_back(monkeypatch, tmp_path):
    def boom(*a, **k): raise ValueError("bad toml")
    monkeypatch.setattr(config, "load_config", boom)  # raises first; load_defaults never reached
    monkeypatch.setattr(llm, "call_model", _no_call)  # must NOT be called
    assert scope.llm_proposal("T", "S", toml_paths=[tmp_path / "none.toml"]) is None
