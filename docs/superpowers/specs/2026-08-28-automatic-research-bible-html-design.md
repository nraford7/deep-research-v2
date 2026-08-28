# Automatic Research Bible HTML Export

**Date:** 2026-08-28

## Goal

Every completed deeper-research export produces a self-contained HTML Research
Bible beside the existing Markdown Bible. When a compatible `jimemo` installation
is available, deeper-research uses jimemo's `research-bible` template. When jimemo
is absent, outdated, or fails to render, deeper-research uses a bundled renderer
with the same information architecture and visually equivalent styling.

This is additive. Markdown remains the canonical Research Bible, and the existing
BibTeX, claims JSONL, and semantic-index outputs keep their current contracts.

## User-visible contract

`scripts/export.py` continues accepting `--sections`, `--bibliography`, and
`--output-dir`. HTML generation is on by default and may be disabled with
`--no-html`.

The exporter resolves the source Bible in this order:

1. An explicit `--bible /path/to/RESEARCH-BIBLE_topic.md`.
2. One unambiguous `RESEARCH-BIBLE*.md` file already present in `--output-dir`.
3. A normalized document assembled from the section files and master bibliography.

When a Markdown Bible is resolved, the HTML uses the same basename. When the
exporter must assemble from sections, it writes `RESEARCH-BIBLE_<run-slug>.html`,
where the slug comes from the parent of `sections/`. It does not create or replace
a Markdown Bible.

On success the command prints one of:

```text
HTML: .../RESEARCH-BIBLE_topic.html (jimemo)
HTML: .../RESEARCH-BIBLE_topic.html (built-in; jimemo unavailable)
```

If jimemo is installed but cannot render the template, the command prints the
reason and falls back. Failure of the optional renderer never suppresses the
built-in attempt. Failure of both renderers is an export error and returns a
nonzero exit rather than claiming that HTML was produced.

## Architecture

The feature has three layers.

### 1. Normalized research-document model

A small module converts pipeline artifacts into one renderer-neutral object:

- title, subtitle, compilation date, and introductory prose;
- provenance tiles when those values can be read without guessing;
- the fixed evidence-tag legend defined by the deeper-research output contract;
- ordered manuscript sections, each with a heading and Markdown body;
- the verifier's unresolved-links block;
- the master bibliography; and
- a renderer colophon.

Both renderers consume this exact object. Renderer selection therefore cannot
change section order, headings, bibliography contents, evidence labels, or link
warnings.

Normalization applies the pipeline-to-page rules once:

- Sort manuscript files by their natural numeric filename order.
- Exclude `bibliography.md`, `dedup-decisions.md`, and recognized bibliography
  variants from manuscript sections.
- Drop each section's title line, strip a duplicate leading section ordinal from
  the displayed heading, and demote remaining Markdown headings by one level so
  the renderer-owned section heading remains the parent.
- Extract `## ⚠ Unresolved links` only until the next level-two heading or EOF.
- Convert `<!-- editorial:background -->` fences into an explicit normalized
  background-aside marker only after the canonical Markdown has passed the
  pipeline's lint and adversary checks.

The original section files and final Markdown Bible are never rewritten. This
keeps the machine-readable editorial fences available to other renderers and to
future verification.

### 2. Preferred jimemo renderer

The exporter detects jimemo with `shutil.which("jimemo")`, then verifies that the
installed CLI exposes the `research-bible` template. It serializes the normalized
document to a temporary JSON content file and invokes:

```text
jimemo render research-bible <temporary-content.json> -o <final-output.html>
```

JSON avoids adding YAML generation rules to deeper-research and is a documented
jimemo input format. The temporary file is removed after the attempt. Jimemo owns
its exact template, sanitizer, asset inlining, and future visual updates.

### 3. Built-in renderer

The fallback renderer is an independent implementation, not a vendored copy of
jimemo. It uses the same page anatomy: masthead, provenance row, evidence legend,
numbered contents, anchored sections, background asides, unresolved-links warning,
bibliography, and colophon. Its restrained light/dark design is visually
equivalent to jimemo's research-document presentation without promising pixel
identity.

The fallback uses Mistune 3 in escaped-HTML mode. Raw HTML and scripts are never
passed through. Unsafe link protocols are rejected. Remote images are reduced to
their alt text so opening the page makes no network request; supported local
raster images may be embedded as data URLs. CSS is bundled and the result contains
no external stylesheet, font, script, or runtime dependency.

The generated markup uses semantic landmarks, a single `h1`, one `h2` per
top-level section, nested heading levels, unique stable anchors, a real ordered
list for contents, and an accessible warning label.

## Provenance and partial metadata

The exporter may parse provenance values from a finished Bible's standard
provenance line. It may also read explicit run artifacts whose meaning is stable.
It must omit a tile when the value cannot be established; it must not infer that
the evidence gate passed, an adversary was independent, or a citation graph was
verified merely because export was invoked.

Missing optional metadata never blocks HTML. Title, manuscript sections, and
bibliography are sufficient for a valid page.

## Compatibility

- Existing `export.py` callers require no new flags.
- `--no-html` preserves the old machine-artifact-only behavior.
- Consumers of Markdown, BibTeX, JSONL, and the semantic index see no schema or
  content changes.
- Jimemo users receive the upstream jimemo template whenever their installed
  version supports it.
- Non-jimemo users receive an immediately viewable, self-contained HTML file.
- Future renderers can consume the normalized document model without importing
  jimemo-specific slot names or CSS.

## Documentation changes

`SKILL.md` and `README.md` will describe HTML as an automatic additive export,
show `--bible` and `--no-html`, and explain the jimemo-preferred/fallback behavior.
Detailed jimemo mapping belongs in `references/jimemo-export.md`, loaded only for
HTML/jimemo work. The reference will identify Markdown as canonical and document
the section exclusions, heading normalization, unresolved-block boundary, and
background-aside conversion.

## Testing

Tests are written before implementation and cover observable behavior:

1. Natural section ordering, auxiliary-file exclusion, ordinal stripping, heading
   demotion, bibliography separation, and background-aside normalization.
2. Unresolved-link extraction stops at the next level-two heading.
3. A temporary fake jimemo executable proves detection, JSON slot mapping, command
   invocation, and preferred-renderer selection without requiring a developer's
   personal jimemo installation.
4. Missing jimemo and failing/incompatible jimemo both produce the built-in HTML.
5. Fallback output is self-contained, escapes raw HTML, rejects unsafe links, has
   valid landmarks and unique anchors, and does not leak audit sidecars.
6. Explicit Bible paths and automatic basename discovery produce the expected
   neighboring `.html` path.
7. Existing bibliography and claims tests remain green, proving the additive
   export does not change current machine artifacts.

Validation includes the focused export tests, the full repository test suite,
`scripts/quick_validate.py` for the updated skill, and one render over an existing
real Research Bible fixture.

## Non-goals

- Replacing jimemo, publishing HTML, or tracking every jimemo visual change.
- Making HTML the canonical or mechanically verified research artifact.
- Introducing a generic website/theme framework into deeper-research.
- Rewriting existing section Markdown to fit one renderer.
