from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_layout import RunLayout
from scripts.run_path_schema import PATH_SCHEMAS, PathBase, PathSchemaError


def _v2(tmp_path: Path) -> RunLayout:
    run = tmp_path / "topic"
    (run / "Process").mkdir(parents=True)
    (run / "Process" / "run.json").write_text(
        json.dumps({"layout_version": 2, "schema_version": 1, "slug": "topic"}),
        encoding="utf-8",
    )
    return RunLayout.open(run)


def _legacy(tmp_path: Path) -> RunLayout:
    run = tmp_path / "legacy"
    (run / "round1" / "sources").mkdir(parents=True)
    return RunLayout.open(run)


def test_registry_resolves_active_slice_claim_and_manifest_paths(tmp_path: Path) -> None:
    layout = _v2(tmp_path)
    cases = [
        ("Process/round1/slice_anchor.jsonl", "text_path", "Sources/Extracted/a.txt", layout.run_root / "Sources/Extracted/a.txt"),
        ("Process/round1/slice_anchor.jsonl", "raw_path", "Sources/Extracted/a.html", layout.run_root / "Sources/Extracted/a.html"),
        ("Sources/claims.jsonl", "file", "Sections/01.md", layout.run_root / "Sections/01.md"),
        ("Process/stages/export.json", "outputs", "RESEARCH-BIBLE_topic.md", layout.run_root / "RESEARCH-BIBLE_topic.md"),
    ]
    for document, field, value, expected in cases:
        assert PATH_SCHEMAS.resolve(layout, document, field, value) == expected


def test_registry_uses_historical_round1_base_for_legacy_slice_paths(tmp_path: Path) -> None:
    layout = _legacy(tmp_path)
    resolved = PATH_SCHEMAS.resolve(layout, "round1/slice_anchor.jsonl", "text_path", "sources/a.txt")
    assert resolved == layout.run_root / "round1" / "sources" / "a.txt"


def test_archive_paths_require_declared_root_and_cannot_escape(tmp_path: Path) -> None:
    layout = _v2(tmp_path)
    archive = layout.run_root / "Process" / "Inherited" / "parent"
    assert PATH_SCHEMAS.resolve(
        layout,
        "Process/Inherited/parent/snapshot.json",
        "path",
        "Sources/claims.jsonl",
        archive_root=archive,
    ) == archive / "Sources" / "claims.jsonl"
    with pytest.raises(PathSchemaError, match="archive root"):
        PATH_SCHEMAS.resolve(layout, "Process/Inherited/parent/snapshot.json", "path", "a.txt")
    with pytest.raises(ValueError):
        PATH_SCHEMAS.resolve(
            layout,
            "Process/Inherited/parent/snapshot.json",
            "path",
            "../escape",
            archive_root=archive,
        )


def test_opaque_provenance_labels_are_non_resolving(tmp_path: Path) -> None:
    layout = _v2(tmp_path)
    assert PATH_SCHEMAS.field_for("Process/lineage.json", "source_label").base is PathBase.NON_RESOLVING
    assert PATH_SCHEMAS.resolve(layout, "Process/lineage.json", "source_label", "Interview notes") is None


def test_unknown_path_like_fields_fail_closed(tmp_path: Path) -> None:
    layout = _v2(tmp_path)
    document = {"text_path": "Sources/Extracted/a.txt", "mystery_file": "../outside"}
    with pytest.raises(PathSchemaError, match="unregistered path-like field"):
        PATH_SCHEMAS.validate_document(layout, "Process/round1/slice_anchor.jsonl", document)


def test_rewrite_document_is_transactional_and_preserves_nonpaths(tmp_path: Path) -> None:
    layout = _legacy(tmp_path)
    document = {"title": "Source", "text_path": "sources/a.txt", "raw_path": None}
    rewritten = PATH_SCHEMAS.rewrite_document(
        layout,
        "round1/slice_anchor.jsonl",
        document,
        {layout.run_root / "round1" / "sources" / "a.txt": "Sources/Extracted/a.txt"},
    )
    assert rewritten == {"title": "Source", "text_path": "Sources/Extracted/a.txt", "raw_path": None}
    assert document["text_path"] == "sources/a.txt"


@pytest.mark.parametrize(
    ("document", "fields"),
    [
        ("Process/round1/slice_anchor.jsonl", {"text_path", "raw_path", "run_dir"}),
        ("Sources/claims.jsonl", {"file"}),
        ("Process/round1/fulltext_manifest.json", {"text_path", "raw_path"}),
        ("Process/round1/evidence_manifest.json", {"text_path", "raw_path"}),
        ("Process/round2_5/coverage.json", {"file"}),
        ("Process/round4/verification.json", {"file"}),
        ("Process/export_manifest.json", {"inputs", "outputs"}),
        ("Process/stages/export.json", {"inputs", "outputs"}),
        ("Process/lineage.json", {"source_label", "snapshot"}),
        ("Process/Inherited/parent/snapshot.json", {"path"}),
    ],
)
def test_all_required_path_bearing_document_families_are_registered(document: str, fields: set[str]) -> None:
    schema = PATH_SCHEMAS.schema_for(document)
    assert fields <= {field.field for field in schema.fields}
