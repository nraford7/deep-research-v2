import json
import os
import re
from pathlib import Path

import pytest

from scripts.background import BACKGROUND_LABEL, render_background
from scripts.research_bible_html import (
    ContentBlock,
    ResearchDocument,
    ResearchSection,
    build_document,
    export_html,
    render_builtin_html,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _document() -> ResearchDocument:
    return ResearchDocument(
        title="A <Research> Bible",
        subtitle="Safe & self-contained",
        compiled="28 August 2026",
        introduction=(
            ContentBlock(
                "markdown",
                "Purpose with <script>alert(1)</script>, "
                "[an unsafe link](javascript:alert(2)), and "
                "![Remote portrait](https://tracker.example/image.png).",
            ),
        ),
        provenance=(("Sources in corpus", "14"),),
        evidence_legend=(("[Author, Year]", "An attributed claim."),),
        sections=(
            ResearchSection(
                title="Shared heading",
                anchor="section-shared-heading",
                blocks=(
                    ContentBlock("markdown", "Opening.\n\n### Nested finding\n\nBody."),
                    ContentBlock("background", "Orienting **context** only."),
                ),
            ),
            ResearchSection(
                title="Shared heading",
                anchor="section-shared-heading-2",
                blocks=(ContentBlock("markdown", "Second position."),),
            ),
        ),
        unresolved=(ContentBlock("markdown", "- https://bad.example — timeout"),),
        bibliography=(ContentBlock("markdown", "- Author (2026). *A source*."),),
        colophon="Produced by deeper-research.",
    )


def test_build_document_normalizes_sections_without_mutating_sources(tmp_path):
    sections = tmp_path / "topic-run" / "sections"
    first = _write(
        sections / "01-first.md",
        "# 1. First position\n\nLead.\n\n"
        "<!-- editorial:background -->\nContext only.\n<!-- /editorial -->\n\n"
        "## Nested claim\n\nEvidence.\n",
    )
    first_before = first.read_bytes()
    _write(sections / "10-tenth.md", "# 10 — Tenth position\n\nTenth.\n")
    _write(sections / "2-second.md", "# 2. Second position\n\nSecond.\n")
    bibliography = _write(
        sections / "bibliography.md",
        "# Master Bibliography\n\n- Author (2026). A source. https://example.com\n",
    )
    _write(sections / "dedup-decisions.md", "# Audit sidecar\n\nNever publish me.\n")
    _write(sections / "bibliography-pre-dedup.md", "# Old bibliography\n\nNever publish me.\n")
    bible = _write(
        tmp_path / "topic-run" / "export" / "RESEARCH-BIBLE_topic.md",
        "# Topic title\n"
        "## A careful subtitle\n\n"
        "*Compiled 2026-08-28. Retrieval corpus: 14 sources across 3 slices; "
        "evidence gate passed; draft passed an independent (OpenAI-family) "
        "refute-mode adversary.*\n\n"
        "**Purpose.** A concise orientation.\n\n"
        "## Contents\n\n1. First\n\n"
        "## ⚠ Unresolved links\n\n- https://bad.example — timeout\n\n"
        "## Verification notes\n\nThis must not enter the warning.\n",
    )

    document = build_document(sections, bibliography, bible)

    assert document.title == "Topic title"
    assert document.subtitle == "A careful subtitle"
    assert document.compiled == "2026-08-28"
    assert document.provenance == (
        ("Sources in corpus", "14"),
        ("Retrieval slices", "3"),
        ("Evidence gate", "Passed"),
        ("Refute adversary", "OpenAI family"),
    )
    assert "A concise orientation" in document.introduction[0].markdown
    assert [section.title for section in document.sections] == [
        "First position",
        "Second position",
        "Tenth position",
    ]
    assert [block.kind for block in document.sections[0].blocks] == [
        "markdown",
        "background",
        "markdown",
    ]
    assert "### Nested claim" in document.sections[0].blocks[-1].markdown
    assert "Audit sidecar" not in repr(document)
    assert "Old bibliography" not in repr(document)
    assert "Master Bibliography" not in document.bibliography[0].markdown
    assert "https://bad.example" in document.unresolved[0].markdown
    assert "Verification notes" not in document.unresolved[0].markdown
    assert first.read_bytes() == first_before


def test_build_document_uses_honest_defaults_without_a_bible(tmp_path):
    sections = tmp_path / "my-topic" / "sections"
    bibliography = _write(sections / "bibliography.md", "# Bibliography\n\n- One.\n")
    _write(sections / "01-result.md", "# 01 Result\n\nFinding.\n")

    document = build_document(sections, bibliography)

    assert document.title == "My Topic"
    assert document.subtitle == ""
    assert document.compiled == ""
    assert document.provenance == ()
    assert document.sections[0].title == "Result"


def test_provenance_is_parsed_only_from_the_explicit_compiled_line(tmp_path):
    sections = tmp_path / "topic" / "sections"
    bibliography = _write(sections / "bibliography.md", "# Bibliography\n\n- One.\n")
    _write(sections / "01-result.md", "# Result\n\nFinding.\n")
    bible = _write(
        tmp_path / "topic" / "export" / "RESEARCH-BIBLE_topic.md",
        "# Topic\n\n"
        "**Purpose.** A body paragraph compares 77 sources across 4 slices, "
        "describes an evidence gate failed state, and mentions an independent "
        "(Other-family) refute-mode adversary as a hypothetical.\n",
    )

    document = build_document(sections, bibliography, bible)

    assert document.compiled == ""
    assert document.provenance == ()


def test_canonical_background_renderer_is_normalized_without_duplicate_chrome(tmp_path):
    sections = tmp_path / "topic" / "sections"
    bibliography = _write(sections / "bibliography.md", "# Bibliography\n\n- One.\n")
    _write(
        sections / "01-result.md",
        "# 1. Result\n\n" + render_background("Canonical context.\n\nSecond paragraph."),
    )

    document = build_document(sections, bibliography)

    background = document.sections[0].blocks[0]
    assert background.kind == "background"
    assert background.markdown == "Canonical context.\n\nSecond paragraph."
    assert BACKGROUND_LABEL not in background.markdown
    assert not any(line.startswith(">") for line in background.markdown.splitlines())


def test_heading_demotion_respects_longer_markdown_code_fences(tmp_path):
    sections = tmp_path / "topic" / "sections"
    bibliography = _write(sections / "bibliography.md", "# Bibliography\n\n- One.\n")
    _write(
        sections / "01-result.md",
        "# Result\n\n"
        "````markdown\n"
        "## Code heading\n"
        "```\n"
        "## Still code\n"
        "````\n\n"
        "## Real subsection\n",
    )

    document = build_document(sections, bibliography)
    markdown = document.sections[0].blocks[0].markdown

    assert "## Code heading" in markdown
    assert "## Still code" in markdown
    assert "### Real subsection" in markdown


def test_builtin_renderer_is_semantic_safe_and_self_contained(tmp_path):
    output = tmp_path / "research.html"

    render_builtin_html(_document(), output)

    html = output.read_text(encoding="utf-8")
    lowered = html.lower()
    assert html.count("<h1") == 1
    assert 'id="section-shared-heading"' in html
    assert 'id="section-shared-heading-2"' in html
    assert '<ol class="toc-list">' in html
    assert '<dl class="evidence-legend">' in html
    assert '<aside class="background-aside"' in html
    assert '<section class="unresolved" id="unresolved"' in html
    assert '<section class="bibliography" id="bibliography"' in html
    assert "<style>" in html
    assert "<script" not in lowered
    assert "javascript:" not in lowered
    assert "https://tracker.example/image.png" not in html
    assert "Remote portrait" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "@import" not in lowered
    assert "<link" not in lowered
    assert "Nested finding" in html


def test_builtin_renderer_wraps_unbroken_research_links(tmp_path):
    output = tmp_path / "research.html"

    render_builtin_html(_document(), output)

    html = output.read_text(encoding="utf-8")
    assert ".bibliography a" in html
    assert "overflow-wrap: anywhere" in html


def test_section_ids_are_namespaced_and_unique_across_generated_heading_ids(tmp_path):
    sections = tmp_path / "topic" / "sections"
    bibliography = _write(sections / "bibliography.md", "# Bibliography\n\n- One.\n")
    for number, title in enumerate(("Main", "Bibliography", "Foo", "Foo Title"), start=1):
        _write(sections / f"{number:02d}.md", f"# {title}\n\nBody.\n")

    document = build_document(sections, bibliography)
    output = tmp_path / "research.html"
    render_builtin_html(document, output)
    rendered = output.read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', rendered)

    assert [section.anchor for section in document.sections] == [
        "section-main",
        "section-bibliography",
        "section-foo",
        "section-foo-title-2",
    ]
    assert len(ids) == len(set(ids))


def _fake_jimemo(tmp_path: Path, mode: str = "success") -> Path:
    executable = tmp_path / "jimemo"
    info_action = {
        "info-list": "    print('[]')\n",
        "info-null-slots": "    print(json.dumps({'name': 'research-bible', 'slots': None}))\n",
        "info-bytes": "    sys.stdout.buffer.write(b'\\xff')\n",
    }.get(mode, "    print(json.dumps({'name': 'research-bible', 'slots': {'sections': {}}}))\n")
    render_action = (
        "    output.write_text(json.dumps({'args': args, 'payload': json.loads(source.read_text())}))\n"
        "    raise SystemExit(0)\n"
        if mode == "success"
        else "    sys.stderr.buffer.write(b'\\xff')\n    raise SystemExit(7)\n"
        if mode == "render-bytes"
        else "    print('template render failed', file=sys.stderr)\n    raise SystemExit(7)\n"
    )
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['info', 'research-bible']:\n"
        + info_action
        +
        "    raise SystemExit(0)\n"
        "if args[:2] == ['render', 'research-bible']:\n"
        "    source = pathlib.Path(args[2])\n"
        "    output = pathlib.Path(args[args.index('-o') + 1])\n"
        + render_action,
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def test_compatible_jimemo_is_preferred_and_receives_normalized_json(tmp_path):
    output = tmp_path / "rendered.html"
    executable = _fake_jimemo(tmp_path)

    result = export_html(_document(), output, jimemo_executable=str(executable))

    assert result.renderer == "jimemo"
    assert result.fallback_reason is None
    record = json.loads(output.read_text(encoding="utf-8"))
    args = record["args"]
    received = record["payload"]
    assert args[:2] == ["render", "research-bible"]
    assert args[-2] == "-o"
    assert received["title"] == "A <Research> Bible"
    assert received["sections"][0]["heading"] == "Shared heading"
    assert "> **Background.** Orienting **context** only." in received["sections"][0]["body"]
    assert received["legend"][0]["tag"] == "[Author, Year]"
    assert not Path(args[2]).exists()


@pytest.mark.parametrize("mode", ["missing", "failure"])
def test_missing_or_failing_jimemo_uses_builtin_renderer(tmp_path, mode):
    output = tmp_path / "rendered.html"
    executable = tmp_path / "not-installed" if mode == "missing" else _fake_jimemo(tmp_path, "failure")

    result = export_html(_document(), output, jimemo_executable=str(executable))

    assert result.renderer == "built-in"
    assert result.fallback_reason
    assert "Research Bible" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["info-list", "info-null-slots", "info-bytes", "render-bytes"])
def test_malformed_jimemo_metadata_or_output_still_falls_back(tmp_path, mode):
    output = tmp_path / "rendered.html"

    result = export_html(_document(), output, jimemo_executable=str(_fake_jimemo(tmp_path, mode)))

    assert result.renderer == "built-in"
    assert result.fallback_reason
    assert output.is_file()
