"""test_dispatch_mode.py — guard the slices-only dispatch entry point.

`dispatch.main()` is a thin orchestrator guide: it validates the run mode,
preflights EXA_API_KEY, and prints the ordered Round-1 command sequence
(scope → slice_search → evidence_gate → coverage_audit), plus a Round-4
reminder for lint_background. There is no legacy branch and no fleet dispatch.

Isolation: we monkeypatch `config.load_env_files` (and, defensively,
`config.default_toml_paths`) so NO live ~/.env or ~/.config TOML is read.
The Exa key is injected purely through the patched env dict.
"""

import pytest

import config
import dispatch
from scripts.run_transactions import create_skeleton_transaction


def _run_main(monkeypatch, argv, env):
    """Invoke dispatch.main() with a synthetic argv and a fully-controlled env.

    Returns the SystemExit code (or None if main() returned normally without
    raising). No live config/env files are ever consulted.
    """
    # Hermetic env: dispatch reads EXA_API_KEY only via config.load_env_files.
    monkeypatch.setattr(config, "load_env_files", lambda *a, **k: dict(env))
    # Defensive — dispatch does not read TOML, but pin it so a stray call can't
    # touch the user's ~/.config during the test.
    monkeypatch.setattr(config, "default_toml_paths", lambda: [])
    monkeypatch.setattr("sys.argv", ["dispatch.py"] + argv)
    try:
        dispatch.main()
    except SystemExit as exc:
        code = exc.code
        return code if code is not None else 0
    return None


def test_slices_mode_with_key_prints_sequence_exit_0(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    code = _run_main(
        monkeypatch,
        ["--topic", "grid battery", "--scope", "full scope",
         "--run-dir", str(run_dir), "--mode", "slices"],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    # main() returns normally (no SystemExit) on the happy path.
    assert code is None
    out = capsys.readouterr().out
    # The ordered scope → slice_search → evidence_gate → coverage_audit sequence prints.
    assert "scripts/scope.py" in out
    assert "scripts/slice_search.py" in out
    assert "scripts/evidence_gate.py" in out
    assert "scripts/citation_chase.py" in out
    assert "scripts/coverage_audit.py" in out
    # Ordering: scope before slice_search before evidence_gate before citation_chase
    # before coverage_audit.
    assert (out.index("scope.py") < out.index("slice_search.py")
            < out.index("evidence_gate.py") < out.index("citation_chase.py")
            < out.index("coverage_audit.py"))
    # lint_background is named as the Round-4 checklist step (not in the Round-1 order).
    assert "scripts/lint_background.py" in out
    # The run dir is threaded into each command.
    assert str(run_dir) in out


def test_slices_mode_is_the_default(monkeypatch, capsys, tmp_path):
    # No --mode flag → defaults to slices → prints the sequence, exit 0.
    run_dir = tmp_path / "d"
    run_dir.mkdir()
    code = _run_main(
        monkeypatch,
        ["--topic", "t", "--scope", "s", "--run-dir", str(run_dir)],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    assert code is None
    assert "slice_search.py" in capsys.readouterr().out


def test_legacy_mode_exits_2(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    code = _run_main(
        monkeypatch,
        ["--topic", "t", "--scope", "s",
         "--run-dir", str(tmp_path / "d"), "--mode", "legacy"],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    assert code == 2


def test_missing_exa_key_exits_20(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    # EXA_API_KEY absent from the (hermetic) env → preflight fails with exit 20.
    code = _run_main(
        monkeypatch,
        ["--topic", "t", "--scope", "s", "--run-dir", str(tmp_path / "d")],
        env={},
    )
    assert code == 20


def test_max_retrieval_cap_threads_into_slice_command(monkeypatch, capsys, tmp_path):
    (tmp_path / "d").mkdir()
    code = _run_main(
        monkeypatch,
        ["--topic", "t", "--scope", "s", "--run-dir", str(tmp_path / "d"),
         "--max-retrieval-usd", "8"],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    assert code is None
    out = capsys.readouterr().out
    assert "--max-retrieval-usd" in out and "8" in out


def test_explicit_run_dir_never_creates_unmanaged_directory(monkeypatch, tmp_path):
    run_dir = tmp_path / "missing"
    code = _run_main(
        monkeypatch,
        ["--topic", "t", "--scope", "s", "--run-dir", str(run_dir)],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    assert code == 2
    assert not run_dir.exists()


def test_project_dir_creates_project_local_v2_run(monkeypatch, capsys, tmp_path):
    code = _run_main(
        monkeypatch,
        ["--topic", "Grid Battery", "--scope", "s", "--question", "Grid Battery",
         "--project-dir", str(tmp_path)],
        env={"EXA_API_KEY": "exa-test-key"},
    )
    assert code is None
    run_dir = tmp_path / "Deeper_Research" / "grid-battery"
    assert (run_dir / "Process" / "run.json").is_file()
    out = capsys.readouterr().out
    assert "run_manager.py invoke-helper" in out
    assert str(run_dir / "Sections") in out


def test_existing_v2_run_is_resumed_through_managed_helpers(monkeypatch, capsys, tmp_path):
    library = tmp_path / "research"
    library.mkdir()
    run_dir = create_skeleton_transaction(library, "topic", question="Q").publish()

    code = _run_main(
        monkeypatch,
        ["--topic", "Q", "--scope", "s", "--run-dir", str(run_dir)],
        env={"EXA_API_KEY": "exa-test-key"},
    )

    assert code is None
    out = capsys.readouterr().out
    assert "--helper slice-search" in out
    assert "scripts/slice_search.py" not in out
