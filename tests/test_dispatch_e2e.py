import json, config, dispatch

def _writer(report="REPORT"):
    """Return a run_agent stub that writes a canned report and a result dict."""
    def fake_run_agent(provider, agent_type, prompt, output_path):
        output_path.write_text(report, encoding="utf-8")
        return {"agent_type": agent_type.name, "provider": provider.name, "model": provider.model,
                "status": "ok", "words": len(report.split()), "seconds": 0.0, "file": str(output_path)}
    return fake_run_agent

def _boom(*a, **k):
    """A run_agent stub that fails loudly if called (eagerly, not a lazy generator)."""
    raise AssertionError("run_agent should not have been called")

def test_filename_for_agent_type():
    assert dispatch.agent_filename("real-time") == "agent-real-time.md"

def test_run_job_writes_agent_type_files_and_manifest(tmp_path, monkeypatch):
    providers = {"ds": config.Provider("ds", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    monkeypatch.setattr(dispatch, "run_agent", _writer())
    result = dispatch.run_job(topic="T", scope="S", output_dir=tmp_path,
                              providers=providers, agents=agents, languages=["en"],
                              domain_priorities=None, resume=False, seed=0)
    for t in agents:
        assert (tmp_path / f"agent-{t}.md").read_text() == "REPORT"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert set(manifest["assignments"]) == set(agents)
    assert all(r["agent_type"] in agents for r in manifest["results"])
    assert len([r for r in manifest["results"] if r["provider"] == "ds"]) == len(agents)

def test_run_job_resume_reuses_assignments(tmp_path, monkeypatch):
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",)),
                 "b": config.Provider("b", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    monkeypatch.setattr(dispatch, "run_agent", _writer())
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=1)
    first = json.loads((tmp_path / "manifest.json").read_text())["assignments"]
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=True, seed=999)
    second = json.loads((tmp_path / "manifest.json").read_text())["assignments"]
    assert first == second


# Fix 14: resume SKIP path — all agents already done, run_agent must never be called
def test_run_job_resume_skips_completed_agents(tmp_path, monkeypatch):
    """When all agent output files exist, are >4000 bytes, and manifest shows status ok,
    a resume run must skip all agents without calling run_agent."""
    providers = {"ds": config.Provider("ds", "openai", "k", "m", capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    # First run: produce >4000-byte reports
    monkeypatch.setattr(dispatch, "run_agent", _writer("X" * 5000))
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=0)

    # Second run: run_agent must NOT be called — monkeypatch it to raise
    def _must_not_call(*args, **kwargs):
        raise AssertionError("run_agent was called during resume when all agents were already done")

    monkeypatch.setattr(dispatch, "run_agent", _must_not_call)
    # Should not raise — all agents skipped
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=True, seed=0)


def test_run_job_resume_recomputes_when_provider_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "run_agent", _writer())
    p_ab = {"a": config.Provider("a","openai","k","m",capabilities=("web_search",)),
            "b": config.Provider("b","openai","k","m",capabilities=("web_search",))}
    agents = dict(config.BUILTIN_AGENT_TYPES)
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=p_ab, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=0)
    # now provider 'b' is gone; resume must recompute, not KeyError
    p_a = {"a": p_ab["a"]}
    m = dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=p_a, agents=agents,
                         languages=["en"], domain_priorities=None, resume=True, seed=0)
    assert all(prov == "a" for prov in m["assignments"].values())


def test_run_job_resume_reports_skipped_not_new(tmp_path, monkeypatch, capsys):
    providers = {"a": config.Provider("a", "openai", "k", "m", capabilities=("web_search",))}
    agents = {"academic": config.BUILTIN_AGENT_TYPES["academic"]}
    monkeypatch.setattr(dispatch, "run_agent", _writer("X" * 5000))
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=0)
    capsys.readouterr()  # clear
    # resume: nothing new should run; output must say skipped, not "newly run (5xxx words)"
    monkeypatch.setattr(dispatch, "run_agent", _boom)
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=True, seed=0)
    out = capsys.readouterr().out
    assert "skipping 1 agent type" in out
    assert "0 newly run" in out
    assert "1/1 agent types complete" in out


def test_run_job_tags_realtime_when_no_web_search(tmp_path, monkeypatch):
    providers = {"plain": config.Provider("plain", "openai", "k", "m")}  # no web_search
    agents = dict(config.BUILTIN_AGENT_TYPES)
    monkeypatch.setattr(dispatch, "run_agent", _writer("BODY"))
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=providers, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=0)
    rt = (tmp_path / "agent-real-time.md").read_text()
    assert rt.startswith("> [no live web search — knowledge-cutoff results]")
    # a non-web-search agent is NOT tagged
    assert not (tmp_path / "agent-academic.md").read_text().startswith("> [no live web search")


def test_run_job_tags_degraded_realtime_skipped_on_resume(tmp_path, monkeypatch):
    searcher = {"s": config.Provider("s", "openai", "k", "m", capabilities=("web_search",))}
    agents = {"real-time": config.BUILTIN_AGENT_TYPES["real-time"]}
    monkeypatch.setattr(dispatch, "run_agent", _writer("BODY" + "X" * 5000))
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=searcher, agents=agents,
                     languages=["en"], domain_priorities=None, resume=False, seed=0)
    assert not (tmp_path / "agent-real-time.md").read_text().startswith("> [no live web search")
    # resume with provider downgraded to NON-web-search: existing file must get tagged
    plain = {"s": config.Provider("s", "openai", "k", "m")}  # same name, no web_search now
    monkeypatch.setattr(dispatch, "run_agent", _boom)
    dispatch.run_job(topic="T", scope="S", output_dir=tmp_path, providers=plain, agents=agents,
                     languages=["en"], domain_priorities=None, resume=True, seed=0)
    assert (tmp_path / "agent-real-time.md").read_text().startswith("> [no live web search")
