import os, sys, types
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import scripts.search as w  # the wrapper


@pytest.fixture
def fake_engine(monkeypatch):
    """Install a fake vendored engine; record calls. Key resolves True."""
    calls = {}
    eng = types.SimpleNamespace()
    def cmd_index(root, db_path, *, rebuild, in_patterns):
        calls["index"] = dict(root=root, db_path=db_path, rebuild=rebuild, in_patterns=in_patterns)
    def cmd_query(q, root, db_path, *, top_k, in_patterns, as_json):
        calls["query"] = dict(q=q, root=root, db_path=db_path, top_k=top_k, in_patterns=in_patterns, as_json=as_json)
    eng.cmd_index = cmd_index
    eng.cmd_query = cmd_query
    monkeypatch.setattr(w, "_load_engine", lambda: eng)
    monkeypatch.setattr(w, "_resolve_key", lambda: True)
    return calls


def _make_index(tmp_path):
    """Create research/.semantic-index.db so _do_query reaches the engine call."""
    r = tmp_path / "research"
    r.mkdir(exist_ok=True)
    (r / ".semantic-index.db").write_bytes(b"x")
    return r


# --- wiring ---------------------------------------------------------------

def test_index_uses_bible_globs_and_research_root(fake_engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert w.run(["index"]) == 0
    assert fake_engine["index"]["in_patterns"] == ["*/README.md", "*/sections/*.md"]
    assert fake_engine["index"]["root"] == (tmp_path / "research").resolve()
    assert fake_engine["index"]["db_path"] == (tmp_path / "research").resolve() / ".semantic-index.db"
    assert fake_engine["index"]["rebuild"] is False


def test_index_honors_root_flag(fake_engine, tmp_path):
    target = tmp_path / "custom"
    assert w.run(["index", "--root", str(target)]) == 0
    assert fake_engine["index"]["root"] == target.resolve()


def test_query_positional_routed(fake_engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); _make_index(tmp_path)
    assert w.run(["why does X happen?"]) == 0
    assert fake_engine["query"]["q"] == "why does X happen?"
    assert fake_engine["query"]["in_patterns"] is None
    assert fake_engine["query"]["top_k"] == 5


def test_topic_scopes_glob(fake_engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); _make_index(tmp_path)
    assert w.run(["risks", "--topic", "cbdc"]) == 0
    assert fake_engine["query"]["in_patterns"] == ["cbdc/**"]


def test_no_query_is_graceful(fake_engine, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert w.run([]) == 0
    assert "index" in capsys.readouterr().err


# --- graceful degradation (assert exit 0 AND the notice text) -------------

def test_missing_key_skips_exit0(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(w, "_load_engine", lambda: types.SimpleNamespace(
        cmd_index=lambda *a, **k: None, cmd_query=lambda *a, **k: None))
    monkeypatch.setattr(w, "_resolve_key", lambda: False)
    assert w.run(["index"]) == 0
    assert w.run(["some query"]) == 0
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_import_failure_names_requirements_file_exit0(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    def boom():
        raise ImportError("No module named 'sqlite_vec'")
    monkeypatch.setattr(w, "_load_engine", boom)
    assert w.run(["index"]) == 0
    assert "requirements-search.txt" in capsys.readouterr().err
    # query path import failure also names the requirements file
    assert w.run(["q"]) == 0
    assert "requirements-search.txt" in capsys.readouterr().err


def test_load_engine_probes_missing_optional_dep(monkeypatch):
    """The REAL _load_engine must raise ImportError when sqlite_vec is absent —
    the engine imports it lazily, so without the probe a missing dep would slip
    past the ImportError branch and surface as a cryptic late SystemExit. This
    proves the requirements-search.txt guidance path is actually reachable in
    production, not just when ImportError is hand-mocked."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "sqlite_vec":
            raise ImportError("No module named 'sqlite_vec'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError):
        w._load_engine()


def test_index_runtime_error_skips_exit0(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    eng = types.SimpleNamespace()
    eng.cmd_index = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    monkeypatch.setattr(w, "_load_engine", lambda: eng)
    monkeypatch.setattr(w, "_resolve_key", lambda: True)
    assert w.run(["index"]) == 0
    assert "indexing failed" in capsys.readouterr().err


def test_bad_args_exit0_one_line_notice(monkeypatch, tmp_path, capsys):
    """argparse errors (unknown flag) must NOT propagate nonzero AND must emit
    only the wrapper's one-line notice — no argparse 'usage:' / 'error:' noise."""
    monkeypatch.chdir(tmp_path)
    assert w.run(["index", "--bogus"]) == 0
    assert w.run(["--bogus", "q"]) == 0
    err = capsys.readouterr().err
    assert "invalid arguments" in err
    assert "usage:" not in err  # _QuietParser suppresses argparse's own output
    assert "error:" not in err


def test_missing_index_gives_guidance_exit0(monkeypatch, tmp_path, capsys):
    """No db on disk -> _do_query short-circuits with 'run index' guidance, exit 0."""
    monkeypatch.chdir(tmp_path)  # NOTE: no _make_index here
    eng = types.SimpleNamespace(cmd_index=lambda *a, **k: None,
                                cmd_query=lambda *a, **k: None)
    monkeypatch.setattr(w, "_load_engine", lambda: eng)
    monkeypatch.setattr(w, "_resolve_key", lambda: True)
    assert w.run(["anything"]) == 0
    assert "index" in capsys.readouterr().err


def test_query_engine_sysexit_caught_exit0(monkeypatch, tmp_path, capsys):
    """Engine sys.exit(2) (e.g. db race) must be caught -> exit 0.
    DB exists so we get past the missing-index guard into the engine call."""
    monkeypatch.chdir(tmp_path); _make_index(tmp_path)
    def cmd_query(*a, **k):
        raise SystemExit(2)
    eng = types.SimpleNamespace(cmd_query=cmd_query, cmd_index=lambda *a, **k: None)
    monkeypatch.setattr(w, "_load_engine", lambda: eng)
    monkeypatch.setattr(w, "_resolve_key", lambda: True)
    assert w.run(["anything"]) == 0
    assert "search failed" in capsys.readouterr().err


def test_query_runtime_error_exit0(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path); _make_index(tmp_path)
    eng = types.SimpleNamespace(
        cmd_query=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        cmd_index=lambda *a, **k: None)
    monkeypatch.setattr(w, "_load_engine", lambda: eng)
    monkeypatch.setattr(w, "_resolve_key", lambda: True)
    assert w.run(["anything"]) == 0
    assert "search failed" in capsys.readouterr().err


def test_resolve_key_uses_config_loader(monkeypatch):
    """_resolve_key reads via config.load_env_files (which honors ./.env) and
    sets os.environ. We mock the loader to avoid depending on the dev's real
    ~/.env precedence (config.py has its own precedence tests)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sys.path.insert(0, str(REPO))
    import config
    monkeypatch.setattr(config, "load_env_files",
                        lambda *a, **k: {"OPENAI_API_KEY": "sk-local-test"})
    assert w._resolve_key() is True
    assert os.environ.get("OPENAI_API_KEY") == "sk-local-test"
