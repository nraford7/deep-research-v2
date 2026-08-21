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
import json
import re
from collections import Counter
from pathlib import Path


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


BIB_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def parse_entries(text: str):
    """Match dedup_bib.py output: bullet/numbered lines with a year."""
    entries = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if not BIB_BULLET_RE.match(line):
            continue
        body = BIB_BULLET_RE.sub("", line, count=1).strip()
        if len(body) < 30 or not YEAR_RE.search(body):
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
    lines += ["", f"## Quality score: **{quality_score:.2f}** / 1.0",
              "",
              "(weighted: peer_reviewed=3, institutional=3, preprint=2, book=2, news=1, blog/wiki/unknown=0)",
              ""]

    for tier in ["unknown", "wiki", "blog", "news", "book", "preprint", "institutional", "peer_reviewed"]:
        members = [e for e, t in classified if t == tier]
        if not members:
            continue
        lines += [f"## {tier} ({len(members)})", ""]
        for e in members[:200]:
            lines.append(f"- `{e[:240]}`")
        lines.append("")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")

    json_path = Path(args.output).with_suffix(".json")
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
