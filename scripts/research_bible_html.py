#!/usr/bin/env python3
"""Normalize deeper-research artifacts and render a self-contained HTML Bible."""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from scripts.background import BACKGROUND_LABEL


BACKGROUND_OPEN = "<!-- editorial:background -->"
BACKGROUND_CLOSE = "<!-- /editorial -->"
ATX_HEADING_RE = re.compile(r"^(\s{0,3})(#{1,6})\s+(.+?)\s*#*\s*$")
UNRESOLVED_RE = re.compile(r"^##\s+⚠\s*Unresolved links\s*$", re.IGNORECASE)
BIBLIOGRAPHY_FILE_RE = re.compile(r"^bibliography(?:[-_.].*)?\.md$", re.IGNORECASE)
ORDINAL_RE = re.compile(
    r"^\s*(?:section\s+)?\d+(?:\.\d+)*(?:(?:\s*[.)])|(?:\s*[—–-]\s*)|\s+)",
    re.IGNORECASE,
)


EVIDENCE_LEGEND = (
    ("[Author, Year]", "An attributed claim, resolved against the bibliography."),
    ("[disputed: …]", "Figures the corpus reports differently; never silently averaged."),
    ("[confidence: …]", "The strength and transfer-distance of a claim."),
    ("FLAG", "A number resting on a weak or single source."),
)


@dataclass(frozen=True)
class ContentBlock:
    kind: str
    markdown: str


@dataclass(frozen=True)
class ResearchSection:
    title: str
    anchor: str
    blocks: Tuple[ContentBlock, ...]


@dataclass(frozen=True)
class ResearchDocument:
    title: str
    subtitle: str
    compiled: str
    introduction: Tuple[ContentBlock, ...]
    provenance: Tuple[Tuple[str, str], ...]
    evidence_legend: Tuple[Tuple[str, str], ...]
    sections: Tuple[ResearchSection, ...]
    unresolved: Tuple[ContentBlock, ...]
    bibliography: Tuple[ContentBlock, ...]
    colophon: str


@dataclass(frozen=True)
class HtmlExportResult:
    path: Path
    renderer: str
    fallback_reason: Optional[str]


def _natural_key(path: Path):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", path.name)
    )


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value or "research"


def _strip_ordinal(title: str) -> str:
    stripped = ORDINAL_RE.sub("", title, count=1).strip()
    return stripped or title.strip()


def _drop_title_line(markdown: str) -> Tuple[str, str]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        match = ATX_HEADING_RE.match(line)
        if match:
            title = _strip_ordinal(match.group(3))
            del lines[index]
            return title, "\n".join(lines).strip()
        if line.strip():
            break
    return "", markdown.strip()


def _demote_headings(markdown: str) -> str:
    lines = []
    fence = None
    for line in markdown.splitlines():
        if fence is None:
            fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if fence_match:
                marker = fence_match.group(1)
                fence = (marker[0], len(marker))
                lines.append(line)
                continue
        else:
            close_match = re.match(r"^\s{0,3}(`{3,}|~{3,})\s*$", line)
            if close_match:
                marker = close_match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
            lines.append(line)
            continue
        match = ATX_HEADING_RE.match(line)
        if match:
            level = min(len(match.group(2)) + 1, 6)
            line = f"{match.group(1)}{'#' * level} {match.group(3)}"
        lines.append(line)
    return "\n".join(lines).strip()


def _blocks_from_markdown(markdown: str) -> Tuple[ContentBlock, ...]:
    lines = markdown.splitlines()
    blocks: List[ContentBlock] = []
    normal: List[str] = []
    background: List[str] = []
    in_background = False

    def flush(kind: str, values: List[str]):
        if kind == "background" and any(line.strip() == BACKGROUND_LABEL for line in values):
            normalized = []
            for line in values:
                if line.strip() == BACKGROUND_LABEL:
                    continue
                normalized.append(re.sub(r"^\s*>\s?", "", line) if line.strip().startswith(">") else line)
            values[:] = normalized
        text = "\n".join(values).strip()
        if text:
            blocks.append(ContentBlock(kind, text))
        values.clear()

    for line in lines:
        marker = line.strip()
        if marker == BACKGROUND_OPEN and not in_background:
            flush("markdown", normal)
            in_background = True
            continue
        if marker == BACKGROUND_CLOSE and in_background:
            flush("background", background)
            in_background = False
            continue
        (background if in_background else normal).append(line)

    if in_background:
        # Preserve malformed input as ordinary escaped Markdown. The pipeline's
        # background lint should prevent this path in a completed run.
        normal.extend([BACKGROUND_OPEN, *background])
        background.clear()
    flush("markdown", normal)
    return tuple(blocks)


def _extract_unresolved(markdown: str) -> str:
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if UNRESOLVED_RE.match(line.strip()):
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"^##\s+", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _bible_metadata(markdown: str) -> Dict[str, object]:
    lines = markdown.splitlines()
    title = ""
    subtitle = ""
    title_index = -1
    subtitle_index = -1
    for index, line in enumerate(lines):
        match = ATX_HEADING_RE.match(line)
        if match and len(match.group(2)) == 1:
            title = match.group(3).strip()
            title_index = index
            break
    if title_index >= 0:
        for index in range(title_index + 1, len(lines)):
            if not lines[index].strip():
                continue
            match = ATX_HEADING_RE.match(lines[index])
            if match and len(match.group(2)) == 2 and not UNRESOLVED_RE.match(lines[index].strip()):
                subtitle = match.group(3).strip()
                subtitle_index = index
            break

    compiled = ""
    compiled_index = -1
    provenance_line = ""
    for index, line in enumerate(lines):
        if re.match(r"^\s*[*_]{0,3}Compiled\b", line, re.IGNORECASE):
            provenance_line = line
            compiled_index = index
            break
    compiled_match = re.search(r"\bCompiled\s+([^.;*]+)", provenance_line, re.IGNORECASE)
    if compiled_match:
        compiled = compiled_match.group(1).strip()

    provenance: List[Tuple[str, str]] = []
    corpus_match = re.search(
        r"([\d,]+)\s+sources?\s+across\s+([\d,]+)\s+slices?", provenance_line, re.IGNORECASE
    )
    if corpus_match:
        provenance.extend(
            [
                ("Sources in corpus", corpus_match.group(1)),
                ("Retrieval slices", corpus_match.group(2)),
            ]
        )
    gate_match = re.search(
        r"evidence gate\s+(passed|partial|failed)", provenance_line, re.IGNORECASE
    )
    if gate_match:
        provenance.append(("Evidence gate", gate_match.group(1).title()))
    adversary_match = re.search(
        r"independent\s+\(([^)]+)\)\s+refute-mode adversary",
        provenance_line,
        re.IGNORECASE,
    )
    if adversary_match:
        value = re.sub(r"[-_]+", " ", adversary_match.group(1)).strip()
        provenance.append(("Refute adversary", value))

    intro_start = max(title_index, subtitle_index, compiled_index) + 1
    intro_end = len(lines)
    for index in range(intro_start, len(lines)):
        marker = lines[index].strip()
        if marker == "---" or re.match(r"^##\s+Contents\s*$", marker, re.IGNORECASE):
            intro_end = index
            break
    intro = "\n".join(lines[intro_start:intro_end]).strip()
    return {
        "title": title,
        "subtitle": subtitle,
        "compiled": compiled,
        "introduction": intro,
        "provenance": tuple(provenance),
    }


def build_document(
    sections_dir: Path,
    bibliography_path: Path,
    bible_path: Optional[Path] = None,
) -> ResearchDocument:
    """Build one renderer-neutral document without mutating its source files."""
    sections_dir = Path(sections_dir)
    bibliography_path = Path(bibliography_path)
    bible_path = Path(bible_path) if bible_path is not None else None
    bible_text = bible_path.read_text(encoding="utf-8", errors="replace") if bible_path else ""
    metadata = _bible_metadata(bible_text) if bible_text else {}

    used_ids = {
        "main",
        "legend-title",
        "contents-title",
        "unresolved",
        "unresolved-title",
        "bibliography",
        "bibliography-title",
    }
    sections: List[ResearchSection] = []
    candidates = []
    for path in sections_dir.glob("*.md"):
        name = path.name.casefold()
        if name == "dedup-decisions.md" or BIBLIOGRAPHY_FILE_RE.match(name):
            continue
        # Skip prompt/meta artifacts (e.g. _SECTION_BRIEF.md, the shared per-section
        # briefing) — underscore-prefixed files are never report content. Without this
        # the "shared brief for all section subagents" block leaks into the export.
        if name.startswith("_") or "shared-brief" in name or "shared brief" in name:
            continue
        candidates.append(path)
    for path in sorted(candidates, key=_natural_key):
        source = path.read_text(encoding="utf-8", errors="replace")
        title, body = _drop_title_line(source)
        if not title:
            title = _strip_ordinal(path.stem.replace("_", " ").replace("-", " ").title())
        if "shared brief for all section subagents" in title.casefold():
            continue
        base_anchor = f"section-{_slugify(title)}"
        anchor = base_anchor
        suffix = 2
        while anchor in used_ids or f"{anchor}-title" in used_ids:
            anchor = f"{base_anchor}-{suffix}"
            suffix += 1
        used_ids.update((anchor, f"{anchor}-title"))
        body = _demote_headings(body)
        sections.append(ResearchSection(title, anchor, _blocks_from_markdown(body)))

    bibliography_text = bibliography_path.read_text(encoding="utf-8", errors="replace")
    _, bibliography_body = _drop_title_line(bibliography_text)
    bibliography = _blocks_from_markdown(_demote_headings(bibliography_body))
    unresolved = _blocks_from_markdown(_extract_unresolved(bible_text)) if bible_text else ()
    introduction_text = str(metadata.get("introduction", ""))
    introduction = _blocks_from_markdown(introduction_text) if introduction_text else ()
    default_title = sections_dir.parent.name.replace("_", " ").replace("-", " ").title()

    return ResearchDocument(
        title=str(metadata.get("title") or default_title or "Research Report"),
        subtitle=str(metadata.get("subtitle") or ""),
        compiled=str(metadata.get("compiled") or ""),
        introduction=introduction,
        provenance=tuple(metadata.get("provenance", ())),
        evidence_legend=EVIDENCE_LEGEND,
        sections=tuple(sections),
        unresolved=unresolved,
        bibliography=bibliography,
        colophon="Produced by the deeper-research pipeline.",
    )


def resolve_bible_path(output_dir: Path, explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ValueError(f"Research Bible not found: {path}")
        return path
    candidates = sorted(Path(output_dir).glob("RESEARCH-BIBLE*.md"))
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ValueError(f"multiple Research Bible files found in {output_dir}: {names}")
    return candidates[0] if candidates else None


def assembled_html_name(sections_dir: Path) -> str:
    return f"RESEARCH-BIBLE_{_slugify(Path(sections_dir).parent.name)}.html"


def _markdown_renderer():
    try:
        import mistune
    except ImportError as exc:
        raise RuntimeError("built-in HTML export requires Mistune 3; install requirements.txt") from exc

    class SafeRenderer(mistune.HTMLRenderer):
        def link(self, text, url, title=None):
            parsed = urlsplit(url)
            if parsed.scheme.casefold() not in ("", "http", "https", "mailto"):
                return text
            return super().link(text, url, title)

        def image(self, text, url, title=None):
            label = re.sub(r"<[^>]+>", "", text or "Image")
            return f'<span class="image-alt">[Image: {html.escape(label)}]</span>'

    renderer = SafeRenderer(escape=True)
    return mistune.create_markdown(renderer=renderer, plugins=["table", "strikethrough", "url"])


def _render_blocks(blocks: Sequence[ContentBlock], markdown) -> str:
    rendered = []
    for block in blocks:
        content = markdown(block.markdown)
        if block.kind == "background":
            rendered.append(
                '<aside class="background-aside" aria-label="Background context">'
                '<p class="aside-label">Background</p>' + content + "</aside>"
            )
        else:
            rendered.append(content)
    return "\n".join(rendered)


def _css_text() -> str:
    css_path = Path(__file__).resolve().parents[1] / "assets" / "research-bible.css"
    return css_path.read_text(encoding="utf-8")


def render_builtin_html(document: ResearchDocument, output_path: Path) -> None:
    markdown = _markdown_renderer()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    esc = html.escape

    provenance = ""
    if document.provenance:
        items = "".join(
            f'<div class="stat"><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>'
            for label, value in document.provenance
        )
        provenance = f'<dl class="provenance" aria-label="Research provenance">{items}</dl>'

    legend = ""
    if document.evidence_legend:
        items = "".join(
            f"<div><dt><code>{esc(tag)}</code></dt><dd>{esc(meaning)}</dd></div>"
            for tag, meaning in document.evidence_legend
        )
        legend = (
            '<section class="legend" aria-labelledby="legend-title">'
            '<p class="eyebrow" id="legend-title">Evidence key</p>'
            f'<dl class="evidence-legend">{items}</dl></section>'
        )

    toc_items = [
        f'<li><a href="#{esc(section.anchor)}">{esc(section.title)}</a></li>'
        for section in document.sections
    ]
    if document.unresolved:
        toc_items.append('<li><a href="#unresolved">Unresolved links</a></li>')
    if document.bibliography:
        toc_items.append('<li><a href="#bibliography">Bibliography</a></li>')
    toc = (
        '<nav class="contents" aria-labelledby="contents-title">'
        '<p class="eyebrow" id="contents-title">Contents</p>'
        f'<ol class="toc-list">{"".join(toc_items)}</ol></nav>'
    )

    section_html = []
    for index, section in enumerate(document.sections, start=1):
        section_html.append(
            f'<section class="research-section" id="{esc(section.anchor)}" '
            f'aria-labelledby="{esc(section.anchor)}-title">'
            f'<div class="section-number" aria-hidden="true">{index:02d}</div>'
            f'<div class="section-copy"><h2 id="{esc(section.anchor)}-title">'
            f'{esc(section.title)}</h2>{_render_blocks(section.blocks, markdown)}</div></section>'
        )

    unresolved = ""
    if document.unresolved:
        unresolved = (
            '<section class="unresolved" id="unresolved" aria-labelledby="unresolved-title">'
            '<div class="warning-mark" aria-hidden="true">!</div><div>'
            '<p class="eyebrow">Verification notice</p>'
            '<h2 id="unresolved-title">Unresolved links</h2>'
            f'{_render_blocks(document.unresolved, markdown)}</div></section>'
        )

    bibliography = ""
    if document.bibliography:
        bibliography = (
            '<section class="bibliography" id="bibliography" aria-labelledby="bibliography-title">'
            '<p class="eyebrow">Source record</p><h2 id="bibliography-title">Bibliography</h2>'
            f'{_render_blocks(document.bibliography, markdown)}</section>'
        )

    subtitle = f'<p class="subtitle">{esc(document.subtitle)}</p>' if document.subtitle else ""
    date = f'<p class="compiled">Compiled {esc(document.compiled)}</p>' if document.compiled else ""
    introduction = _render_blocks(document.introduction, markdown)
    full_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(document.title)} — Research Report</title>
<style>{_css_text()}</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to research</a>
<header class="masthead">
  <div class="masthead-rule"><span>Deeper research</span><span>Research Report</span></div>
  <div class="title-block"><p class="kicker">Research Report</p><h1>{esc(document.title)}</h1>{subtitle}{date}</div>
  {provenance}
</header>
<main id="main">
  <div class="opening"><div class="introduction">{introduction}</div>{legend}</div>
  {toc}
  <div class="sections">{"".join(section_html)}</div>
  {unresolved}
  {bibliography}
</main>
<footer><p>{esc(document.colophon)}</p><p>Rendered with the built-in deeper-research exporter.</p></footer>
</body>
</html>
"""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(full_html)
        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _jimemo_markdown(blocks: Sequence[ContentBlock]) -> str:
    parts = []
    for block in blocks:
        if block.kind != "background":
            parts.append(block.markdown)
            continue
        lines = block.markdown.splitlines()
        if lines:
            lines[0] = f"**Background.** {lines[0]}"
        parts.append("\n".join(f"> {line}" if line else ">" for line in lines))
    return "\n\n".join(part for part in parts if part.strip())


def _jimemo_content(document: ResearchDocument) -> Dict[str, object]:
    content: Dict[str, object] = {
        "title": document.title,
        "kicker": "Research Report",
        "provenance": [
            {"label": label, "value": value} for label, value in document.provenance
        ],
        "body": _jimemo_markdown(document.introduction),
        "legend": [
            {"tag": tag, "meaning": meaning} for tag, meaning in document.evidence_legend
        ],
        "sections": [
            {"heading": section.title, "body": _jimemo_markdown(section.blocks)}
            for section in document.sections
        ],
        "unresolved": _jimemo_markdown(document.unresolved),
        "bibliography": _jimemo_markdown(document.bibliography),
        "colophon": document.colophon,
    }
    if document.subtitle:
        content["subtitle"] = document.subtitle
    if document.compiled:
        content["date"] = f"Compiled {document.compiled}"
    return content


def _jimemo_reason(executable: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            [executable, "info", "research-bible", "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, OSError) as exc:
        return f"jimemo unavailable: {exc}"
    except subprocess.TimeoutExpired:
        return "jimemo compatibility check timed out"
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "template unavailable"
        return f"jimemo incompatible: {detail}"
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "jimemo incompatible: template info was not JSON"
    if not isinstance(info, dict):
        return "jimemo incompatible: template info was not an object"
    slots = info.get("slots")
    if info.get("name") != "research-bible" or not isinstance(slots, dict) or "sections" not in slots:
        return "jimemo incompatible: research-bible template unavailable"
    return None


def export_html(
    document: ResearchDocument,
    output_path: Path,
    jimemo_executable: Optional[str] = None,
) -> HtmlExportResult:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    executable = jimemo_executable if jimemo_executable is not None else shutil.which("jimemo")
    fallback_reason = None
    if executable is None:
        fallback_reason = "jimemo unavailable"
    else:
        fallback_reason = _jimemo_reason(str(executable))

    if fallback_reason is None:
        content_fd, content_name = tempfile.mkstemp(
            prefix=".research-bible-content-", suffix=".json", dir=output_path.parent
        )
        output_fd, temporary_output = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".html", dir=output_path.parent
        )
        os.close(output_fd)
        os.unlink(temporary_output)
        try:
            with os.fdopen(content_fd, "w", encoding="utf-8") as handle:
                json.dump(_jimemo_content(document), handle, ensure_ascii=False, indent=2)
            completed = subprocess.run(
                [str(executable), "render", "research-bible", content_name, "-o", temporary_output],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            rendered = Path(temporary_output)
            if completed.returncode == 0 and rendered.is_file() and rendered.stat().st_size:
                os.replace(rendered, output_path)
                return HtmlExportResult(output_path, "jimemo", None)
            detail = completed.stderr.strip() or completed.stdout.strip() or "no HTML was produced"
            fallback_reason = f"jimemo render failed: {detail}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            fallback_reason = f"jimemo render failed: {exc}"
        finally:
            for name in (content_name, temporary_output):
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass

    render_builtin_html(document, output_path)
    return HtmlExportResult(output_path, "built-in", fallback_reason)


__all__ = [
    "ContentBlock",
    "HtmlExportResult",
    "ResearchDocument",
    "ResearchSection",
    "assembled_html_name",
    "build_document",
    "export_html",
    "render_builtin_html",
    "resolve_bible_path",
]
