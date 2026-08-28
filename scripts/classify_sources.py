#!/usr/bin/env python3
"""
classify_sources.py — tier each bibliography entry.

Tiers, in descending evidentiary weight:
  - peer_reviewed   : journal article, conference paper, book chapter (DOI, journal name match)
  - institutional   : government, IGO, central bank, NGO, university working paper
  - book            : monograph or edited volume
  - news            : major newspaper, magazine, wire
  - blog            : personal blog, Substack, Medium, corporate blog
  - wiki            : Wikipedia or wiki-family
  - unknown         : doesn't match any of the above heuristics

Emits a per-entry tier annotation + a summary table (tier mix).

Usage:
  python3 classify_sources.py sections/bibliography.md --output tier-report.md
"""

import argparse
import functools
import json
import re
from collections import Counter
from pathlib import Path

from scripts.helper_runtime import standalone_mutation_guard


PEER_REVIEWED_HINTS = [
    r"\bjournal of\b", r"\breview\b", r"\bproceedings of\b", r"\bquarterly\b",
    r"\bAmerican Economic Review\b", r"\bNature\b", r"\bScience\b", r"\bCell\b",
    r"\bLancet\b", r"\bNEJM\b", r"\bIEEE\b", r"\bACM\b", r"\bSpringer\b",
    r"\bElsevier\b", r"\bWiley\b", r"\bMIT Press\b", r"\bCambridge\b",
    r"\bOxford\b", r"\bRoutledge\b", r"\bAcademy of\b",
    # Generic venue cues
    r"\bConference on\b", r"\bWorkshop on\b", r"\bSymposium\b", r"\bTransactions\b",
    # NLP / ML / HCI venues (and the ACL Anthology)
    r"\bEMNLP\b", r"\bNAACL\b", r"\bEACL\b", r"\bTACL\b", r"\bCOLING\b", r"\bLREC\b",
    r"\bSIGDIAL\b", r"\bComputational Linguistics\b", r"aclanthology\.org",
    r"\bACL\b", r"\bNeurIPS\b", r"\bNIPS\b", r"\bICLR\b", r"\bICML\b", r"\bAAAI\b",
    r"\bIJCAI\b", r"\bCHI\b", r"\bCOLM\b", r"\bCSCW\b",
    # Journals appearing in narrative / computational-humanities work
    r"\bPNAS\b", r"Proceedings of the National Academy", r"\bScience Advances\b",
    r"\bEPJ Data Science\b", r"Humanities and Social Sciences Communications",
    r"\bCognitive Science\b", r"\bDiscourse Processes\b",
    r"Journal of Cultural Analytics", r"\bPLOS\b", r"\bJAIR\b", r"\bScientific Reports\b",
]
INSTITUTIONAL_HINTS = [
    r"\bIMF\b", r"\bWorld Bank\b", r"\bUNCTAD\b", r"\bOECD\b", r"\bUNDP\b",
    r"\bNBER\b", r"\bSSRN\b", r"\bBIS\b", r"\bFederal Reserve\b", r"\bECB\b",
    r"\bBank of England\b", r"\bIEA\b", r"\bWTO\b", r"\bWHO\b", r"\bUNESCO\b",
    r"\bCongressional\b", r"\bGAO\b", r"\bCRS\b", r"\bRAND\b",
    r"\bBrookings\b", r"\bCEPR\b", r"\bChatham House\b", r"\bCFR\b",
    r"\.gov(?:\.[a-z]{2,3})?\b", r"\bcentral bank\b", r"\bworking paper\b",
    r"\btechnical report\b", r"\bwhite paper\b",
]
BOOK_HINTS = [
    r"\bISBN\b", r"\bChapter \d+\b", r"\bUniversity Press\b",
    r"\bUniversity of [A-Z][A-Za-z]+ Press\b", r"\b[A-Z][a-z]+ University Press\b",
    r"\bPress\b\s*[.,]?\s*$", r"\bRoutledge\b", r"\bGuilford\b", r"\bNorton\b",
    r"\bPenguin\b", r"\bVintage\b", r"\bBasic Books\b", r"\bRandom House\b",
    r"\bHarperCollins\b", r"\bFarrar, Straus\b", r"\bDAW Books\b",
    r"\bMichael Wiese\b", r"\bChicago Press\b", r"\bHarvard\b", r"\bYale\b", r"\bPrinceton\b",
]
NEWS_HINTS = [
    r"\bNew York Times\b", r"\bWall Street Journal\b", r"\bFinancial Times\b",
    r"\bThe Economist\b", r"\bReuters\b", r"\bAssociated Press\b", r"\bBloomberg\b",
    r"\bGuardian\b", r"\bWashington Post\b", r"\bLA Times\b", r"\bBBC\b",
    r"\bAxios\b", r"\bPolitico\b", r"\bForeign Policy\b", r"\bForeign Affairs\b",
    r"nytimes\.com", r"wsj\.com", r"ft\.com", r"reuters\.com", r"bloomberg\.com",
]
BLOG_HINTS = [
    r"medium\.com", r"substack\.com", r"\bblog\b", r"wordpress\.com",
    r"linkedin\.com/pulse", r"\bpersonal blog\b",
]
WIKI_HINTS = [
    r"wikipedia\.org", r"\bWikipedia\b", r"fandom\.com", r"wikimedia\.org",
]
PREPRINT_HINTS = [
    r"\barXiv\b", r"arxiv\.org", r"\bpreprint\b", r"\bbioRxiv\b", r"\bmedRxiv\b",
    r"\bOpenReview\b", r"openreview\.net",
]

PATTERNS = [
    ("peer_reviewed", PEER_REVIEWED_HINTS),
    ("institutional", INSTITUTIONAL_HINTS),
    ("book", BOOK_HINTS),
    ("preprint", PREPRINT_HINTS),
    ("news", NEWS_HINTS),
    ("blog", BLOG_HINTS),
    ("wiki", WIKI_HINTS),
]
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def tier_of(url: str, venue: str | None = None) -> str:
    """Thin wrapper over ``classify`` for a retrieval result (URL + optional venue).

    Round-1 slice items arrive as a URL and (sometimes) a venue/source name rather
    than a formatted bibliography line, so we hand ``classify`` the concatenation of
    the two and let its existing hint regexes decide the tier. Returns one of
    classify()'s tiers: peer_reviewed / institutional / preprint / book / news /
    blog / wiki / unknown."""
    return classify(f"{venue or ''} {url}")


def classify(entry: str) -> str:
    if DOI_RE.search(entry):
        for label, hints in PATTERNS:
            if label in ("wiki", "blog", "news"):
                continue
            if any(re.search(h, entry, re.IGNORECASE) for h in hints):
                return label
        return "peer_reviewed"
    for label, hints in PATTERNS:
        if any(re.search(h, entry, re.IGNORECASE) for h in hints):
            return label
    return "unknown"


# The .gov hint reused by descriptors_of (mirrors the pattern in INSTITUTIONAL_HINTS).
GOV_HINT_RE = re.compile(r"\.gov(?:\.[a-z]{2,3})?\b", re.IGNORECASE)

_INSTITUTIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "institutions.json"


@functools.lru_cache(maxsize=1)
def load_institutions():
    """Load the curated institution list, CWD-independently and defensively.

    Returns a list of dicts, each a copy of the source entry with an added
    ``_compiled`` key holding the entry's ``match`` regexes pre-compiled
    (case-insensitive). A missing/unreadable file yields ``[]`` (never raises);
    an entry with a bad regex is skipped rather than crashing the whole load.
    Cached so the JSON is read and compiled only once per process."""
    try:
        raw = _INSTITUTIONS_PATH.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        compiled = []
        ok = True
        for pat in entry.get("match", []) or []:
            try:
                compiled.append(re.compile(pat, re.IGNORECASE))
            except (re.error, TypeError):
                ok = False
                break
        if not ok:
            continue
        record = dict(entry)
        record["_compiled"] = compiled
        out.append(record)
    return out


def descriptors_of(entry: str, tier: str) -> dict:
    """Descriptor sub-classification for an entry already tiered by ``classify``.

    Only meaningful for the institutional tier; every other tier returns ``{}``
    so callers can treat non-institutional entries uniformly. For institutional
    entries:
      - a match against the curated list -> the org's standing/stance, sub=named
      - else a .gov domain -> a named, established research source
      - else (matched only via generic hints like "white paper") -> unverified
    """
    if tier != "institutional":
        return {}

    for org in load_institutions():
        for rx in org.get("_compiled", []):
            if rx.search(entry):
                return {
                    "sub": "named",
                    "standing": org.get("standing", "unknown"),
                    "stance": org.get("stance", "unverified"),
                }

    if GOV_HINT_RE.search(entry):
        return {"sub": "named", "standing": "established", "stance": "research"}

    return {"sub": "unverified", "standing": "unknown", "stance": "unverified"}


def authority_tag(entry: dict) -> str:
    """Transparent source-authority tag: a ``·``-joined bracketed string built
    from whatever provenance fields are present on ``entry``, absent ones omitted.

    Field order: tier · year · N cited · author h-index N · institution · stance
    · replication. ``tier`` is always present, so the minimum output is
    ``[tier]``. Uses middot (U+00B7) as the separator; never an em-dash."""
    parts = [str(entry.get("tier", "unknown"))]

    year = entry.get("year")
    if not year:
        pub = entry.get("published_date")
        if pub and len(str(pub)) >= 4 and str(pub)[:4].isdigit():
            year = str(pub)[:4]
    if year:
        parts.append(str(year))

    cited = entry.get("cited_by")
    if isinstance(cited, int) and not isinstance(cited, bool):
        parts.append(f"{cited} cited")

    h_index = entry.get("h_index")
    if h_index:
        parts.append(f"author h-index {h_index}")

    institution = entry.get("institution")
    if institution:
        parts.append(str(institution))

    if entry.get("stance") == "advocacy":
        parts.append("advocacy")
    if entry.get("sub") == "unverified":
        parts.append("unverified")

    replication = entry.get("replication")
    if replication:
        parts.append(str(replication))

    return "[" + " · ".join(parts) + "]"


BIB_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
URL_RE = re.compile(r"https?://[^\s\)\]]+", re.IGNORECASE)


def parse_entries(text: str):
    """Match dedup_bib.py output: bullet/numbered lines. Academic entries carry
    a 4-digit year; year-less WEB entries are still kept when they carry a URL
    or an explicit n.d. marker, so they can be tiered by domain (not dropped)."""
    entries = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if not BIB_BULLET_RE.match(line):
            continue
        body = BIB_BULLET_RE.sub("", line, count=1).strip()
        if len(body) < 30:
            continue
        if not YEAR_RE.search(body):
            if not (URL_RE.search(body) or "n.d." in body.lower()):
                continue
        entries.append(body)
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bibliography", help="Bibliography markdown file")
    ap.add_argument("--output", default="tier-report.md")
    args = ap.parse_args()

    text = Path(args.bibliography).read_text(encoding="utf-8", errors="replace")
    entries = parse_entries(text)

    classified = [(e, classify(e)) for e in entries]
    counts = Counter(tier for _, tier in classified)
    total = sum(counts.values()) or 1

    lines = [
        "# Source Tier Report",
        "",
        f"Bibliography: `{args.bibliography}`",
        f"Total entries: **{total}**",
        "",
        "## Tier mix",
        "",
        "| Tier | Count | % |",
        "|---|---|---|",
    ]
    for tier in ["peer_reviewed", "institutional", "preprint", "book", "news", "blog", "wiki", "unknown"]:
        n = counts.get(tier, 0)
        lines.append(f"| {tier} | {n} | {100*n/total:.1f}% |")

    quality_score = (
        counts.get("peer_reviewed", 0) * 3
        + counts.get("institutional", 0) * 3
        + counts.get("preprint", 0) * 2
        + counts.get("book", 0) * 2
        + counts.get("news", 0) * 1
    ) / (total * 3)
    # Advisory only: how many institutional entries are unverified (generic-hint
    # matches with no curated org and no .gov). Does NOT feed quality_score.
    institutional_unverified = sum(
        1
        for e, t in classified
        if t == "institutional" and descriptors_of(e, t).get("sub") == "unverified"
    )

    lines += ["", f"## Quality score: **{quality_score:.2f}** / 1.0",
              "",
              "(weighted: peer_reviewed=3, institutional=3, preprint=2, book=2, news=1, blog/wiki/unknown=0)",
              "",
              f"_Advisory: institutional_unverified = **{institutional_unverified}** "
              "(institutional entries matched only by generic hints, not the curated list; "
              "these still count at institutional weight 3 in the score above, flagged here as low-confidence)_",
              ""]

    for tier in ["unknown", "wiki", "blog", "news", "book", "preprint", "institutional", "peer_reviewed"]:
        members = [e for e, t in classified if t == tier]
        if not members:
            continue
        lines += [f"## {tier} ({len(members)})", ""]
        for e in members[:200]:
            lines.append(f"- `{e[:240]}`")
        lines.append("")

    output_path = Path(args.output)
    json_path = output_path.with_suffix(".json")
    with standalone_mutation_guard(output_path, operation="classify sources"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        json_path.write_text(json.dumps({
            "total": total,
            "tier_mix": dict(counts),
            "quality_score": round(quality_score, 3),
            "entries": [{"tier": t, "text": e} for e, t in classified],
        }, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")
    print(f"JSON:   {json_path}")
    print(f"Quality score: {quality_score:.2f}")


if __name__ == "__main__":
    main()
