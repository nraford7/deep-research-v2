"""numkeys.py — extract numeric claims from prose and match them against
retrieved-evidence text.

Shared by sweep_numbers.py (Round-4 gate) and deepen_questions.py (Round-2.5
UNVERIFIED marking). The check is an EXISTENCE tripwire, not source binding:
a claim passes if its normalized number appears (boundary-guarded, unit-aware)
anywhere in the evidence text.
"""

import re

# Bracketed spans that are NOT claims: citation markers, metadata tags,
# footnotes. Stripped before number extraction.
_MARKER_RE = re.compile(r"\[[^\]]{0,200}\]\([^)\s]{0,500}\)|\[[^\]]{0,200}\]|\[\^[^\]]{1,20}\]")
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


def strip_nonclaims(sentence: str) -> str:
    """Remove bracketed citation/metadata/footnote/link spans, parenthetical
    APA cites, and structural references (Section 2.5, Table 3) so their
    years/numbers are never claims."""
    out = _MARKER_RE.sub(" ", sentence)
    out = _PAREN_CITE_RE.sub(" ", out)
    return _SECTION_REF_RE.sub(" ", out)


def _canonical(num: str, scale: str | None) -> str:
    plain = num.replace(",", "")
    if scale:
        mult = _SCALE[scale.lower()]
        val = float(plain) * mult
        if val == int(val):
            return str(int(val))
        return repr(val)
    return plain


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


def _match_positions(key: str, hay: str):
    for m in re.finditer(rf"(?<![\d.]){re.escape(key)}(?![\d.])", hay):
        yield m.start(), m.end()


def _expanded_alternates(claim: dict):
    """Also try the scale-suffixed spellings of an expanded key
    (175000 -> 175k / 175 thousand; 5400000 -> 5.4 million)."""
    key = claim["key"]
    alts = []
    if "." not in key and key.endswith("000"):
        for suffix, mult in (("k", 1_000), ("thousand", 1_000),
                             ("million", 1_000_000), ("bn", 1_000_000_000),
                             ("billion", 1_000_000_000)):
            val = int(key) / mult
            if val >= 1 and (val == int(val) or round(val, 2) == val):
                base = str(int(val)) if val == int(val) else str(round(val, 2))
                alts.append(f"{base}{suffix}" if suffix == "k" else f"{base} {suffix}")
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
