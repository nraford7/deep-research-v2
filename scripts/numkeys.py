"""numkeys.py — extract numeric claims from prose and match them against
retrieved-evidence text.

Shared by sweep_numbers.py (Round-4 gate) and deepen_questions.py (Round-2.5
UNVERIFIED marking). The check is an EXISTENCE tripwire, not source binding:
a claim passes if its normalized number appears (boundary-guarded, unit-aware)
anywhere in the evidence text.
"""

import decimal
import re

# Markdown structure around bracketed spans. Links keep their visible LABEL
# (only the (url) part is dropped); footnote markers vanish; a plain
# bracketed span is stripped ONLY when citation-ish or metadata (see
# _bracket_repl) — other bracketed prose keeps its text, brackets removed.
_LINK_RE = re.compile(r"\[([^\]]{0,200})\]\(([^)\s]{0,500})\)")
_FOOTNOTE_RE = re.compile(r"\[\^[^\]]{1,20}\]")
_BRACKET_RE = re.compile(r"\[([^\]]{0,200})\]")
_META_PREFIXES = ("as of:", "confidence:", "disputed:", "unverified", "r2.5-")
_KB_CITE_SHAPE_RE = re.compile(
    r"kb:[a-z0-9][a-z0-9-]*\s*,\s*(?:\d{4}[a-z]?|n\.d\.)", re.IGNORECASE)
_CITE_YEAR_RE = re.compile(
    r",\s*(?:\d{4}[a-z]?(?![0-9a-z])|n\.d\.)", re.IGNORECASE)
_PAREN_CITE_RE = re.compile(r"\(\s*[A-Za-z][^()]{0,80},\s*(?:\d{4}[a-z]?|n\.d\.)\s*\)")

_CURRENCY_BEFORE = r"(?:US?\$|USD|EUR|GBP|€|£|\$)"
_CURRENCY_AFTER = r"(?:USD|EUR|GBP|dollars?|euros?|pounds?)"
_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
          "mn": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
          "billion": 1_000_000_000}

_NUM_RE = re.compile(
    rf"(?P<cur>{_CURRENCY_BEFORE}\s*)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<scale>k|thousand|m|mn|million|bn|b|billion)\b)?"
    rf"(?:(?P<pct>\s*(?:%|percent\b))|(?P<curafter>\s*{_CURRENCY_AFTER}\b))?",
    re.IGNORECASE,
)

# Structural references whose numbers are never claims (spec: section
# numbering excluded).
_SECTION_REF_RE = re.compile(
    r"\b(?:section|sec\.|§|part|chapter|ch\.|figure|fig\.|table|appendix|"
    r"footnote|note|page|p\.|pp\.)\s*\d+(?:\.\d+)*", re.IGNORECASE)


def _bracket_repl(m: re.Match) -> str:
    """Strip a plain bracketed span only when it is citation-ish or metadata;
    otherwise it is visible prose — keep the text, drop the brackets."""
    inner = m.group(1).strip()
    low = inner.lower()
    if low.startswith(_META_PREFIXES):
        return " "
    if _KB_CITE_SHAPE_RE.match(inner):
        return " "
    if _CITE_YEAR_RE.search(inner):
        return " "
    return f" {inner} "


def strip_nonclaims(sentence: str) -> str:
    """Remove citation/metadata spans WITHOUT deleting visible text: markdown
    link labels survive (only the (url) part is dropped), footnote markers
    [^x] vanish, bracketed spans are stripped only when citation-ish or
    metadata (metadata prefix, kb-cite shape, or a comma-year/n.d. tail) —
    other bracketed prose keeps its text so its numbers stay claims.
    Parenthetical APA cites and structural references (Section 2.5, Table 3)
    are removed as before."""
    out = _LINK_RE.sub(r" \1 ", sentence)
    out = _FOOTNOTE_RE.sub(" ", out)
    out = _BRACKET_RE.sub(_bracket_repl, out)
    out = _PAREN_CITE_RE.sub(" ", out)
    return _SECTION_REF_RE.sub(" ", out)


def _canonical(num: str, scale: str | None) -> str:
    """Decimal, not float: 1.07 * 1e9 in float leaves artifacts
    ("1070000000.0000001"). Also strips trailing decimal zeros
    ("2.20" → "2.2") so spellings normalize to one key."""
    plain = num.replace(",", "")
    val = decimal.Decimal(plain)
    if scale:
        val *= _SCALE[scale.lower()]
    return format(val.normalize(), "f")


def extract_claims(sentence: str) -> list[dict]:
    """Callers pass strip_nonclaims(sentence) — that is where citation /
    metadata / section-reference years are removed. Everything numeric that
    survives, bare prose years included, is a checkable claim."""
    claims = []
    for m in _NUM_RE.finditer(sentence):
        num, scale = m.group("num"), m.group("scale")
        unit = ("percent" if m.group("pct")
                else "currency" if (m.group("cur") or m.group("curafter"))
                else None)
        claims.append({"raw": m.group(0).strip(),
                       "key": _canonical(num, scale), "unit": unit})
    return claims


def search_keys(claim: dict) -> list[str]:
    """The full list of normalized spellings claim_supported will try —
    exposed so sweep reports can show exactly what was searched."""
    return [claim["key"]] + _expanded_alternates(claim)


def normalize_haystack(text: str) -> str:
    """Lowercase and strip thousands-separators so `175,000` == `175000`."""
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text.lower())


_UNIT_WINDOW = 16  # chars of context checked for %/currency markers


_LETTER_SUFFIXES = ("k", "m", "bn", "b")


def _match_positions(key: str, hay: str):
    # Trailing boundary: a digit-ending key tolerates a sentence-final period
    # ("...employs 175,000.") but not a digit or decimal continuation; a
    # letter-ending key (175k) must not continue into a unit (175kg).
    tail = r"(?![a-z0-9])" if key[-1].isalpha() else r"(?!\d|\.\d)"
    for m in re.finditer(rf"(?<![\d.]){re.escape(key)}{tail}", hay):
        yield m.start(), m.end()


def _expanded_alternates(claim: dict):
    """Also try the scale-suffixed spellings of an expanded key
    (2500 -> 2.5k / 2.5 thousand; 5400000 -> 5.4m / 5.4 million)."""
    key = claim["key"]
    alts = []
    if key.isdigit() and int(key) >= 1000:
        for suffix, mult in (("k", 1_000), ("thousand", 1_000),
                             ("m", 1_000_000), ("million", 1_000_000),
                             ("bn", 1_000_000_000), ("b", 1_000_000_000),
                             ("billion", 1_000_000_000)):
            val = int(key) / mult
            if val >= 1 and (val == int(val) or round(val, 2) == val):
                base = str(int(val)) if val == int(val) else str(round(val, 2))
                alts.append(f"{base}{suffix}" if suffix in _LETTER_SUFFIXES
                            else f"{base} {suffix}")
    return alts


def claim_supported(claim: dict, norm_haystack: str) -> bool:
    keys = search_keys(claim)
    for key in keys:
        for start, end in _match_positions(key.lower(), norm_haystack):
            if claim["unit"] is None:
                return True
            before = norm_haystack[max(0, start - _UNIT_WINDOW):start]
            after = norm_haystack[end:end + _UNIT_WINDOW]
            if claim["unit"] == "percent" and re.search(r"%|percent", after):
                return True
            if claim["unit"] == "currency" and (
                    re.search(r"\$|usd|eur|gbp|€|£", before)
                    or re.search(r"\$|usd|eur|gbp|€|£|dollar|euro|pound", after)):
                return True
    return False
