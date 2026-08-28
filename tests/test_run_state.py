from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_fs import RootedFS, UnsafePathError
from scripts.run_state import (
    LifecycleError,
    RunMetadata,
    make_state_guard,
    record_stage,
    derive_legacy_metadata,
    recover_seal,
    resume_plan,
    seal_run,
    seal_legacy_complete,
    transition_status,
    validate_completion,
    validate_legacy_completion,
    validate_seal,
)
from scripts.run_transactions import ImmutableRegistry, Journal, create_skeleton_transaction


@pytest.fixture
def v2_run(tmp_path: Path) -> Path:
    library = tmp_path / "research"
    library.mkdir()
    return create_skeleton_transaction(library, "topic", question="What is true?").publish()


@pytest.fixture
def legacy_run(tmp_path: Path) -> Path:
    run = tmp_path / "legacy"
    (run / "round1").mkdir(parents=True)
    (run / "scope.json").write_text('{"question":"Q"}', encoding="utf-8")
    return run


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_native_artifacts(run: Path) -> None:
    _write(run / "Sections" / "01.md", "# Finding\nGrounded finding [Source, 2025].\n")
    _write(run / "Sources" / "Extracted" / "source.txt", "evidence\n")
    _write(run / "Sources" / "bibliography.md", "# Bibliography\n- Source (2025)\n")
    _write(run / "Sources" / "bibliography.bib", "@article{source2025, title={Source}}\n")
    _write(
        run / "Sources" / "claims.jsonl",
        json.dumps({"file": "Sections/01.md", "sentence": "Grounded finding", "citations": []}) + "\n",
    )
    markdown = run / "RESEARCH-BIBLE_topic.md"
    html = run / "RESEARCH-BIBLE_topic.html"
    _write(markdown, "# Topic\n\n## Finding\nGrounded finding.\n")
    _write(html, "<!doctype html><html><body><h1>Topic</h1><p>Grounded finding.</p></body></html>\n")
    metadata_path = run / "Process" / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["bible"] = {
        "markdown": markdown.name,
        "html": html.name,
        "markdown_sha256": _sha(markdown),
        "html_sha256": _sha(html),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    stages = [
        ("round0", "Process/scope.json"),
        ("round1", "Process/round1/retrieval.json"),
        ("evidence_gate", "Process/round1/evidence-gate.json"),
        ("integration", "Process/round3/integration.json"),
        ("citation_verifier", "Process/round4/citation-verifier.json"),
        ("adversary", "Process/round4/adversary.json"),
        ("export", "Process/export_manifest.json"),
    ]
    previous: list[str] = []
    for stage, output in stages:
        _write(run / output, json.dumps({"stage": stage, "ok": True}))
        record_stage(
            run,
            stage,
            inputs=[] if not previous else [stages[len(previous) - 1][1]],
            outputs=[output],
            dependencies=previous[-1:],
            tool=stage,
            config_fingerprint="config-v1",
        )
        previous.append(stage)


def test_resume_restarts_at_earliest_invalid_dependency(v2_run: Path) -> None:
    _write(v2_run / "Process" / "scope.json", "original")
    record_stage(v2_run, "round0", inputs=[], outputs=["Process/scope.json"], tool="scope")
    _write(v2_run / "Process" / "round1" / "slice.jsonl", "{}\n")
    record_stage(
        v2_run,
        "round1",
        inputs=["Process/scope.json"],
        outputs=["Process/round1/slice.jsonl"],
        dependencies=["round0"],
        tool="slice",
    )
    (v2_run / "Process" / "scope.json").write_text("changed", encoding="utf-8")
    plan = resume_plan(v2_run)
    assert plan.restart_stage == "round0"
    assert any("input" in reason or "output" in reason for reason in plan.invalid_reasons)


def test_resume_reuses_current_upstream_outputs(v2_run: Path) -> None:
    _write(v2_run / "Process" / "scope.json", "original")
    record_stage(v2_run, "round0", inputs=[], outputs=["Process/scope.json"], tool="scope")
    plan = resume_plan(v2_run)
    assert plan.restart_stage == "round1"
    assert "Process/scope.json" in plan.reusable_outputs


def test_native_complete_requires_current_gate_verifier_adversary_and_exports(v2_run: Path) -> None:
    result = validate_completion(v2_run)
    assert not result.ok
    assert {"evidence_gate", "integration", "adversary", "bible_html"} <= set(result.missing)


def test_native_completion_and_seal_require_a_current_compound_tree(v2_run: Path) -> None:
    _complete_native_artifacts(v2_run)
    assert validate_completion(v2_run).ok
    seal = seal_run(v2_run)
    assert seal["ordinary_content_root"]
    assert seal["self_commitment"]
    assert validate_seal(v2_run).ok
    metadata = RunMetadata.load(v2_run)
    assert metadata.status == "complete"
    assert metadata.sealed is True

    guarded = RootedFS(v2_run, state_guard=make_state_guard(v2_run))
    with pytest.raises(UnsafePathError, match="sealed or frozen"):
        guarded.atomic_write_text("Process/round4/tamper.txt", "no")


def test_claim_path_escape_blocks_native_completion(v2_run: Path) -> None:
    _complete_native_artifacts(v2_run)
    (v2_run / "Sources" / "claims.jsonl").write_text(
        json.dumps({"file": "../outside.md", "sentence": "bad", "citations": []}) + "\n",
        encoding="utf-8",
    )
    result = validate_completion(v2_run)
    assert not result.ok
    assert "claims" in result.missing


def test_migrated_legacy_profile_accepts_one_valid_h1_bible(legacy_run: Path) -> None:
    (legacy_run / "export").mkdir()
    (legacy_run / "export" / "topic-BIBLE.md").write_text("# Topic\nBody", encoding="utf-8")
    assert validate_legacy_completion(legacy_run).ok


def test_migrated_legacy_profile_rejects_ambiguous_or_headingless_bible(legacy_run: Path) -> None:
    (legacy_run / "export").mkdir()
    (legacy_run / "export" / "topic-BIBLE.md").write_text("Body", encoding="utf-8")
    assert not validate_legacy_completion(legacy_run).ok


def test_completed_legacy_run_gets_external_seal_without_tree_mutation(legacy_run: Path) -> None:
    (legacy_run / "export").mkdir()
    (legacy_run / "export" / "topic-BIBLE.md").write_text("# Topic\nBody", encoding="utf-8")
    before = {path.relative_to(legacy_run): path.read_bytes() for path in legacy_run.rglob("*") if path.is_file()}
    synthetic = derive_legacy_metadata(legacy_run)
    transaction_id = "legacy-derivation"
    journal = Journal(
        legacy_run.parent / ".transactions" / transaction_id / "journal.jsonl",
        transaction_id,
        synthetic["run_id"],
    )
    record = seal_legacy_complete(legacy_run, library=legacy_run.parent, journal=journal)
    assert record.kind == "legacy-complete-seal"
    assert record.run_id == synthetic["run_id"]
    assert ImmutableRegistry(legacy_run.parent).resolve(legacy_run) == record
    after = {path.relative_to(legacy_run): path.read_bytes() for path in legacy_run.rglob("*") if path.is_file()}
    assert after == before
    (legacy_run / "export" / "other-BIBLE.md").write_text("# Other\nBody", encoding="utf-8")
    assert not validate_legacy_completion(legacy_run).ok


def test_lifecycle_rejects_terminal_and_illegal_transitions(v2_run: Path) -> None:
    transition_status(v2_run, "failed")
    transition_status(v2_run, "incomplete")
    with pytest.raises(LifecycleError):
        transition_status(v2_run, "new")
    with pytest.raises(LifecycleError, match="seal_run"):
        transition_status(v2_run, "complete")
    transition_status(v2_run, "frozen", frozen_snapshot="snapshot-root")
    with pytest.raises(LifecycleError, match="terminal"):
        transition_status(v2_run, "incomplete")


def test_migrated_v2_completion_preserves_historical_profile_and_seals(v2_run: Path) -> None:
    bible = v2_run / "Historical-Research-Bible.md"
    _write(bible, "# Historical Topic\nBody\n")
    metadata_path = v2_run / "Process" / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["completion_profile"] = "migrated-legacy-v1"
    metadata["bible"] = {
        "markdown": bible.name,
        "html": None,
        "markdown_sha256": _sha(bible),
        "html_sha256": None,
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _write(
        v2_run / "Process" / "migration.json",
        json.dumps({"schema_version": 1, "missing_native_requirements": ["bible_html", "claims"]}),
    )
    assert validate_completion(v2_run).ok
    seal_run(v2_run)
    assert validate_seal(v2_run).ok


@pytest.mark.parametrize("crash_at", ["after-journal", "after-metadata", "after-seal"])
def test_seal_recovery_returns_a_fully_validated_sealed_state(v2_run: Path, crash_at: str) -> None:
    _complete_native_artifacts(v2_run)
    transaction_id = None
    try:
        seal_run(v2_run, crash_at=crash_at)
    except SystemExit as exc:
        assert exc.code == 91
        transaction_id = exc.transaction_id
    assert transaction_id is not None
    recover_seal(v2_run, transaction_id, continue_seal=True)
    assert RunMetadata.load(v2_run).sealed is True
    assert validate_seal(v2_run).ok
