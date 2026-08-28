# Automatic Research Bible HTML Export Implementation Plan

> **For Codex:** Execute this plan test-first under the approved design in
> `docs/superpowers/specs/2026-08-28-automatic-research-bible-html-design.md`.

**Goal:** Make `scripts/export.py` automatically emit a self-contained Research
Bible HTML companion while preserving every existing Markdown and machine-export
contract. Prefer an installed compatible jimemo renderer; otherwise produce a
safe, visually equivalent built-in page.

**Architecture:** Add a renderer-neutral document model in
`scripts/research_bible_html.py`. One normalization path feeds two renderers: a
jimemo adapter driven through its CLI and a bundled Mistune-based renderer. Keep
all renderer selection in the new module and make `export.py` a thin caller.

**Tech stack:** Python 3, stdlib dataclasses/subprocess/tempfile, Mistune 3,
pytest, upstream jimemo CLI when available.

**Risk:** Medium. HTML is additive and has a `--no-html` escape hatch, but it is
enabled by default for an existing command. The fallback therefore must be
non-networking, safe against raw HTML and unsafe URLs, and must not change the
existing BibTeX/claims outputs.

---

## Task 1: Lock the normalized document and safe fallback contracts

**Files:**

- Create: `tests/test_research_bible_html.py`
- Create: `scripts/research_bible_html.py`
- Create: `assets/research-bible.css`
- Modify: `requirements.txt`

### Step 1: Write failing normalization tests

Create fixtures with numbered section files, `bibliography.md`,
`dedup-decisions.md`, a bibliography variant, nested headings, an unresolved
links block followed by another H2, and editorial background fences. Assert:

- natural numeric ordering;
- auxiliary-file exclusion;
- leading ordinal removal from display headings;
- nested heading demotion;
- bibliography and unresolved links occupy dedicated fields;
- background fences become explicit `ContentBlock(kind="background")` values;
- no source Markdown is changed.

Run:

```bash
python3 -m pytest tests/test_research_bible_html.py -q
```

Expected: FAIL because `scripts.research_bible_html` does not exist.

### Step 2: Implement the minimum renderer-neutral model

Add these public objects and helpers:

```python
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

def build_document(sections_dir: Path, bibliography_path: Path,
                   bible_path: Optional[Path] = None) -> ResearchDocument: ...
```

Use deterministic parsing only. Do not infer pipeline pass/fail state. Parse
optional provenance from the standard final-Bible line when present; omit
unknown values. Leave every input file untouched.

### Step 3: Write failing fallback-renderer tests

Assert that `render_builtin_html()` produces:

- one H1, renderer-owned H2 section headings, stable unique anchors, and a real
  ordered-list contents;
- masthead, optional provenance, evidence legend definition list, background
  asides, warning region, bibliography, and colophon;
- inlined CSS with no external stylesheet, font, script, or runtime;
- escaped raw HTML, rejected `javascript:` links, and no remote image request;
- meaningful image alt text retained as text.

### Step 4: Implement the fallback renderer

Configure Mistune 3 with escaped raw HTML. Render normalized blocks rather than
rewriting the original Markdown. Override image rendering so remote images become
escaped alt text; embed only supported local raster files as data URLs when the
path resolves under an explicitly supplied content root. Inline
`assets/research-bible.css` into the page and emit semantic landmarks.

Use a restrained editorial page with jimemo-equivalent information hierarchy,
responsive layout, keyboard-visible focus, readable measure, light/dark colors,
and reduced-motion support. Do not copy jimemo source CSS or template markup.

### Step 5: Verify Task 1

Run:

```bash
python3 -m pytest tests/test_research_bible_html.py -q
```

Expected: PASS.

---

## Task 2: Add preferred jimemo rendering and automatic export

**Files:**

- Modify: `tests/test_research_bible_html.py`
- Create: `tests/test_export_html.py`
- Modify: `scripts/research_bible_html.py`
- Modify: `scripts/export.py`

### Step 1: Write failing jimemo adapter tests

Create a temporary fake `jimemo` executable that records arguments and content.
Cover:

- compatible jimemo is detected and receives JSON matching the normalized model;
- the command is `jimemo render research-bible <content.json> -o <output>`;
- the temporary JSON is removed;
- missing, incompatible, failing, or apparently successful-but-outputless jimemo
  all select the built-in renderer and return an explanatory reason;
- jimemo never receives the canonical files for mutation.

### Step 2: Implement renderer selection

Add:

```python
@dataclass(frozen=True)
class HtmlExportResult:
    path: Path
    renderer: str
    fallback_reason: Optional[str]

def export_html(document: ResearchDocument, output_path: Path,
                jimemo_executable: Optional[str] = None) -> HtmlExportResult: ...
```

When no executable override is provided, use `shutil.which("jimemo")`. Confirm
template availability without parsing human prose more than necessary. Serialize
to a temporary JSON file, render to a temporary output in the destination
directory, and atomically replace the final HTML only after a nonempty output is
present. On any jimemo problem, remove temporaries and call the built-in renderer.

### Step 3: Write failing CLI/integration tests

Invoke `scripts.export.main(argv)` against temporary artifacts. Cover:

- `--bible` produces the same-basename HTML;
- one unambiguous `RESEARCH-BIBLE*.md` in the output directory is discovered;
- ambiguous Bible candidates fail with a useful error;
- section-only assembly uses `RESEARCH-BIBLE_<run-slug>.html` and creates no
  Markdown Bible;
- HTML is automatic by default;
- `--no-html` preserves the former machine-artifact-only behavior;
- existing `bibliography.bib` and `claims.jsonl` contents are unchanged;
- CLI output identifies jimemo or built-in and explains fallback when relevant.

### Step 4: Wire the exporter

Update `export.py` to expose `main(argv=None)`, add optional `--bible` and
`--no-html`, resolve the source and output basename exactly as specified, then
call the new module after the existing BibTeX and claims writes. Return nonzero
only when both HTML paths fail. Keep existing parser/export helpers unchanged.

### Step 5: Verify Task 2

Run:

```bash
python3 -m pytest tests/test_research_bible_html.py tests/test_export_html.py tests/test_parsers.py -q
```

Expected: PASS.

---

## Task 3: Document the contract and validate the skill

**Files:**

- Modify: `SKILL.md`
- Modify: `README.md`
- Create: `references/jimemo-export.md`

### Step 1: Update concise user-facing guidance

In `SKILL.md`'s Export section and the README helper/output sections, state:

- HTML is generated automatically beside Markdown;
- installed compatible jimemo is preferred;
- every user still gets a self-contained visual fallback;
- Markdown remains canonical;
- `--bible` controls source/basename and `--no-html` opts out.

Keep detailed slot mapping and normalization rules out of `SKILL.md`.

### Step 2: Add the progressive-disclosure reference

Create `references/jimemo-export.md` with:

- the automatic selection flow;
- normalized-document-to-jimemo slot mapping;
- section exclusions and heading rules;
- exact unresolved-links boundary;
- editorial-background conversion and canonical-Markdown guarantee;
- visual-equivalence, security, and failure behavior;
- upstream jimemo Research Bible template and README links.

### Step 3: Validate documentation and skill structure

Run:

```bash
python3 /Users/noahraford/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/noahraford/Projects/deeper-research
```

Expected: PASS.

---

## Task 4: Real render, review, and repository verification

**Files:**

- Modify only files required by findings from review.

### Step 1: Render a real existing Bible

Choose a checked-in `research/*/export/RESEARCH-BIBLE*.md` fixture. Force the
built-in path and confirm the neighboring HTML opens without network assets.
Also run the fake-compatible-jimemo test path.

### Step 2: Visually inspect responsive output

Inspect the fallback page at desktop and narrow mobile widths. Check table of
contents navigation, hierarchy, wrapping, asides, warning contrast, bibliography,
dark mode, keyboard focus, and absence of horizontal overflow. Fix material
problems and repeat focused tests.

### Step 3: Run an independent code review

Provide the reviewer the approved spec, this plan, base commit, full changed-file
package, and verification output. Require correctness, security, compatibility,
and maintainability review. Resolve every material finding and ask the same
reviewer to confirm closure.

### Step 4: Run final verification from a clean command boundary

Run:

```bash
python3 -m pytest tests/test_research_bible_html.py tests/test_export_html.py -q
python3 -m pytest -q
python3 /Users/noahraford/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/noahraford/Projects/deeper-research
git diff --check
git status --short
```

Expected: all tests and validation pass; only intended files are changed before
the implementation commit.

## Rollback

Pass `--no-html` for immediate operational rollback. Full code rollback removes
`scripts/research_bible_html.py`, `assets/research-bible.css`, the two new test
files, and `references/jimemo-export.md`, then reverts the small edits to
`scripts/export.py`, `requirements.txt`, `SKILL.md`, and `README.md`. Existing
Markdown, BibTeX, and claims artifacts remain valid throughout.
