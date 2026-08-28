import json

from scripts.run_migration import discover_targets, plan_migration
from scripts.run_manager import run as manager_run
from scripts.run_transactions import TreeInventory


def _legacy(library, name="persona-construction"):
    run = library / name
    (run / "sections").mkdir(parents=True)
    (run / "round1" / "sources").mkdir(parents=True)
    (run / "export").mkdir()
    (run / "sections" / "01-findings.md").write_text("# Findings\n")
    (run / "sections" / "bibliography.md").write_text("# Old bibliography\n")
    (run / "export" / "bibliography.md").write_text("# Master bibliography\n")
    (run / "export" / "bibliography.bib").write_text("@misc{x}\n")
    (run / "export" / "claims.jsonl").write_text(json.dumps({
        "file": "sections/01-findings.md", "sentence": "Claim",
    }) + "\n")
    (run / "round1" / "sources" / "a.txt").write_text("evidence")
    (run / "round1" / "slice_publication.jsonl").write_text(json.dumps({
        "url": "https://example.com", "text_path": "sources/a.txt",
    }) + "\n")
    (run / f"{name}-BIBLE.md").write_text("# Bible\n")
    (run / "scope.json").write_text("{}\n")
    return run


def test_plan_uses_ordered_nonoverlapping_mapping(tmp_path):
    run = _legacy(tmp_path / "research")

    plan = plan_migration(run, run.parent)

    assert plan.dest("export/bibliography.md") == "Sources/bibliography.md"
    assert plan.dest("round1/sources/a.txt") == "Sources/Extracted/a.txt"
    assert plan.dest("round1/slice_publication.jsonl") == "Process/round1/slice_publication.jsonl"
    assert plan.dest("sections/bibliography.md") == "Process/Legacy/sections/bibliography.md"


def test_dry_plan_rewrites_registered_paths_without_writes(tmp_path):
    run = _legacy(tmp_path / "research", "western-philosophy-of-mind")
    before = TreeInventory.capture(run)

    plan = plan_migration(run, run.parent)

    assert plan.preview_rewrite("text_path", "sources/a.txt") == "Sources/Extracted/a.txt"
    claims = next(op for op in plan.rewrites if op.source == "export/claims.jsonl")
    assert json.loads(claims.content)["file"] == "Sections/01-findings.md"
    assert TreeInventory.capture(run) == before


def test_discovery_finds_project_research_children_once(tmp_path):
    first = _legacy(tmp_path / "research", "one")
    second = _legacy(tmp_path / "research", "two")

    targets = discover_targets(tmp_path)

    assert [target.source for target in targets] == [first, second]


def test_manager_migrate_dry_run_is_read_only(tmp_path, capsys):
    legacy = _legacy(tmp_path / "research", "topic")
    before = TreeInventory.capture(tmp_path)

    assert manager_run(["migrate", str(legacy), "--dry-run", "--json"]) == 0

    assert TreeInventory.capture(tmp_path) == before
    assert '"dry_run": true' in capsys.readouterr().out
