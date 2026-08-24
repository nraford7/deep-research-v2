#!/usr/bin/env python3
"""
lint_background.py — reject invented quantities inside editorial background blocks.

Editorial background blocks (see background.py) are synthesis the writer added to
frame a section. They are NOT independently retrieved, so any specific quantity
inside one is unsupported by definition. This linter finds every marked block and
rejects it if it contains a number in ANY form — digits, symbols, spelled-out
cardinals/ordinals/scales, fractions, or word-form years and decades.

Usage:
  python3 scripts/lint_background.py <path-or-dir>

  <path-or-dir> is a single markdown file or a directory scanned for *.md.

A malformed fence is itself a violation: an unclosed OPEN marker (or a CLOSE with
no OPEN) produces zero balanced blocks, so a block-only scan would exit clean and
let uncited quantities through. Each file is therefore checked for balanced
OPEN/CLOSE markers before scanning, and an imbalance fails the lint.

Exit codes:
  0  — all background blocks clean (or no blocks found) and every fence balanced
  1  — a block contains a quantity, or a file has an unbalanced fence; offenders printed
  2  — bad usage (missing/nonexistent path)
"""

import argparse
import re
import sys
from pathlib import Path

# config/llm-style root pattern: background.py lives in scripts/ next to us, but
# mirror the repo convention of putting the skill root on sys.path so the import
# resolves from any CWD.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts import background


# Each trigger is (label, compiled regex). All are case-insensitive and, where a
# word is involved, word-bounded so substrings ("someone" -> "one") never match.
_NUMBER_WORDS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
)
_SCALE_WORDS = r"hundred|thousand|million|billion|trillion|dozen|dozens|score"
_ORDINAL_WORDS = (
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth"
    r"|eighteenth|nineteenth|twentieth"
)
# Vague empirical quantifiers: no digit, but they assert magnitude/frequency.
# The adversary mandate (SKILL Round 4) remains the backstop for comparatives
# the lint still cannot see (doubled, an order of magnitude, more common than).
_QUANTIFIER_WORDS = (
    r"several|many|most|numerous|majority|minority|handful|few|"
    r"myriad|countless|copious|plenty"
)

QUANTITY_TRIGGERS = [
    ("arabic digit", re.compile(r"\d")),
    ("percent sign", re.compile(r"%")),
    ("currency sign", re.compile(r"\$")),
    ("spelled-out cardinal", re.compile(r"\b(?:" + _NUMBER_WORDS + r")\b", re.IGNORECASE)),
    ("scale word", re.compile(r"\b(?:" + _SCALE_WORDS + r")\b", re.IGNORECASE)),
    ("the word percent", re.compile(r"\bpercent\b", re.IGNORECASE)),
    ("spelled-out ordinal", re.compile(r"\b(?:" + _ORDINAL_WORDS + r")\b", re.IGNORECASE)),
    ("vague quantifier", re.compile(r"\b(?:" + _QUANTIFIER_WORDS + r")\b", re.IGNORECASE)),
    # Fraction words: "a third", "a half", "a quarter", "two thirds", "three quarters", ...
    ("fraction word",
     re.compile(r"\b(?:a|one|two|three|four)\s+(?:half|halves|third|thirds"
                r"|quarter|quarters|fourth|fourths|fifth|fifths)\b", re.IGNORECASE)),
    # Word-form years: "nineteen-eighty", "nineteen ninety".
    ("word-form year", re.compile(r"\bnineteen[-\s]\w+", re.IGNORECASE)),
    # Word-form decades / eras: "late nineteen-tens", "mid twenty-twenties".
    ("word-form decade",
     re.compile(r"\b(?:early|mid|late)\s+(?:eighteen|nineteen|twenty)\w*", re.IGNORECASE)),
]


def scan_block(block: str):
    """Return the list of (trigger_label, matched_text) found in one block."""
    hits = []
    for label, pattern in QUANTITY_TRIGGERS:
        m = pattern.search(block)
        if m:
            hits.append((label, m.group(0)))
    return hits


def iter_markdown_paths(target: Path):
    """Yield the markdown file(s) implied by ``target``: the file itself, or every
    *.md under a directory (sorted, recursive)."""
    if target.is_dir():
        yield from sorted(target.rglob("*.md"))
    else:
        yield target


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Markdown file or directory of .md files")
    args = ap.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"error: path not found: {target}", file=sys.stderr)
        sys.exit(2)

    violations = 0
    for md_path in iter_markdown_paths(target):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"error: cannot read {md_path}: {e}", file=sys.stderr)
            sys.exit(2)
        # A malformed fence (unclosed OPEN, or CLOSE before OPEN) yields no
        # balanced blocks, so the block scan below would miss it. Fail here.
        if background.has_unbalanced_fences(text):
            violations += 1
            print(f"VIOLATION: {md_path} — unbalanced editorial:background fence "
                  "(unclosed open or unmatched close)")
            print()
        for i, block in enumerate(background.find_background_blocks(text), start=1):
            hits = scan_block(block)
            if hits:
                violations += 1
                triggers = ", ".join(f"{label} ({matched!r})" for label, matched in hits)
                print(f"VIOLATION: {md_path} — background block {i} contains a quantity")
                print(f"  triggers: {triggers}")
                print("  block:")
                for line in block.splitlines():
                    print(f"    {line}")
                print()

    if violations:
        print(f"{violations} background violation(s): quantities or unbalanced fences.",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
