from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_layout import (
    BibleAmbiguityError,
    FilesystemCapabilities,
    LayoutError,
    LayoutKind,
    RunLayout,
    capabilities_for_dry_run,
    portable_collision_key,
    probe_filesystem,
    reserve_unique_directory,
    resolve_project_root,
    safe_relpath,
    slugify_v1,
)


def _write_v2(run: Path, **overrides: object) -> None:
    (run / "Process").mkdir(parents=True)
    payload = {
        "layout_version": 2,
        "schema_version": 1,
        "run_id": "2b650079-9be0-418c-a5b6-c9e3e678b131",
        "slug": run.name,
        "bible": None,
    }
    payload.update(overrides)
    (run / "Process" / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def test_v2_layout_exposes_reader_source_and_process_homes(tmp_path: Path) -> None:
    run = tmp_path / "research" / "topic"
    _write_v2(run)

    layout = RunLayout.open(run)

    assert layout.kind is LayoutKind.V2
    assert layout.sections == run / "Sections"
    assert layout.sources == run / "Sources"
    assert layout.extracted_sources == run / "Sources" / "Extracted"
    assert layout.process == run / "Process"
    assert layout.round1 == run / "Process" / "round1"
    assert layout.round2_5 == run / "Process" / "round2_5"
    assert layout.scope == run / "Process" / "scope.json"
    assert layout.ledger == run / "Process" / "retrieval_ledger.json"
    assert layout.bibliography_md == run / "Sources" / "bibliography.md"
    assert layout.bibliography_bib == run / "Sources" / "bibliography.bib"
    assert layout.claims == run / "Sources" / "claims.jsonl"
    assert layout.metadata == run / "Process" / "run.json"
    assert layout.lineage == run / "Process" / "lineage.json"
    assert layout.stage_manifests == run / "Process" / "stages"


def test_present_malformed_or_unsupported_v2_metadata_never_falls_back(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt"
    (corrupt / "Process").mkdir(parents=True)
    (corrupt / "sections").mkdir()
    (corrupt / "Process" / "run.json").write_text("{", encoding="utf-8")
    with pytest.raises(LayoutError, match="corrupt"):
        RunLayout.open(corrupt)

    unsupported = tmp_path / "unsupported"
    _write_v2(unsupported, layout_version=3)
    (unsupported / "sections").mkdir()
    with pytest.raises(LayoutError, match="unsupported"):
        RunLayout.open(unsupported)


def test_v2_with_any_legacy_artifact_home_is_mixed(tmp_path: Path) -> None:
    for marker in ("sections", "chapters", "export", "round1", "scope.json"):
        run = tmp_path / marker.replace(".", "-")
        _write_v2(run)
        target = run / marker
        target.write_text("{}", encoding="utf-8") if target.suffix else target.mkdir()
        with pytest.raises(LayoutError, match="mixed"):
            RunLayout.open(run)


def test_legacy_layout_maps_historical_homes_without_moving_them(tmp_path: Path) -> None:
    run = tmp_path / "legacy"
    (run / "chapters").mkdir(parents=True)
    (run / "round1").mkdir()
    (run / "scope.json").write_text("{}", encoding="utf-8")

    layout = RunLayout.open(run)

    assert layout.kind is LayoutKind.LEGACY
    assert layout.sections == run / "chapters"
    assert layout.sources == run / "round1" / "sources"
    assert layout.process == run
    assert layout.scope == run / "scope.json"


def test_empty_directory_requires_explicit_unmanaged_mode(tmp_path: Path) -> None:
    with pytest.raises(LayoutError, match="invalid"):
        RunLayout.open(tmp_path)
    assert RunLayout.open(tmp_path, allow_unmanaged=True).kind is LayoutKind.UNMANAGED


def test_project_and_direct_library_resolution_are_distinct(tmp_path: Path) -> None:
    assert resolve_project_root(project_dir=tmp_path).library == tmp_path.resolve() / "Deeper_Research"
    # A project dir already named with a recognised library name is used in place (incl. legacy "research").
    research = tmp_path / "research"
    assert resolve_project_root(project_dir=research).library == research.resolve()
    custom = tmp_path / "custom"
    assert resolve_project_root(library_dir=custom).library == custom.resolve()
    assert resolve_project_root(output_root=custom).library == custom.resolve()
    assert resolve_project_root(launch_dir=tmp_path).project == tmp_path.resolve()
    with pytest.raises(ValueError, match="conflicting"):
        resolve_project_root(project_dir=tmp_path, library_dir=custom)


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "../x", "/x", "C:\\x", "a\\b", "a/../b", "a//b", "a\x00b", "CON/x"],
)
def test_safe_relpath_rejects_escape_non_posix_and_special_forms(value: str) -> None:
    with pytest.raises(ValueError):
        safe_relpath(value)


def test_safe_relpath_returns_a_pure_posix_path() -> None:
    result = safe_relpath("Sources/Extracted/source.txt")
    assert result.as_posix() == "Sources/Extracted/source.txt"


def test_slug_v1_is_fixed_portable_deterministic_and_bounded() -> None:
    assert slugify_v1("Crème / CON") == "creme-con"
    assert slugify_v1("CON") == "con-run"
    assert slugify_v1("COM1.txt") == "com1-txt"
    assert slugify_v1("東京") == "research-run"
    assert slugify_v1("é" * 400) == slugify_v1("e\u0301" * 400)
    assert len(slugify_v1("é" * 400).encode("utf-8")) <= 120
    assert len(slugify_v1("x" * 400, reserved_suffix="-20260828T010203123456Z-99").encode()) <= 120


def test_collision_key_is_case_unicode_and_trailing_mark_insensitive() -> None:
    assert portable_collision_key("Crème. ") == portable_collision_key("CRE\u0300ME")


def test_dry_run_capabilities_perform_no_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    for name in ("mkdir", "touch", "write_text", "write_bytes", "unlink", "rename", "replace"):
        original = getattr(Path, name)

        def blocked(self: Path, *args: object, _name: str = name, **kwargs: object):
            calls.append(_name)
            raise AssertionError(f"dry-run attempted {_name}")

        monkeypatch.setattr(Path, name, blocked)
    caps = capabilities_for_dry_run(tmp_path)
    assert caps.write_probe_pending is True
    assert calls == []


def test_filesystem_probe_cleans_up_temporary_aliases(tmp_path: Path) -> None:
    caps = probe_filesystem(tmp_path)
    assert isinstance(caps, FilesystemCapabilities)
    assert caps.write_probe_pending is False
    assert list(tmp_path.iterdir()) == []


def test_atomic_reservation_rejects_portable_alias_and_retries(tmp_path: Path) -> None:
    (tmp_path / "Topic").mkdir()
    with pytest.raises(FileExistsError, match="collision"):
        reserve_unique_directory(tmp_path, "topic", capabilities=FilesystemCapabilities(True, True, False))

    first = reserve_unique_directory(tmp_path, "fresh")
    assert first.name == "fresh"
    second = reserve_unique_directory(tmp_path, "fresh", timestamp="20260828T010203123456Z")
    assert second.name == "fresh-20260828T010203123456Z"
    third = reserve_unique_directory(tmp_path, "fresh", timestamp="20260828T010203123456Z")
    assert third.name == "fresh-20260828T010203123456Z-2"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("RESEARCH-BIBLE_topic.md", "RESEARCH-BIBLE_topic.md"),
        ("Persona-Construction-Research-Bible.md", "Persona-Construction-Research-Bible.md"),
        ("western-philosophy-of-mind-BIBLE.md", "western-philosophy-of-mind-BIBLE.md"),
    ],
)
def test_bible_discovery_preserves_topic_qualified_names(tmp_path: Path, filename: str, expected: str) -> None:
    run = tmp_path / "topic"
    _write_v2(run)
    (run / filename).write_text("# Bible", encoding="utf-8")
    (run / Path(filename).with_suffix(".html").name).write_text("<h1>Bible</h1>", encoding="utf-8")
    selected = RunLayout.open(run).discover_bible()
    assert selected.markdown.as_posix() == expected
    assert selected.html.as_posix() == Path(expected).with_suffix(".html").name


def test_bible_metadata_is_authoritative_and_generic_name_is_normalized(tmp_path: Path) -> None:
    run = tmp_path / "topic"
    _write_v2(run, bible={"markdown": "Canonical.md", "html": "Canonical.html"})
    (run / "Canonical.md").write_text("# Canonical", encoding="utf-8")
    (run / "RESEARCH-BIBLE_other.md").write_text("# Other", encoding="utf-8")
    assert RunLayout.open(run).discover_bible().markdown.as_posix() == "Canonical.md"

    legacy = tmp_path / "legacy-topic"
    (legacy / "export").mkdir(parents=True)
    (legacy / "export" / "RESEARCH-BIBLE.md").write_text("# Generic", encoding="utf-8")
    selected = RunLayout.open(legacy).discover_bible()
    assert selected.markdown.as_posix() == "export/RESEARCH-BIBLE_legacy-topic.md"


def test_bible_discovery_rejects_ambiguous_candidates(tmp_path: Path) -> None:
    run = tmp_path / "topic"
    _write_v2(run)
    (run / "RESEARCH-BIBLE_one.md").write_text("# One", encoding="utf-8")
    (run / "RESEARCH-BIBLE_two.md").write_text("# Two", encoding="utf-8")
    with pytest.raises(BibleAmbiguityError):
        RunLayout.open(run).discover_bible()
