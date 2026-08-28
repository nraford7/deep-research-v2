from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import background, evidence_gate, fetch_fulltext, scope, slice_search
from scripts.helper_runtime import resolve_helper_layout
from scripts.ledger import RetrievalLedger
from scripts.run_fs import RootedFS, UnsafePathError
from scripts.run_layout import LayoutKind, RunLayout
from scripts.run_state import make_state_guard
from scripts.run_transactions import create_skeleton_transaction


@pytest.fixture
def v2_run(tmp_path: Path) -> Path:
    library = tmp_path / "research"
    library.mkdir()
    return create_skeleton_transaction(library, "topic", question="Q").publish()


@pytest.mark.parametrize("kind", ["legacy", "v2"])
def test_evidence_gate_reads_logical_round1(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if kind == "v2":
        library = tmp_path / "research"
        library.mkdir()
        run = create_skeleton_transaction(library, "topic", question="Q").publish()
        round1 = run / "Process" / "round1"
        round1.mkdir()
    else:
        run = tmp_path / "legacy"
        round1 = run / "round1"
        round1.mkdir(parents=True)
    rows = [
        {"url": f"https://example.com/{index}", "tier": "T1", "title": str(index)}
        for index in range(4)
    ]
    (round1 / "slice_publication.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(evidence_gate.config, "load_run_config", lambda: type("C", (), {
        "min_evidence_total": 1,
        "min_nonempty_slices": 1,
    })())
    assert evidence_gate.evaluate(run)["passed"] is True


def test_ledger_uses_process_home_for_v2_and_root_for_legacy(v2_run: Path, tmp_path: Path) -> None:
    RetrievalLedger(v2_run, 5)
    assert (v2_run / "Process" / "retrieval_ledger.json").is_file()
    assert not (v2_run / "retrieval_ledger.json").exists()

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    RetrievalLedger(legacy, 5)
    assert (legacy / "retrieval_ledger.json").is_file()


def test_v2_fulltext_writes_run_relative_source_refs(v2_run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    round1 = v2_run / "Process" / "round1"
    round1.mkdir()
    row = {"url": "https://example.com/paper", "title": "Paper", "slice": "web", "text_chars": 0}
    slice_path = round1 / "slice_web.jsonl"
    slice_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        fetch_fulltext,
        "_fetch_row_text",
        lambda *args, **kwargs: ("full text", "html", b"<html>raw</html>", "html"),
    )
    fetch_fulltext.process_run(v2_run, min_chars=1, max_bytes=1000, max_chars=1000, timeout=1)
    updated = json.loads(slice_path.read_text(encoding="utf-8"))
    assert updated["text_path"].startswith("Sources/Extracted/")
    assert updated["raw_path"].startswith("Sources/Extracted/")
    assert (v2_run / updated["text_path"]).is_file()
    assert (v2_run / updated["raw_path"]).is_file()


def test_slice_spill_uses_sources_extracted_for_v2(v2_run: Path) -> None:
    layout = RunLayout.open(v2_run)
    items = [{"url": "https://example.com", "title": "Example", "text": "long body"}]
    slice_search._spill_fulltext(items, layout.round1, layout=layout)
    assert items[0]["text_path"].startswith("Sources/Extracted/")
    assert (v2_run / items[0]["text_path"]).read_text(encoding="utf-8") == "long body"


def test_managed_scope_writes_only_canonical_json(v2_run: Path) -> None:
    layout = RunLayout.open(v2_run)
    fs = RootedFS(v2_run)
    result = scope.managed_scope(
        layout=layout,
        fs=fs,
        typed_args={"topic": "central bank policy", "scope": "global", "use_llm": False},
    )
    assert result["path"] == "Process/scope.json"
    payload = json.loads((v2_run / "Process" / "scope.json").read_text(encoding="utf-8"))
    assert payload["topic"] == "central bank policy"
    assert not (v2_run / "scope.json").exists()


def test_managed_scope_respects_sealed_state_guard(v2_run: Path) -> None:
    metadata_path = v2_run / "Process" / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(status="complete", sealed=True)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    layout = RunLayout.open(v2_run)
    fs = RootedFS(v2_run, state_guard=make_state_guard(layout))
    with pytest.raises(UnsafePathError, match="sealed or frozen"):
        scope.managed_scope(layout=layout, fs=fs, typed_args={"topic": "T", "scope": "", "use_llm": False})


def test_background_resolves_v2_run_relative_extracted_text(v2_run: Path) -> None:
    extracted = v2_run / "Sources" / "Extracted" / "source.txt"
    extracted.write_text("corpus evidence", encoding="utf-8")
    row = {"text_path": "Sources/Extracted/source.txt", "title": "Source"}
    assert "corpus evidence" in background._row_text(row, round1_dir=v2_run / "Process" / "round1")


def test_helper_layout_never_implicitly_creates_an_unmanaged_run(tmp_path: Path) -> None:
    layout = resolve_helper_layout(tmp_path, allow_unmanaged=True)
    assert layout.kind is LayoutKind.UNMANAGED
    assert list(tmp_path.iterdir()) == []
