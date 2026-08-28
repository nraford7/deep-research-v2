from pathlib import Path
import json
import subprocess
import sys

import pytest

from scripts import export


REPO_ROOT = Path(__file__).resolve().parents[1]


def _artifacts(tmp_path: Path):
    run_dir = tmp_path / "my-run"
    sections = run_dir / "sections"
    sections.mkdir(parents=True)
    (sections / "01-findings.md").write_text(
        "# 1. Findings\n\nA supported sentence [Smith, 2024].\n",
        encoding="utf-8",
    )
    bibliography = sections / "bibliography.md"
    bibliography.write_text(
        "# Bibliography\n\n- Smith, A. (2024). A sufficiently long source title. https://example.com/source\n",
        encoding="utf-8",
    )
    output = run_dir / "export"
    return run_dir, sections, bibliography, output


def _run_export(sections: Path, bibliography: Path, output: Path, *extra):
    return export.main(
        [
            "--sections",
            str(sections),
            "--bibliography",
            str(bibliography),
            "--output-dir",
            str(output),
            *extra,
        ]
    )


def test_html_is_automatic_and_section_assembly_does_not_create_markdown(tmp_path, monkeypatch, capsys):
    run_dir, sections, bibliography, output = _artifacts(tmp_path)
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    rc = _run_export(sections, bibliography, output)

    assert rc == 0
    html = output / f"RESEARCH-BIBLE_{run_dir.name}.html"
    assert html.exists()
    assert not (output / f"RESEARCH-BIBLE_{run_dir.name}.md").exists()
    assert "(built-in; jimemo unavailable)" in capsys.readouterr().out


def test_explicit_bible_controls_html_basename(tmp_path, monkeypatch):
    _, sections, bibliography, output = _artifacts(tmp_path)
    bible = tmp_path / "chosen-name.md"
    bible.write_text("# Chosen title\n\n**Purpose.** Context.\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    rc = _run_export(sections, bibliography, output, "--bible", str(bible))

    assert rc == 0
    assert (output / "chosen-name.html").exists()


def test_unambiguous_bible_in_output_directory_is_discovered(tmp_path, monkeypatch):
    _, sections, bibliography, output = _artifacts(tmp_path)
    output.mkdir()
    (output / "RESEARCH-BIBLE_discovered.md").write_text("# Discovered\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    rc = _run_export(sections, bibliography, output)

    assert rc == 0
    assert (output / "RESEARCH-BIBLE_discovered.html").exists()


def test_ambiguous_automatic_bible_candidates_are_an_export_error(tmp_path, monkeypatch):
    _, sections, bibliography, output = _artifacts(tmp_path)
    output.mkdir()
    (output / "RESEARCH-BIBLE_one.md").write_text("# One\n", encoding="utf-8")
    (output / "RESEARCH-BIBLE_two.md").write_text("# Two\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    with pytest.raises(SystemExit, match="multiple Research Bible"):
        _run_export(sections, bibliography, output)


def test_no_html_preserves_existing_machine_exports(tmp_path):
    _, sections, bibliography, output = _artifacts(tmp_path)

    rc = _run_export(sections, bibliography, output, "--no-html")

    assert rc == 0
    assert not list(output.glob("*.html"))
    assert (output / "bibliography.bib").read_text(encoding="utf-8").startswith("@misc{Smith2024")
    claims = (output / "claims.jsonl").read_text(encoding="utf-8")
    assert '"author": "Smith"' in claims


def test_html_addition_does_not_change_machine_artifacts(tmp_path, monkeypatch):
    _, sections, bibliography, without_html = _artifacts(tmp_path / "old")
    _, sections_2, bibliography_2, with_html = _artifacts(tmp_path / "new")
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    _run_export(sections, bibliography, without_html, "--no-html")
    _run_export(sections_2, bibliography_2, with_html)

    assert (without_html / "bibliography.bib").read_bytes() == (with_html / "bibliography.bib").read_bytes()
    old_claims = (without_html / "claims.jsonl").read_text(encoding="utf-8")
    new_claims = (with_html / "claims.jsonl").read_text(encoding="utf-8")
    assert old_claims.replace(str(sections), "SECTIONS") == new_claims.replace(str(sections_2), "SECTIONS")


def test_documented_direct_script_invocation_resolves_html_module(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "export.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--no-html" in completed.stdout


def test_v2_run_export_uses_reader_and_sources_homes(tmp_path, monkeypatch):
    run = tmp_path / "research" / "topic"
    (run / "Process").mkdir(parents=True)
    (run / "Sections").mkdir()
    (run / "Sources").mkdir()
    (run / "Process" / "run.json").write_text(json.dumps({
        "layout_version": 2, "schema_version": 1, "slug": "topic",
    }))
    (run / "Sections" / "01-findings.md").write_text(
        "# Findings\n\nSupported [Smith, 2024].\n", encoding="utf-8")
    (run / "Sources" / "bibliography.md").write_text(
        "# Bibliography\n\n- Smith, A. (2024). A sufficiently long source title. https://example.com/source\n",
        encoding="utf-8",
    )
    bible = run / "RESEARCH-BIBLE_topic.md"
    bible.write_text("# Topic Research Bible\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_bible_html.shutil.which", lambda _: None)

    assert export.main(["--run-dir", str(run)]) == 0

    assert (run / "RESEARCH-BIBLE_topic.html").is_file()
    assert (run / "Sources" / "bibliography.bib").is_file()
    claim = json.loads((run / "Sources" / "claims.jsonl").read_text().splitlines()[0])
    assert claim["file"] == "Sections/01-findings.md"
