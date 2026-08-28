# Research Bible HTML and jimemo export

Read this reference when changing HTML export behavior, adding a renderer, or diagnosing
why a jimemo installation was not selected. The ordinary research workflow only needs
the concise Export section in `SKILL.md`.

## Contract

`scripts/export.py` always preserves the verified Markdown Research Bible as the
canonical artifact. Unless `--no-html` is passed, it also writes one self-contained HTML
companion:

1. `--bible PATH` selects the source Bible and its basename.
2. Otherwise, one unambiguous `RESEARCH-BIBLE*.md` in `--output-dir` is selected.
3. Otherwise, the page is assembled from `sections/` and named
   `RESEARCH-BIBLE_<run-slug>.html`; no Markdown file is synthesized.

The exporter detects `jimemo` on `PATH`, confirms that `jimemo info research-bible
--json` exposes the required template, and asks it to render. An absent, outdated,
failing, timed-out, or outputless jimemo attempt automatically falls back to the bundled
renderer. If both renderers fail, export fails rather than claiming an HTML file exists.

## Renderer-neutral normalization

Both renderers consume the same normalized document, so renderer selection cannot
change evidence or page structure.

- Manuscript files are naturally sorted by their numeric filename components.
- `bibliography.md`, `dedup-decisions.md`, and names matching bibliography variants are
  excluded from position sections.
- A section file's first Markdown title is rendered once by the page; its duplicate
  leading section ordinal is removed and remaining headings are demoted under the
  renderer-owned section H2.
- `bibliography.md` fills the dedicated bibliography region with its title line removed.
- The verifier's exact `## ⚠ Unresolved links` block fills the warning region only until
  the next level-two heading or end of file.
- `<!-- editorial:background -->` … `<!-- /editorial -->` becomes an explicit background
  block in the normalized model. The built-in page renders an `<aside>`; the jimemo
  adapter converts only that block to a blockquote with a bold `Background.` lead.
- Provenance tiles are emitted only when the finished Bible explicitly states their
  values. Missing values are omitted; evidence-gate, adversary, or graph status is never
  inferred from the act of exporting.

Normalization is read-only. The source sections and Markdown Bible retain editorial
fences for linting, adversarial review, other renderers, and future verification.

## jimemo slot mapping

The temporary JSON object passed to jimemo maps as follows:

| Normalized value | `research-bible` slot |
|---|---|
| Document title | `title` |
| Subtitle | `subtitle` |
| Compilation date | `date` |
| Fixed page label | `kicker` (`Research Bible`) |
| Explicit corpus facts | `provenance[]` (`label`, `value`) |
| Introductory prose | `body` |
| Evidence-tag definitions | `legend[]` (`tag`, `meaning`) |
| Ordered positions | `sections[]` (`heading`, `body`) |
| Bounded verifier warning | `unresolved` |
| Master source list | `bibliography` |
| Pipeline credit | `colophon` |

The command is:

```text
jimemo render research-bible <temporary-content.json> -o <temporary-output.html>
```

The content file and partial output are removed after the attempt. A successful,
nonempty temporary output atomically replaces the final HTML.

## Built-in visual fallback

The fallback independently implements the same anatomy: research masthead, provenance
row, evidence definition list, numbered contents, anchored position sections,
background asides, warning box, bibliography, and colophon. It aims for visual and
information-architecture equivalence, not pixel identity with a specific jimemo release.

The page contains its CSS inline and loads no external stylesheet, font, script, image,
or runtime. Markdown raw HTML is escaped, unsafe link protocols are not emitted, and
images render as descriptive alt text instead of making network requests. Semantic
landmarks, heading hierarchy, keyboard focus, responsive layout, dark colors, print
styles, and reduced-motion behavior are built in.

## Upstream compatibility

The adapter follows jimemo's documented JSON content format and runtime template
discovery rather than importing jimemo internals. See the upstream
[research-bible template](https://github.com/Joi/jimemo/tree/main/templates/research-bible)
and [research-document recipe](https://github.com/Joi/jimemo/blob/main/README.md#research-documents).
Jimemo owns its exact visuals and sanitizer; deeper-research owns normalization and the
fallback contract.
