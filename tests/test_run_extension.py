import json
import os

from scripts.run_extension import prepare_extension
from scripts.run_manager import broker_request, prepare_run
from scripts.run_state import RunMetadata
from scripts.run_transactions import TreeInventory


def _partial_parent(tmp_path):
    prepared = prepare_run(question="Original", slug="topic", project_dir=tmp_path)
    broker_request(prepared.broker_endpoint, prepared.lease_token, "release")
    run = prepared.run_dir
    stage = run / "Process" / "stages" / "evidence_gate.json"
    stage.write_text("{}\n")
    source = run / "Sources" / "Extracted" / "source.txt"
    source.write_text("inherited evidence")
    (run / "Process" / "round1").mkdir()
    (run / "Process" / "round1" / "slice_web.jsonl").write_text(json.dumps({
        "title": "Source", "url": "https://example.com", "text_path": "Sources/Extracted/source.txt",
    }) + "\n")
    (run / "RESEARCH-BIBLE_topic.md").write_text("# Prior orientation\n")
    return run, source


def test_partial_parent_freezes_and_child_inherits_full_provenance(tmp_path):
    parent, source = _partial_parent(tmp_path)
    before_source = os.stat(source).st_ino

    result = prepare_extension(parent, "What changes under X?")

    child = result.child
    assert (child / "Process" / "Inherited" / parent.name / "snapshot" / "snapshot.json").is_file()
    rows = [json.loads(line) for line in (child / "Process" / "round1" / "inherited_corpus.jsonl").read_text().splitlines()]
    assert rows[0]["text_path"].startswith("Sources/Extracted/inherited/")
    inherited_source = child / rows[0]["text_path"]
    assert inherited_source.read_text() == "inherited evidence"
    assert os.stat(inherited_source).st_ino != before_source
    lineage = json.loads((child / "Process" / "lineage.json").read_text())
    assert lineage["prior_bible_role"] == "orientation_only"
    assert RunMetadata.load(parent).frozen_for_derivation is True
    broker_request(result.prepared.broker_endpoint, result.prepared.lease_token, "release")


def test_extension_does_not_put_prior_bible_in_active_corpus(tmp_path):
    parent, _ = _partial_parent(tmp_path)

    result = prepare_extension(parent, "New synthesis question")

    corpus = (result.child / "Process" / "round1" / "inherited_corpus.jsonl").read_text()
    assert "Prior orientation" not in corpus
    assert not list(result.child.glob("RESEARCH-BIBLE_*.md"))
    snapshot_bible = result.child / "Process" / "Inherited" / parent.name / "snapshot" / "tree" / "RESEARCH-BIBLE_topic.md"
    assert snapshot_bible.is_file()
    broker_request(result.prepared.broker_endpoint, result.prepared.lease_token, "release")


def test_extension_dry_run_is_read_only(tmp_path):
    parent, _ = _partial_parent(tmp_path)
    before = TreeInventory.capture(parent.parent)

    plan = prepare_extension(parent, "Q2", dry_run=True)

    assert plan.action == "plan-fresh"
    assert TreeInventory.capture(parent.parent) == before


def test_manager_extend_returns_new_child_with_broker_handoff(tmp_path):
    parent, _ = _partial_parent(tmp_path)

    result = prepare_run(
        question="Expanded question", slug=parent.name, project_dir=tmp_path, mode="extend"
    )

    assert result.action == "extended"
    assert result.run_dir != parent
    assert result.broker_endpoint and result.lease_token
    assert (result.run_dir / "Process" / "lineage.json").is_file()
    broker_request(result.broker_endpoint, result.lease_token, "release")
