#!/usr/bin/env python3
"""sweep_numbers.py — Round-4 number-provenance sweep.

For every digit-bearing claim in the section prose, check the number exists
somewhere in the retrieved evidence: extracted full texts, slice-row
text/highlights (many valid sources are highlight-only — fetch_fulltext is
fail-open), inherited corpus rows, and Round-2.5 grounding evidence.

This is an EXISTENCE CHECK (tripwire), not source binding: a flagged number
appears NOWHERE in retrieved evidence — the fabrication class. A number that
exists in an unrelated source still passes; source binding remains the
Round-4 adversary's job.

Usage:
  python3 scripts/sweep_numbers.py --run-dir DIR --output PATH
  python3 scripts/sweep_numbers.py --sections DIR --evidence DIR [--evidence F...] --output PATH

Exit: 0 clean · 1 at least one flag · 2 usage.
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts import numkeys
from scripts.helper_runtime import resolve_helper_layout, standalone_mutation_guard
from scripts.lint_background import _split_sentences


def _resolve_text_path(tp: str, layout):
    """Resolve a row's text_path the way background.py:152-163 does:
    V2 rows carry run-root-relative "Sources/..." paths; legacy rows carry
    round1-relative "sources/..." paths; inherited rows were rewritten to
    run-root-relative by run_extension.py."""
    p = Path(tp)
    if p.is_absolute():
        return p
    if tp.startswith("Sources/"):
        return layout.run_root / tp
    return layout.round1 / tp


def _iter_row_text(jsonl: Path, layout):
    """Evidence from corpus rows: highlights + inline text + the spilled
    full-text file each row points at (inherited rows only carry text_path —
    without reading it, inherited evidence is invisible to the sweep)."""
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("text"):
            yield str(row["text"])
        for h in row.get("highlights") or []:
            yield str(h)
        tp = row.get("text_path")
        if isinstance(tp, str) and tp.strip():
            try:
                yield _resolve_text_path(tp, layout).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                pass


# Grounding-JSON keys whose values are EVIDENCE TEXT. A blind string-walk
# would also collect urls / ids / titles — numeric URL ids could then
# falsely "support" a claim.
_EVIDENCE_KEYS = {"text", "highlights", "snippet", "snippets", "quote",
                  "excerpt", "content", "citation"}


def _iter_grounding_text(path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return

    def walk(node, under_evidence_key):
        if isinstance(node, str):
            if under_evidence_key and node.strip():
                yield node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, str(k).lower() in _EVIDENCE_KEYS)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v, under_evidence_key)
    yield from walk(payload, False)


def _answer_evidence_block(path: Path) -> str:
    """ONLY the ## Evidence section of a Round-2.5 answer file. Reading the
    whole file would let a fabricated number copied from the answer prose
    into a section support ITSELF."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines, keep, out = text.split("\n"), False, []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("## "):
            keep = stripped == "## Evidence"
            continue
        if keep:
            out.append(ln)
    return "\n".join(out)


def collect_evidence_text(run_dir: Path) -> str:
    layout = resolve_helper_layout(run_dir, allow_unmanaged=True)
    parts = []
    src = layout.extracted_sources
    if src.is_dir():
        # rglob: inherited full texts live under Sources/Extracted/inherited/.
        for f in sorted(src.rglob("*.txt")):
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
    r1 = layout.round1
    if r1.is_dir():
        for jsonl in sorted(r1.glob("slice_*.jsonl")) + [r1 / "inherited_corpus.jsonl"]:
            if jsonl.exists():
                parts.extend(_iter_row_text(jsonl, layout))
    r25 = layout.round2_5
    if r25.is_dir():
        for g in sorted(r25.glob("grounding_*.json")):
            parts.extend(_iter_grounding_text(g))
        for a in sorted(r25.glob("answer_*.md")):
            parts.append(_answer_evidence_block(a))
    return numkeys.normalize_haystack("\n".join(parts))


def sweep(sections_dir: Path, norm_haystack: str):
    flags, supported = [], []
    for f in sorted(sections_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        in_fence = False
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or line.lstrip().startswith("#"):
                continue
            for sent in _split_sentences(line):
                clean = numkeys.strip_nonclaims(sent)
                for claim in numkeys.extract_claims(clean):
                    if numkeys.claim_supported(claim, norm_haystack):
                        supported.append({"key": claim["key"], "file": str(f)})
                    else:
                        flags.append({"claim": claim,
                                      "keys_searched": numkeys.search_keys(claim),
                                      "file": str(f), "line": line_no,
                                      "sentence": sent.strip()[:400]})
    return flags, supported


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir")
    ap.add_argument("--sections")
    ap.add_argument("--evidence", action="append", default=[])
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    if bool(args.run_dir) == bool(args.sections):
        print("exactly one of --run-dir / --sections is required", file=sys.stderr)
        return 2

    if args.run_dir:
        run_dir = Path(args.run_dir)
        layout = resolve_helper_layout(run_dir, allow_unmanaged=True)
        sections_dir = layout.sections
        haystack = collect_evidence_text(run_dir)
    else:
        sections_dir = Path(args.sections)
        parts = []
        for e in args.evidence:
            p = Path(e)
            files = sorted(p.glob("*.txt")) if p.is_dir() else [p]
            parts.extend(f.read_text(encoding="utf-8", errors="replace") for f in files)
        haystack = numkeys.normalize_haystack("\n".join(parts))

    if not sections_dir.is_dir():
        print(f"sections dir not found: {sections_dir}", file=sys.stderr)
        return 2

    flags, supported = sweep(sections_dir, haystack)

    out = [
        "# Number-Provenance Sweep",
        "",
        "This is an **existence check** (tripwire), not source binding: a",
        "flagged number appears NOWHERE in the retrieved evidence text.",
        "A passing number may still be misattributed — the adversary owns",
        "source binding.",
        "",
        f"- Claims checked: **{len(flags) + len(supported)}**",
        f"- Unsupported (flagged): **{len(flags)}**",
        "",
    ]
    if flags:
        out += ["## ⚠ Numbers with no evidence support", ""]
        for fl in flags[:300]:
            c = fl["claim"]
            out.append(f"- `{c['raw']}` (searched: "
                       f"{', '.join(fl['keys_searched'])}; unit {c['unit'] or '—'}) "
                       f"— {fl['file']}:{fl['line']}\n  > {fl['sentence']}")
        out.append("")

    output_path = Path(args.output)
    with standalone_mutation_guard(output_path, operation="number sweep"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(out), encoding="utf-8")
        output_path.with_suffix(".json").write_text(json.dumps({
            "flags": flags,
            "supported_keys": supported,
            "stats": {"checked": len(flags) + len(supported),
                      "flagged": len(flags)},
        }, indent=2), encoding="utf-8")
    print(f"Sweep: {len(flags)} unsupported / {len(flags) + len(supported)} checked "
          f"→ {output_path}")
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
