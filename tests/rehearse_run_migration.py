#!/usr/bin/env python3
"""Temporary, deterministic rehearsal for legacy migration and v2 publication."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from scripts import export
from scripts.run_extension import prepare_extension
from scripts.run_layout import LayoutKind, RunLayout
from scripts.run_manager import broker_request
from scripts.run_migration import apply_migration, plan_migration, recover_migration, rollback_migration
from scripts.run_transactions import TreeInventory
from scripts.search import select_documents


def seed(root: Path, name: str, *, bible: bool = True) -> Path:
    run = root / name
    (run / "sections").mkdir(parents=True)
    (run / "round1" / "sources").mkdir(parents=True)
    (run / "export").mkdir()
    (run / "sections" / "01-findings.md").write_text(
        "# Findings\n\nGrounded claim [Smith, 2024].\n", encoding="utf-8")
    (run / "export" / "bibliography.md").write_text(
        "# Bibliography\n\n- Smith, A. (2024). A sufficiently long source title. https://example.com/source\n",
        encoding="utf-8",
    )
    (run / "round1" / "sources" / "source.txt").write_text("evidence", encoding="utf-8")
    (run / "round1" / "slice_web.jsonl").write_text(json.dumps({
        "url": "https://example.com/source", "text_path": "sources/source.txt",
    }) + "\n", encoding="utf-8")
    if bible:
        (run / f"RESEARCH-BIBLE_{name}.md").write_text(f"# {name} Research Bible\n", encoding="utf-8")
    return run


def main() -> None:
    repository = REPOSITORY
    with tempfile.TemporaryDirectory(prefix="deeper-research-rehearsal-") as temporary:
        project = Path(temporary) / "project"
        project.mkdir()
        direct = seed(project, "persona-construction")
        nested = seed(project / "research", "western-philosophy-of-mind")
        incomplete = seed(project / "research", "incomplete", bible=False)
        originals = {path.name: TreeInventory.capture(path).root_digest for path in (direct, nested, incomplete)}

        plans = [
            plan_migration(direct, project / "research"),
            plan_migration(nested, nested.parent),
            plan_migration(incomplete, incomplete.parent),
        ]
        assert {path.name: TreeInventory.capture(path).root_digest for path in (direct, nested, incomplete)} == originals

        try:
            apply_migration(plans[0], crash_at="after-source-backup")
        except RuntimeError:
            pass
        recover_migration(project / "research", mode="continue")
        migrated_direct = project / "research" / direct.name
        migrated_nested = apply_migration(plans[1])
        migrated_incomplete = apply_migration(plans[2])
        for run in (migrated_direct, migrated_nested, migrated_incomplete):
            assert RunLayout.open(run).kind is LayoutKind.V2

        export.main(["--run-dir", str(migrated_nested)])
        assert (migrated_nested / "RESEARCH-BIBLE_western-philosophy-of-mind.html").is_file()
        assert (migrated_nested / "Sources" / "claims.jsonl").is_file()

        # A migrated partial becomes eligible for extension only after a fresh
        # evidence-gate record. Its prior publication stays orientation-only.
        (migrated_incomplete / "Process" / "stages" / "evidence_gate.json").parent.mkdir(parents=True, exist_ok=True)
        (migrated_incomplete / "Process" / "stages" / "evidence_gate.json").write_text("{}\n")
        extension = prepare_extension(migrated_incomplete, "Expanded synthesis")
        assert (extension.child / "Process" / "lineage.json").is_file()
        broker_request(extension.prepared.broker_endpoint, extension.prepared.lease_token, "release")

        documents = select_documents(project / "research")
        logical_ids = [doc.logical_id for values in documents.values() for doc in values]
        assert len(logical_ids) == len(set(logical_ids))

        restored = rollback_migration(migrated_direct)
        assert TreeInventory.capture(restored).root_digest == originals[direct.name]
        assert repository not in restored.parents and restored != repository

    print("REHEARSAL PASS")


if __name__ == "__main__":
    main()
