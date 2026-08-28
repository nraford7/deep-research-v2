"""coverage_audit --use-matrix — OFFLINE. The opt-in matrix report is default-off
and can never change the audit's behavior or return code. Reuses the same
monkeypatch stubs as test_coverage_audit.py (no network, no real subprocess).

Covers:
  1. --use-matrix writes round1/coverage_matrix.json on a success path; rc unchanged.
  2. default (no flag) writes NO report; behavior identical.
  3. a matrix-build error is swallowed — rc is still the audit's normal code.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import llm  # noqa: E402
from scripts import coverage_audit, coverage_matrix_adapter  # noqa: E402


def _provider():
    return config.Provider("kimi", "openai", "k", "m")


def _patch_provider(monkeypatch):
    monkeypatch.setattr(config, "default_toml_paths", lambda: [])
    monkeypatch.setattr(config, "load_env_files", lambda: {})
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({"kimi": _provider()}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {"utility": "kimi"})
    monkeypatch.setattr(config, "pick_provider", lambda providers, role, defaults: _provider())


def _patch_no_gaps(monkeypatch):
    """LLM reports zero gaps → the audit stops on the round-1 success path."""
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: json.dumps({"gaps": []}))


def _seed_run(tmp_path):
    """A scope.json (one hostname cell) + one anchor slice row whose host matches."""
    (tmp_path / "scope.json").write_text(json.dumps(
        {"ranked_domains": ["technology"], "domains": ["rand.org"]}), encoding="utf-8")
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True, exist_ok=True)
    (round1 / "slice_anchor.jsonl").write_text(json.dumps(
        {"url": "https://rand.org/a", "slice": "anchor", "tier": "peer_reviewed",
         "published_date": "2020"}) + "\n", encoding="utf-8")
    return round1


def test_use_matrix_writes_report(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    _patch_no_gaps(monkeypatch)
    round1 = _seed_run(tmp_path)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets",
        "--use-matrix", "--current-year", "2026",
    ])
    assert rc == 0
    report = round1 / "coverage_matrix.json"
    assert report.exists()
    data = json.loads(report.read_text())
    assert data["status"] in ("OPEN", "PARTIAL", "SATURATED")
    assert data["n_sources"] == 1
    assert "coverage_note" in data


def test_default_off_no_report(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    _patch_no_gaps(monkeypatch)
    round1 = _seed_run(tmp_path)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets",
    ])
    assert rc == 0
    # flag off → the report is never written; default behavior unchanged.
    assert not (round1 / "coverage_matrix.json").exists()


def test_matrix_build_error_does_not_change_rc(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    _patch_no_gaps(monkeypatch)
    round1 = _seed_run(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("intentional adapter failure")
    monkeypatch.setattr(coverage_matrix_adapter, "build_matrix", boom)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--use-matrix",
    ])
    assert rc == 0  # the audit's normal code, unperturbed by the emit failure
    assert not (round1 / "coverage_matrix.json").exists()  # nothing written on error


def test_emit_survives_broken_stderr(monkeypatch, tmp_path):
    """The except-handler's own notice print must not escape: a broken stderr
    during a matrix-build failure must still leave the audit's rc unchanged."""
    import builtins

    _patch_provider(monkeypatch)
    _patch_no_gaps(monkeypatch)
    _seed_run(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("adapter down")
    monkeypatch.setattr(coverage_matrix_adapter, "build_matrix", boom)

    real_print = builtins.print

    def raising_print(*a, **k):
        # simulate a broken pipe specifically on the handler's notice line
        if a and isinstance(a[0], str) and a[0].startswith("coverage-matrix: skipped"):
            raise BrokenPipeError("stderr closed")
        return real_print(*a, **k)
    monkeypatch.setattr(builtins, "print", raising_print)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--use-matrix",
    ])
    assert rc == 0  # broken-stderr in the handler is swallowed; rc unchanged
