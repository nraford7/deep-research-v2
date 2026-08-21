import json

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


# --- optional domains / fresh_since contract (back-compatible) ---

def test_infer_domains_maps_named_institutions():
    got = scope.infer_domains("What does RAND say about deterrence?", "compare with OECD")
    assert "rand.org" in got and "oecd.org" in got

def test_infer_domains_empty_when_no_institution():
    assert scope.infer_domains("grid-scale battery storage economics") == []

def test_infer_domains_deduplicates_and_is_stable():
    got = scope.infer_domains("RAND and rand and RAND again", "")
    assert got == ["rand.org"]

def test_main_writes_inferred_domains(tmp_path):
    out = tmp_path / "scope.md"
    import sys as _sys
    argv = ["scope.py", "--topic", "RAND analysis of deterrence policy", "--output", str(out)]
    monkey = _sys.argv
    _sys.argv = argv
    try:
        scope.main()
    finally:
        _sys.argv = monkey
    payload = json.loads(out.with_suffix(".json").read_text())
    assert "rand.org" in payload.get("domains", [])
    # fresh_since only appears when a proposal supplies it — absent by default.
    assert "fresh_since" not in payload

def test_main_no_domains_key_when_none(tmp_path):
    out = tmp_path / "scope.md"
    import sys as _sys
    argv = ["scope.py", "--topic", "grid-scale battery storage economics", "--output", str(out)]
    monkey = _sys.argv
    _sys.argv = argv
    try:
        scope.main()
    finally:
        _sys.argv = monkey
    payload = json.loads(out.with_suffix(".json").read_text())
    assert "domains" not in payload
