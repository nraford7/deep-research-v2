import hashlib
import json

import pytest

from scripts.run_layout import LayoutKind, RunLayout
from scripts.run_migration import (
    RollbackConflict,
    apply_migration,
    plan_migration,
    recover_migration,
    rollback_migration,
)
from scripts.run_manager import run as manager_run
from scripts.run_transactions import TreeInventory


def _legacy(library, name="topic"):
    run = library / name
    (run / "sections").mkdir(parents=True)
    (run / "round1" / "sources").mkdir(parents=True)
    (run / "export").mkdir()
    (run / "sections" / "01.md").write_text("# Findings\n")
    (run / "export" / "bibliography.md").write_text("# Bibliography\n")
    (run / "round1" / "sources" / "a.txt").write_text("evidence")
    (run / "round1" / "slice_web.jsonl").write_text(json.dumps({
        "url": "https://example.com", "text_path": "sources/a.txt",
    }) + "\n")
    (run / f"RESEARCH-BIBLE_{name}.md").write_text("# Bible\n")
    return run


def test_apply_and_rollback_preserve_exact_legacy_tree(tmp_path):
    legacy = _legacy(tmp_path / "research")
    before = TreeInventory.capture(legacy)

    migrated = apply_migration(plan_migration(legacy, legacy.parent))

    assert RunLayout.open(migrated).kind is LayoutKind.V2
    assert (migrated / "Sources" / "Extracted" / "a.txt").is_file()
    restored = rollback_migration(migrated)
    assert restored == legacy
    assert TreeInventory.capture(legacy).root_digest == before.root_digest


def test_rollback_refuses_unrecorded_or_changed_entry(tmp_path):
    legacy = _legacy(tmp_path / "research")
    migrated = apply_migration(plan_migration(legacy, legacy.parent))
    (migrated / "Process" / "round1" / "unrecorded.txt").write_text("x")

    with pytest.raises(RollbackConflict, match="unrecorded"):
        rollback_migration(migrated)


def test_rollback_refuses_tampered_migration_metadata(tmp_path):
    legacy = _legacy(tmp_path / "research")
    migrated = apply_migration(plan_migration(legacy, legacy.parent))
    metadata = migrated / "Process" / "migration.json"
    payload = json.loads(metadata.read_text())
    payload["original_source"] = str(tmp_path / "redirected" / "topic")
    metadata.write_text(json.dumps(payload) + "\n")

    with pytest.raises(RollbackConflict, match="digest"):
        rollback_migration(migrated)


def test_rollback_refuses_rehashed_metadata_that_disagrees_with_transaction(tmp_path):
    legacy = _legacy(tmp_path / "research")
    migrated = apply_migration(plan_migration(legacy, legacy.parent))
    metadata = migrated / "Process" / "migration.json"
    payload = json.loads(metadata.read_text())
    payload["original_source"] = str(tmp_path / "redirected" / "topic")
    metadata.write_text(json.dumps(payload) + "\n")
    digest = hashlib.sha256(metadata.read_bytes()).hexdigest()
    (migrated / "Process" / "migration.sha256").write_text(digest + "\n")

    with pytest.raises(RollbackConflict, match="transaction anchor"):
        rollback_migration(migrated)


def test_direct_project_child_relocates_and_rolls_back(tmp_path):
    source = _legacy(tmp_path, "legacy-topic")
    before = TreeInventory.capture(source)
    research = tmp_path / "research"

    migrated = apply_migration(plan_migration(source, research))

    assert migrated == research / "legacy-topic"
    assert not source.exists()
    rollback_migration(migrated)
    assert TreeInventory.capture(source).root_digest == before.root_digest
    assert not migrated.exists()


def test_continue_and_abort_recover_source_backup_boundary(tmp_path):
    first = _legacy(tmp_path / "research", "continue")
    with pytest.raises(RuntimeError, match="after-source-backup"):
        apply_migration(plan_migration(first, first.parent), crash_at="after-source-backup")
    assert not first.exists()
    outcome = recover_migration(first.parent, mode="continue")
    assert outcome[-1]["status"] == "committed"
    assert RunLayout.open(first).kind is LayoutKind.V2

    second = _legacy(tmp_path / "research", "abort")
    before = TreeInventory.capture(second)
    with pytest.raises(RuntimeError, match="after-source-backup"):
        apply_migration(plan_migration(second, second.parent), crash_at="after-source-backup")
    outcome = recover_migration(second.parent, mode="abort")
    assert any(item["status"] == "aborted" and item["path"] == str(second) for item in outcome)
    assert TreeInventory.capture(second).root_digest == before.root_digest


def test_continue_recovers_from_fully_staged_intent(tmp_path):
    legacy = _legacy(tmp_path / "research", "staged")
    with pytest.raises(RuntimeError, match="after-stage"):
        apply_migration(plan_migration(legacy, legacy.parent), crash_at="after-stage")

    outcomes = recover_migration(legacy.parent, mode="continue")

    assert any(item["status"] == "committed" and item["path"] == str(legacy) for item in outcomes)
    assert RunLayout.open(legacy).kind is LayoutKind.V2


def test_embedded_inverse_survives_external_backup_removal(tmp_path):
    legacy = _legacy(tmp_path / "research")
    before = TreeInventory.capture(legacy)
    migrated = apply_migration(plan_migration(legacy, legacy.parent))
    migration = json.loads((migrated / "Process" / "migration.json").read_text())
    tx = migrated.parent / ".transactions" / f"migration-{migration['transaction_id']}"
    external = tx / "legacy-backup"
    if external.exists():
        import shutil
        shutil.rmtree(external)

    rollback_migration(migrated)

    assert TreeInventory.capture(legacy).root_digest == before.root_digest


def test_manager_migrates_immediately_and_can_rollback(tmp_path):
    legacy = _legacy(tmp_path / "research")
    before = TreeInventory.capture(legacy)

    assert manager_run(["migrate", str(legacy), "--json"]) == 0
    assert RunLayout.open(legacy).kind is LayoutKind.V2
    assert manager_run(["rollback-migration", str(legacy), "--json"]) == 0
    assert TreeInventory.capture(legacy).root_digest == before.root_digest
