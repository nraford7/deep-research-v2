import json

from scripts.search import select_documents


def _v2(root, slug, *, complete):
    run = root / slug
    (run / "Process").mkdir(parents=True)
    (run / "Sections").mkdir()
    (run / "Sources").mkdir()
    (run / "Process" / "run.json").write_text(json.dumps({
        "layout_version": 2,
        "schema_version": 1,
        "slug": slug,
        "run_id": f"id-{slug}",
        "status": "complete" if complete else "incomplete",
        "sealed": complete,
    }))
    (run / "Sections" / "01-a.md").write_text("# A\n")
    (run / "Sections" / "02-b.md").write_text("# B\n")
    (run / "Sources" / "bibliography.md").write_text("# Bibliography\n")
    (run / f"RESEARCH-BIBLE_{slug}.md").write_text(f"# {slug}\n")
    return run


def test_index_selects_complete_bible_or_incomplete_sections_once(tmp_path):
    library = tmp_path / "research"
    complete = _v2(library, "complete", complete=True)
    partial = _v2(library, "partial", complete=False)

    docs = select_documents(library)

    assert [d.content_path for d in docs["id-complete"]] == [complete / "RESEARCH-BIBLE_complete.md"]
    assert [d.content_path for d in docs["id-partial"]] == [
        partial / "Sections" / "01-a.md",
        partial / "Sections" / "02-b.md",
    ]
    assert all(d.display_path.startswith(("complete/", "partial/")) for values in docs.values() for d in values)


def test_logical_ids_are_stable_for_same_run_identity_and_document(tmp_path):
    first_library = tmp_path / "one"
    second_library = tmp_path / "two"
    _v2(first_library, "topic", complete=True)
    _v2(second_library, "topic", complete=True)

    first = select_documents(first_library)["id-topic"][0]
    second = select_documents(second_library)["id-topic"][0]

    assert first.logical_id == second.logical_id
