"""Minimal regression tests for parser behavior that's easy to silently break.

Run:
    python3 -m pytest tests/
    or
    python3 tests/test_parsers.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.verify_citations import (
    INLINE_CITE_RE,
    extract_inline_cites,
    extract_bibliography,
    first_surname,
)
from scripts.export import parse_bib_entries as export_parse_bib, to_bibtex_entry
from scripts.classify_sources import parse_entries as classify_parse_entries, classify
from scripts.dedup_bib import extract_title_key, extract_year, similarity, cluster_entries
from scripts.lit_search import _normalize_doi, compare_against_bib


def t(label, expr):
    if expr:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        sys.exit(1)


# --- INLINE CITATIONS ---

def test_inline_cite_formats():
    text = """
    [Smith, 2020] and [Jones et al., 2021] but also (Brown, 2022) and
    [van der Berg, 2019] plus [U.S. Treasury, 2024] and [Smith & Jones, 2023]
    and [Smith and Jones, 2018]. Noise like [12] should be ignored,
    and a year alone like 2020 should be ignored.
    """
    cites = extract_inline_cites(text)
    authors = {c["author"].lower() for c in cites}
    t("Smith 2020", any("smith" == a.strip() for a in authors))
    t("Jones et al 2021", any("et al" in a for a in authors))
    t("Brown 2022 (parens)", any("brown" in a for a in authors))
    t("van der Berg 2019", any("van der berg" in a for a in authors))
    t("U.S. Treasury 2024", any("u.s. treasury" in a or "u.s." in a for a in authors))
    t("Smith & Jones 2023", any("smith & jones" in a for a in authors))
    t("Smith and Jones 2018", any("smith and jones" in a for a in authors))
    t("Numeric [12] rejected", not any(a.strip() in ("1", "12") for a in authors))


def test_first_surname():
    t("solo", first_surname("Smith") == "smith")
    t("et al", first_surname("Smith et al.") == "smith")
    t("ampersand", first_surname("Smith & Jones") == "smith")
    t("and", first_surname("Smith and Jones") == "smith")
    t("particle", first_surname("van der Berg") == "berg")
    t("dotted institution", first_surname("U.S. Treasury") == "treasury")


# --- BIBLIOGRAPHY PARSERS ---

DEDUP_OUTPUT = """\
# Master Bibliography

Deduplicated across 5 model outputs (123 → 87).

- Smith, J. (2020). Monetary policy in emerging markets. *Journal of Finance*, 75(3). doi:10.1111/jofi.12345
- Jones, A., & Brown, B. (2021). A survey of CBDC adoption. *Annual Review of Economics*, 13.
- van der Berg, K. (2019). Capital flows and crises. World Bank Working Paper.

# Some Other Section
- This line is in another section, should be ignored
"""


def test_export_parse_bib_skips_prose():
    entries = export_parse_bib(DEDUP_OUTPUT)
    t("3 entries parsed", len(entries) == 3)
    t("Prose 'Deduplicated across' skipped",
      not any("deduplicated" in e.lower() for e in entries))
    t("Section divider skipped",
      not any("ignored" in e for e in entries))


def test_classify_parse_bib_skips_prose():
    entries = classify_parse_entries(DEDUP_OUTPUT)
    t("classify_sources: 3 entries", len(entries) == 3)
    t("classify_sources: no prose",
      not any("deduplicated" in e.lower() for e in entries))


def test_classify_tiers():
    t("DOI -> peer_reviewed", classify("Smith, J. (2020). Title. doi:10.1111/jofi.12345") == "peer_reviewed")
    t("NBER -> institutional", classify("Jones, A. (2021). Topic. NBER Working Paper 12345.") == "institutional")
    t("Wikipedia -> wiki", classify("CBDC. en.wikipedia.org/wiki/CBDC") == "wiki")
    # Should NOT match because "gov.ernment" was a bad regex; now we use \.gov(\.cc)?
    t("'government' string -> not institutional via gov regex",
      classify("Smith. Discussion of government policy in 2020.") != "institutional")
    t("'.gov' URL -> institutional",
      classify("Treasury report 2024. https://www.treasury.gov/report.pdf") == "institutional")


def test_classify_tiers_scholarly_venues():
    # NLP/ML/HCI conference papers and major journals should be peer_reviewed.
    t("EMNLP -> peer_reviewed",
      classify("Yang, K. (2022). Re3. Findings of EMNLP 2022.") == "peer_reviewed")
    t("aclanthology -> peer_reviewed",
      classify("Doe, J. (2023). Title. aclanthology.org/2023.acl-long.1") == "peer_reviewed")
    t("PNAS -> peer_reviewed",
      classify("Reinhart, A. (2025). Do LLMs write like humans. PNAS 122(8).") == "peer_reviewed")
    # Bare arXiv preprints get their own tier, not 'unknown'.
    t("arXiv -> preprint",
      classify("Pham, C. (2025). Frankentext. arXiv:2505.18128") == "preprint")
    # University-press books (incl. "University of X Press") classify as book.
    t("University of X Press -> book",
      classify("Propp, V. (1968). Morphology of the Folktale. University of Texas Press.") == "book")


def test_bibliography_parser_handles_master_heading_and_subcategories():
    # Regression: a "# Master Bibliography" heading (keyword not first) with
    # "### Category" subheadings must parse ALL entries, not stop at the first
    # subheading and not fail the header match entirely.
    bib = (
        "# Master Bibliography\n\n"
        "Method: deduplicated across reports.\n\n"
        "### A. Formal narratology\n"
        "- Genette, G. (1980). Narrative Discourse. Cornell University Press.\n"
        "- Propp, V. (1968). Morphology of the Folktale. University of Texas Press.\n\n"
        "### B. Computational\n"
        "- Russell, J. et al. (2026). StoryScope. arXiv:2604.03136\n"
        "- Reagan, A. et al. (2016). Emotional arcs. EPJ Data Science 5:31.\n"
    )
    entries = extract_bibliography(bib)
    t("master-bibliography heading parses >= 4 entries", len(entries) >= 4)
    t("entries span both subcategories",
      any("Genette" in e for e in entries) and any("StoryScope" in e for e in entries))
    t("category subheadings are not captured as entries",
      not any(e.strip().lower().startswith("b. computational") for e in entries))


# --- DEDUP ---

def test_dedup_does_not_overmerge_distinct_papers():
    by_origin = {
        "a.md": ["- Smith, J. (2020). Monetary policy in emerging markets. *Journal of Finance*."],
        "b.md": ["- Smith, J. (2020). Monetary policy and emerging markets: a survey. *Annual Review*."],
    }
    clusters = cluster_entries(by_origin, threshold=0.92)
    # These are different works (different titles, same author/year). Threshold 0.92
    # with title-key truncation should keep them separate.
    t("Distinct papers stay separate (no over-merge)", len(clusters) == 2)


def test_dedup_merges_same_doi():
    by_origin = {
        "a.md": ["- Smith, J. (2020). Title v1. doi:10.1111/abc"],
        "b.md": ["- Smith, J. (2020). Title v1 longer version. doi:10.1111/abc"],
    }
    clusters = cluster_entries(by_origin, threshold=0.92)
    t("Same DOI merges", len(clusters) == 1)


def test_dedup_year_mismatch_blocks_merge():
    by_origin = {
        "a.md": ["- Smith, J. (2015). The same exact title here as below appears."],
        "b.md": ["- Smith, J. (2022). The same exact title here as below appears."],
    }
    clusters = cluster_entries(by_origin, threshold=0.92)
    t("Year mismatch (7yrs) blocks merge", len(clusters) == 2)


# --- BIBTEX KEY ---

def test_bibtex_key_no_collision():
    counter = {}
    e = "Smith, J. (2020). Paper one."
    k1 = to_bibtex_entry(e, counter)
    k2 = to_bibtex_entry(e, counter)
    t("Second same-key entry gets suffix",
      k1.split("{")[1].split(",")[0] != k2.split("{")[1].split(",")[0])


# --- DOI NORMALIZATION ---

def test_doi_normalization():
    t("strip https://doi.org/",
      _normalize_doi("https://doi.org/10.1111/abc") == "10.1111/abc")
    t("strip http://dx.doi.org/",
      _normalize_doi("http://dx.doi.org/10.1111/abc") == "10.1111/abc")
    t("strip 'doi:' prefix",
      _normalize_doi("doi: 10.1111/abc") == "10.1111/abc")
    t("lowercase",
      _normalize_doi("10.1111/ABC") == "10.1111/abc")
    t("empty",
      _normalize_doi(None) == "")


# --- COMPARE AGAINST BIB (no false positives from section prose) ---

def test_compare_against_bib_ignores_section_prose():
    canonical = [
        {"title": "Monetary policy in emerging markets",
         "doi": "https://doi.org/10.1111/foo",
         "cited_by": 100, "year": 2020, "authors": ["Smith"], "source": "openalex"},
    ]
    bib_text = """\
# Section 1: Background
This section discusses monetary policy in emerging markets at length.
The story of monetary policy in emerging markets begins in 1973.

# Bibliography
- Jones, K. (2020). Different paper entirely. doi:10.1111/bar
"""
    present, missing = compare_against_bib(canonical, bib_text)
    t("Canonical work absent from bibliography correctly flagged MISSING",
      len(missing) == 1 and len(present) == 0)


if __name__ == "__main__":
    print("Running parser tests…")
    test_inline_cite_formats()
    test_first_surname()
    test_export_parse_bib_skips_prose()
    test_classify_parse_bib_skips_prose()
    test_classify_tiers()
    test_classify_tiers_scholarly_venues()
    test_bibliography_parser_handles_master_heading_and_subcategories()
    test_dedup_does_not_overmerge_distinct_papers()
    test_dedup_merges_same_doi()
    test_dedup_year_mismatch_blocks_merge()
    test_bibtex_key_no_collision()
    test_doi_normalization()
    test_compare_against_bib_ignores_section_prose()
    print("\nAll parser tests passed.")
