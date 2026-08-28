from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import run_manager
from scripts.run_manager import Exit, ManagerError, prepare_run
from scripts.run_transactions import broker_request, create_skeleton_transaction


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = ("directory", None) if path.is_dir() else ("file", path.read_bytes())
    return result


def _release(result: run_manager.PrepareResult) -> None:
    if result.broker_endpoint and result.lease_token:
        broker_request(result.broker_endpoint, result.lease_token, "release")


def test_prepare_defaults_to_captured_project_research(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = prepare_run(question="Q", slug="topic")
    try:
        assert result.action == "created"
        assert result.run_dir == tmp_path / "research" / "topic"
        assert (result.run_dir / "Process" / "run.json").exists()
        assert result.lease_keeper_pid
        assert result.broker_endpoint
        assert result.scratch_dir and result.scratch_dir.is_dir()
    finally:
        _release(result)


def test_collision_without_mode_is_a_zero_write_result(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    create_skeleton_transaction(library, "topic", question="Q").publish()
    before = _snapshot(tmp_path)
    result = prepare_run(question="Q", slug="topic", project_dir=tmp_path)
    assert result.action == "mode-required"
    assert result.choices == ("resume", "extend", "fresh", "cancel")
    assert _snapshot(tmp_path) == before


def test_cancel_is_a_clean_skip_with_no_writes(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    create_skeleton_transaction(library, "topic", question="Q").publish()
    before = _snapshot(tmp_path)
    result = prepare_run(question="Q", slug="topic", project_dir=tmp_path, mode="cancel")
    assert result.action == "cancelled"
    assert result.run_dir is None
    assert _snapshot(tmp_path) == before


def test_frozen_resume_is_rejected_but_fresh_gets_untouched_sibling(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    parent = create_skeleton_transaction(library, "topic", question="Q").publish()
    metadata_path = parent / "Process" / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(status="frozen", frozen_for_derivation=True, frozen_snapshot="root")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    parent_before = _snapshot(parent)
    with pytest.raises(ManagerError) as caught:
        prepare_run(question="Q", slug="topic", library_dir=library, mode="resume")
    assert caught.value.exit_code is Exit.FROZEN_PARENT

    result = prepare_run(question="Q", slug="topic", library_dir=library, mode="fresh")
    try:
        assert result.action == "fresh"
        assert result.run_dir and result.run_dir.name.startswith("topic-")
        assert result.run_dir != parent
        assert _snapshot(parent) == parent_before
    finally:
        _release(result)


def test_corrupt_or_nonrun_collision_offers_only_fresh_and_cancel(tmp_path: Path) -> None:
    library = tmp_path / "research"
    occupied = library / "topic"
    (occupied / "Process").mkdir(parents=True)
    (occupied / "Process" / "run.json").write_text("{", encoding="utf-8")
    before = _snapshot(tmp_path)
    result = prepare_run(question="Q", slug="TOPIC", library_dir=library)
    assert result.action == "mode-required"
    assert result.classification == "corrupt"
    assert result.choices == ("fresh", "cancel")
    assert _snapshot(tmp_path) == before


def test_dry_run_is_deterministic_and_performs_no_writes(tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    first = prepare_run(question="Q", slug="topic", project_dir=tmp_path, dry_run=True)
    second = prepare_run(question="Q", slug="topic", project_dir=tmp_path, dry_run=True)
    assert first.to_dict() == second.to_dict()
    assert first.action == "plan-create"
    assert first.write_probe_pending is True
    assert _snapshot(tmp_path) == before


def test_resume_returns_dependency_plan_and_live_broker(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    run = create_skeleton_transaction(library, "topic", question="Q").publish()
    (run / "Process" / "scope.json").write_text("scope", encoding="utf-8")
    result = prepare_run(question="Q", slug="topic", library_dir=library, mode="resume")
    try:
        assert result.action == "resumed"
        assert result.resume_plan and result.resume_plan.restart_stage == "round0"
        assert result.broker_endpoint
    finally:
        _release(result)


def test_broker_publishes_only_stage_allowed_scratch_artifacts_and_records_stage(tmp_path: Path) -> None:
    result = prepare_run(question="Q", slug="topic", project_dir=tmp_path)
    assert result.scratch_dir and result.broker_endpoint and result.lease_token and result.run_dir
    try:
        scratch = result.scratch_dir / "scope.json"
        scratch.write_text('{"question":"Q"}\n', encoding="utf-8")
        data = scratch.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        published = broker_request(
            result.broker_endpoint,
            result.lease_token,
            "publish-artifact",
            logical_destination="Process/scope.json",
            scratch_name="scope.json",
            sha256=digest,
            size=len(data),
            stage="round0",
        )
        assert published["path"] == "Process/scope.json"
        broker_request(
            result.broker_endpoint,
            result.lease_token,
            "record-stage",
            stage="round0",
            manifest={"inputs": [], "outputs": ["Process/scope.json"], "dependencies": [], "tool": "scope"},
        )
        assert (result.run_dir / "Process" / "stages" / "round0.json").exists()
        with pytest.raises(Exception, match="not allowed"):
            broker_request(
                result.broker_endpoint,
                result.lease_token,
                "publish-artifact",
                logical_destination="Process/run.json",
                scratch_name="scope.json",
                sha256=digest,
                size=len(data),
                stage="round0",
            )
    finally:
        _release(result)


def test_broker_runs_allowlisted_mutating_helper_under_lease(tmp_path: Path) -> None:
    result = prepare_run(question="Q", slug="topic", project_dir=tmp_path)
    try:
        response = broker_request(
            result.broker_endpoint,
            result.lease_token,
            "invoke-helper",
            helper_id="fetch-fulltext",
            args={},
        )
        assert response == {"exit_code": 0}
    finally:
        _release(result)


def test_cli_mode_required_uses_stable_exit_code_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = tmp_path / "research"
    library.mkdir()
    create_skeleton_transaction(library, "topic", question="Q").publish()
    code = run_manager.run(
        ["prepare", "--library-dir", str(library), "--slug", "topic", "--question", "Q", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == Exit.MODE_REQUIRED
    assert payload["action"] == "mode-required"


def test_short_lived_cli_hands_off_a_live_detached_broker(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_manager.py",
            "prepare",
            "--project-dir",
            str(tmp_path),
            "--slug",
            "topic",
            "--question",
            "Q",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
        timeout=5,
    )
    payload = json.loads(completed.stdout)
    renewed = broker_request(payload["broker_endpoint"], payload["lease_token"], "renew")
    assert renewed["renewed_until"]
    broker_request(payload["broker_endpoint"], payload["lease_token"], "release")


def test_status_is_read_only(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    run = create_skeleton_transaction(library, "topic", question="Q").publish()
    before = _snapshot(tmp_path)
    status = run_manager.status_run(run)
    assert status["layout"] == "v2"
    assert status["status"] == "incomplete"
    assert _snapshot(tmp_path) == before
