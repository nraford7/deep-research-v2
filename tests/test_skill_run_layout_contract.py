from pathlib import Path


def test_skill_documents_project_local_v2_and_four_collision_choices():
    text = Path("SKILL.md").read_text(encoding="utf-8")
    for token in [
        "<project>/research/<run-slug>", "Sections/", "Sources/Extracted/",
        "Process/", "Resume", "Extend", "Start fresh", "Cancel",
    ]:
        assert token in text


def test_skill_export_invokes_managed_mode_and_finalize():
    text = Path("SKILL.md").read_text(encoding="utf-8")
    assert "scripts/export.py --run-dir" in text
    assert "scripts/run_manager.py finalize" in text


def test_skill_documents_immediate_migration_recovery_and_rollback():
    text = Path("SKILL.md").read_text(encoding="utf-8")
    assert "migrate \"$PROJECT\" --json" in text
    assert "--dry-run" in text
    assert "migration-recover" in text
    assert "rollback-migration" in text
