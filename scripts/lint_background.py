#!/usr/bin/env python3
"""
lint_background.py — reject UNCITED quantities inside editorial background blocks.

Editorial background blocks (see background.py) are marked synthesis / explainer
prose the writer added to frame a section. They MAY now carry substance, but an
empirical quantity inside one must be cited: a quantity is a violation only when it
sits in a sentence that carries NO citation marker. This linter finds every marked
block, splits it into sentences, SKIPS any sentence carrying a citation marker
(`[Author, Year]` / `(Author, Year)` with a 4-digit year — its quantities are
cited), and rejects the block if any citation-free sentence contains a number in
ANY form — digits, symbols, spelled-out cardinals/ordinals/scales, fractions, or
word-form years and decades.

Per-sentence licensing (not whole-block): a citation licenses only ITS OWN
sentence, so an uncited quantity in a citation-free sentence still fails even when
another sentence in the block is cited. A second, uncited number hiding inside an
already-cited sentence is the one case the coarse lint cannot catch; that narrow
same-sentence-laundering hole is owned by the Round-4 refute adversary, which reads
the prose and cross-checks figures against the full-text store.

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


# A citation marker: a bracketed or parenthesised reference carrying BOTH a 4-digit
# year (18xx–20xx) AND author-like text (at least one letter), e.g. [Author, 2019],
# [Chalmers 1996], (Dennett, 1991). A sentence carrying one is "cited" and its
# quantities (including the citation's own year) are licensed. The letter requirement
# stops a bare bracketed number / interval / array index from spoofing a citation:
# `[1, 2, 2019]`, `[1900]`, `[2050]` are NOT markers and do not license a sentence.
# A bare year with no brackets/parens is likewise NOT a citation marker.
CITATION_MARKER = re.compile(
    r"[\[(](?=[^\])]*[A-Za-z])[^\])]*\b(?:1[89]\d\d|20\d\d)\b[^\])]*[\])]")


# Dots that end an abbreviation, not a sentence. Splitting on them orphaned a cited
# quantity into a citation-free fragment ("It sold for $300 in 1893 per inv." |
# "records [Smith 2001].") and produced false violations (2026-09-01). Lower-case,
# without the trailing dot; multi-dot forms keep their inner dots ("e.g").
# Abbreviations that commonly introduce what follows ("ca. 1720", "p. 4",
# "inv. nos.", "Dr. Smith"). They join only when the next fragment fits the
# abbreviation's role; an ambiguous capitalised fragment remains a boundary.
# Only tokens that are not also ordinary sentence-final words: "art.", "ill.",
# "no.", "gen.", "rev." and the like are excluded on purpose — "forms of art. The"
# must stay two sentences.
_ABBREVIATIONS = frozenset({
    "ca", "cf", "vs", "viz", "e.g", "i.e", "p", "pp", "pl", "pls", "n", "nn", "nos",
    "inv", "cat", "fig", "figs", "fol", "fols", "vol", "vols", "ed", "eds", "ch",
    "para", "suppl", "illus", "approx", "ff", "fl", "st", "mt", "dr", "mr", "mrs",
    "ms", "prof",
})
_ALWAYS_INTRODUCING_ABBREVIATIONS = frozenset({"e.g", "i.e", "vs", "viz"})
_NAME_INTRODUCING_ABBREVIATIONS = frozenset({"dr", "mr", "mrs", "ms", "prof"})
_EDITOR_ABBREVIATIONS = frozenset({"ed", "eds"})
_PLACE_INTRODUCING_ABBREVIATIONS = frozenset({"st", "mt"})
_ROMAN_REFERENCE_ABBREVIATIONS = frozenset({"fig", "figs", "pl", "pls", "vol", "vols"})
_PAGE_LABEL_ABBREVIATIONS = frozenset({"p", "pp"})
_INITIAL_CONTEXT = re.compile(
    r"(?:^|\b(?:by|to|from|with|of|for|and|or|named|author|artist|scholar))\s*$",
    re.IGNORECASE,
)
_INITIAL_CONTEXT_WORDS = frozenset({
    "by", "to", "from", "with", "of", "for", "and", "or", "named", "author", "artist", "scholar",
})
_PLACE_CONTEXT = re.compile(
    r"(?:^|\b(?:at|in|on|near|from|to|of|around|toward|towards|outside|inside))\s*$",
    re.IGNORECASE,
)
_SENTENCE_STARTERS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "he", "she", "they", "we", "i",
    "then", "however", "therefore", "thus", "meanwhile", "nevertheless", "nonetheless",
})
# Months introduce a date ("Mar. 3", "Jan. 1893") but also end sentences ("in
# Jan. The trend"): joined before a digit, lower-case or a citation marker only.
_MONTHS = frozenset({"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"})
# Abbreviations and forms that end a sentence as often as they introduce the next
# word ("..., etc. The next", "Kanda et al. The census", "in the U.S. The", "Acme
# Inc. The"): joined only when the continuation does not look like a new sentence
# — lower-case or a citation marker; a digit is NOT enough ("U.S. 2020 figures").
# Dotted initialisms (U.S., e.g., Ph.D.) and name suffixes (Jr., Sr.) are in this
# class too. The residual false positives ("47 works in U.S. Census records [X]"
# and "in the U.S. 2020 figures" both split) are accepted so that a cited next
# sentence cannot launder an uncited number — the linter exists to catch those. Single-letter initials are NOT
# ambiguous here: "J. Pierpont Morgan" is everyday art-history prose, a sentence
# ending in a lone capital is not.
_SENTENCE_ENDING_ABBREVIATIONS = frozenset({"etc", "al", "ibid", "cit", "seq", "inc", "ltd", "co", "corp", "bros", "jr", "sr"})
# the token before the final dot: letters in any script, inner dots/hyphens
# ("e.g", "U.S", "J.-P", "É"); the opener may be Markdown emphasis
_TRAILING_TOKEN = re.compile(r"(?:^|[\s(\[\"'“‘«*_`~])([^\W\d_][^\W\d_]*(?:[.\-][^\W\d_]+)*|[^\W\d_](?:\.-[^\W\d_])*)\.$")
_INITIALS = re.compile(r"^[^\W\d_](?:\.?-[^\W\d_])*$")   # J, É, J.-P (single letters joined by hyphens); "U.S"/"e.g" are NOT initials
# closing quotes / brackets / Markdown delimiters that may follow terminal punctuation
_CLOSERS = "”\"’')]*_`~»›」』）】》"
_MARKDOWN_OPENERS = "*_`~"


_SENTENCE_BOUNDARY = re.compile(
    "(?:" + "|".join(r"(?<=[.!?]" + ("[" + re.escape(_CLOSERS) + "]") * n + ")" for n in range(0, 6)) + r")\s+")


def _continues(following: str) -> bool:
    """Does `following` read as the continuation of a clause rather than a new
    sentence? Lower-case (any script) or a citation marker right at the start
    ("et al. [Kanda 2015]"), looking past Markdown emphasis ("*and*"). A digit is
    NOT enough ("in the U.S. 2020 figures ...") and neither is an opening quote —
    quoted speech usually starts a sentence."""
    head = following.lstrip(_MARKDOWN_OPENERS)
    return head[:1].islower() or bool(CITATION_MARKER.match(head))


def _starts_name(following: str) -> bool:
    head = following.lstrip(_MARKDOWN_OPENERS)
    return bool(head[:1] and head[:1].isupper())


def _leading_token(following: str) -> str:
    head = following.lstrip(_MARKDOWN_OPENERS)
    match = re.match(r"([^\s,;:!?()[\]{}]+)", head)
    return match.group(1).rstrip(".\"'\u2019\u201d") if match else ""


def _starts_person_name(following: str) -> bool:
    token = _leading_token(following)
    return bool(token[:1].isupper() and token.lower() not in _SENTENCE_STARTERS)


def _starts_roman_reference(following: str) -> bool:
    return bool(re.fullmatch(r"[IVXLCDM]+", _leading_token(following)))


def _starts_page_label(following: str) -> bool:
    return bool(re.fullmatch(r"(?:\d+[A-Za-z]?|[A-Z]+\d+)", _leading_token(following)))


def _looks_like_name_piece(piece: str) -> bool:
    token = piece.strip("*_`~\"'\u2018\u2019\u201c\u201d(),;:")
    if token.endswith("."):
        token = token[:-1]
    if _INITIALS.fullmatch(token):
        return token[:1].isupper()
    chunks = re.split(r"[-'\u2019]", token)
    return bool(chunks and all(chunk[:1].isupper() and chunk.isalpha() for chunk in chunks))


def _initial_has_name_context(core: str, match: re.Match) -> bool:
    prefix = core[:match.start(1)].rstrip(_MARKDOWN_OPENERS)
    if _INITIAL_CONTEXT.search(prefix):
        return True
    words = prefix.split()
    for index in range(len(words) - 1, -1, -1):
        context_word = words[index].strip("*_`~\"'\u2018\u2019\u201c\u201d(),;:").lower()
        if context_word in _INITIAL_CONTEXT_WORDS:
            tail = words[index + 1:]
            return bool(tail and all(_looks_like_name_piece(piece) for piece in tail))
    return False


def _place_abbreviation_has_context(core: str, match: re.Match) -> bool:
    prefix = core[:match.start(1)].rstrip(_MARKDOWN_OPENERS)
    return not prefix.strip() or bool(_PLACE_CONTEXT.search(prefix))


def _joins_previous(previous: str, following: str) -> bool:
    """True when `following` continues `previous` rather than starting a sentence:
    the split fell after an introducing abbreviation ("ca.", "p.", "Dr.") whose
    continuation fits its role, or a single-letter initial in name context; after
    an ambiguous form ("etc.", "et al.", "Jr.", "U.S.", "Ph.D.") — joined only
    before a continuation (lower-case or a citation marker). Plain periods stay
    boundaries. Ambiguous forms before a capitalised word are resolved toward
    SPLITTING ("in the U.S. The ... [X]" stays two sentences), so a cited next
    sentence cannot launder an uncited number. Months join before a digit as well
    ("Mar. 3")."""
    core = previous.rstrip(_CLOSERS)
    if not core.endswith("."):
        return False   # "?" / "!" end a sentence whatever follows
    m = _TRAILING_TOKEN.search(core)   # look past closers: "*fig.* 2", "*J.* Smith"
    if m:
        token = m.group(1)
        low = token.lower()
        if low in _ABBREVIATIONS:
            if low in _ALWAYS_INTRODUCING_ABBREVIATIONS:
                return True
            if low in _NAME_INTRODUCING_ABBREVIATIONS:
                return _starts_person_name(following) or _continues(following)
            if low in _EDITOR_ABBREVIATIONS:
                return _starts_person_name(following) or _continues(following)
            if low in _PLACE_INTRODUCING_ABBREVIATIONS:
                return (_starts_person_name(following) and _place_abbreviation_has_context(core, m)) or _continues(following)
            if low in _ROMAN_REFERENCE_ABBREVIATIONS and _starts_roman_reference(following):
                return True
            if low in _PAGE_LABEL_ABBREVIATIONS and _starts_page_label(following):
                return True
            head = following.lstrip(_MARKDOWN_OPENERS)
            return head[:1].isdigit() or _continues(following)
        if low in _MONTHS:                              # "Mar. 3", "Jan. 1893" join; "in Jan. The" splits
            head = following.lstrip(_MARKDOWN_OPENERS)
            return head[:1].isdigit() or _continues(following)
        if (_INITIALS.match(token) and token[:1].isupper() and len(token) <= 5
                and _starts_name(following) and _initial_has_name_context(core, m)):
            return True                                  # "by J. Smith", "to É. Zola"
        if low in _SENTENCE_ENDING_ABBREVIATIONS or "." in token or "-" in token:   # etc., et al., Jr., U.S., Ph.D.
            return _continues(following)
    # Ambiguous plain periods remain boundaries. Lowercase-styled sentence starts
    # such as "pH was measured" must not license an uncited previous quantity.
    return False


def _split_sentences(block: str):
    """Split a block into sentences on terminal punctuation (optionally followed by
    closing quotes/brackets/Markdown delimiters) and whitespace, then re-join
    fragments that were cut at a contextually introducing abbreviation ("ca. 1720",
    "inv. nos.", "p. 4"), at an ambiguous one followed by a continuation ("et al. [Kanda 2015]",
    "U.S. and"), or at an initial in high-confidence name context. A block with no
    terminal punctuation is one sentence."""
    # up to five closing quotes/brackets/Markdown delimiters after the terminal
    # punctuation still end the sentence: .” .) .** .”) .”**)
    parts = [p for p in _SENTENCE_BOUNDARY.split(block.strip()) if p.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and _joins_previous(merged[-1], part):
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return merged


def scan_block(block: str):
    """Return the list of (trigger_label, matched_text) quantity violations in one
    block, applying PER-SENTENCE citation licensing: a sentence carrying a citation
    marker is skipped (its quantities are cited); every citation-free sentence is
    scanned for quantity triggers. An uncited quantity in a citation-free sentence is
    a violation — a cited sibling sentence never launders the block."""
    hits = []
    for sentence in _split_sentences(block):
        if CITATION_MARKER.search(sentence):
            continue  # licensed — cited substance may carry quantities
        for label, pattern in QUANTITY_TRIGGERS:
            m = pattern.search(sentence)
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
