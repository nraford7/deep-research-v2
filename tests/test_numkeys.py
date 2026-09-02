# tests/test_numkeys.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import numkeys


def _keys(sentence):
    return [(c["key"], c["unit"]) for c in numkeys.extract_claims(sentence)]


def test_plain_number_with_thousands_separator():
    assert _keys("about 175,000 dollars changed hands") == [("175000", "currency")]

def test_percent_claim():
    assert _keys("only 2.2% of receipts") == [("2.2", "percent")]

def test_currency_with_scale_suffix():
    assert _keys("capped at US$175k per year") == [("175000", "currency")]

def test_scale_word_million():
    assert _keys("about 5.4 million samples") == [("5400000", None)]

def test_bare_count():
    assert _keys("roughly 930 commercial SMTAs") == [("930", None)]

def test_bare_year_in_prose_is_a_claim():
    # Years are excluded ONLY inside citation markers / metadata / section
    # numbering / footnotes (via strip_nonclaims) — a factual date in prose
    # is a checkable claim like any other number.
    assert _keys("the treaty entered into force in 2004.") == [("2004", None)]

def test_year_inside_citation_is_not_a_claim():
    assert _keys(numkeys.strip_nonclaims("receipts fell [Tvedt, 2015].")) == []

def test_currency_suffix_word():
    # Plan's original used _keys()[0] (a (key, unit) tuple) but compared it to
    # a full claim dict and then indexed it with ["unit"] — a TypeError.
    # Internally inconsistent, so check the claim dict directly instead.
    claim = numkeys.extract_claims("cost 175,000 USD upfront")[0]
    assert claim == {"raw": "175,000 USD", "key": "175000", "unit": "currency"} or \
           claim["unit"] == "currency"

def test_section_numbering_is_stripped():
    assert _keys(numkeys.strip_nonclaims("See Section 2.5 and Table 3 for detail.")) == []

def test_search_keys_include_scale_alternates():
    (c,) = numkeys.extract_claims("no more than US$175k annually")
    keys = numkeys.search_keys(c)
    assert "175000" in keys and any(k.startswith("175") and k != "175000" for k in keys)

def test_citation_markers_are_stripped():
    s = "receipts fell [Tvedt, 2015] to 930 units [FAO, 2019]."
    assert _keys(numkeys.strip_nonclaims(s)) == [("930", None)]

def test_metadata_tags_are_stripped():
    s = "figures current [as of: 2026-09-02] show 41 states"
    assert _keys(numkeys.strip_nonclaims(s)) == [("41", None)]

def test_markdown_link_url_stripped_label_kept():
    # FE2: the (url) part is dropped (its /2019/ id is not a claim source),
    # but the visible LABEL survives — its year IS a checkable claim.
    s = "see [the 2019 report](https://fao.org/2019/x) counting 41 states"
    assert _keys(numkeys.strip_nonclaims(s)) == [("2019", None), ("41", None)]


def test_markdown_link_label_number_is_a_claim():
    # FE2 regression: "[41 states](https://x.org)" is VISIBLE text — the 41
    # must survive strip_nonclaims as a claim.
    s = "[41 states](https://x.org) have ratified"
    assert _keys(numkeys.strip_nonclaims(s)) == [("41", None)]


def test_plain_bracketed_prose_is_kept():
    # FE2 regression: a bracketed span that is neither citation-ish nor
    # metadata is visible prose — its numbers are claims.
    s = "revenue [rose 40% overall] last year"
    assert _keys(numkeys.strip_nonclaims(s)) == [("40", "percent")]


def test_footnote_marker_stripped():
    assert _keys(numkeys.strip_nonclaims("receipts fell[^3] sharply")) == []


def test_unverified_metadata_bracket_stripped():
    s = "[UNVERIFIED — not in retrieved evidence] Receipts hit 930 units."
    assert _keys(numkeys.strip_nonclaims(s)) == [("930", None)]


def test_supported_exact():
    hay = numkeys.normalize_haystack("The fund collected US$119,083 from Nunhems.")
    (c,) = numkeys.extract_claims("a single payment of US$119,083")
    assert numkeys.claim_supported(c, hay)

def test_boundary_930_does_not_match_1930():
    hay = numkeys.normalize_haystack("Founded in 1930, the institute...")
    (c,) = numkeys.extract_claims("roughly 930 commercial SMTAs")
    assert not numkeys.claim_supported(c, hay)

def test_boundary_930_does_not_match_9300():
    hay = numkeys.normalize_haystack("over 9300 accessions")
    (c,) = numkeys.extract_claims("roughly 930 commercial SMTAs")
    assert not numkeys.claim_supported(c, hay)

def test_percent_claim_requires_percent_context():
    (c,) = numkeys.extract_claims("only 2.2% of receipts")
    assert numkeys.claim_supported(c, numkeys.normalize_haystack("about 2.2 percent of receipts"))
    assert numkeys.claim_supported(c, numkeys.normalize_haystack("about 2.2% of receipts"))
    assert not numkeys.claim_supported(c, numkeys.normalize_haystack("version 2.2 of the SMTA"))

def test_currency_claim_requires_currency_context():
    (c,) = numkeys.extract_claims("no more than US$175k annually")
    assert numkeys.claim_supported(c, numkeys.normalize_haystack("capped at $175,000 a year"))
    assert not numkeys.claim_supported(c, numkeys.normalize_haystack("a crowd of 175000 people"))

def test_scale_match_against_expanded_form():
    (c,) = numkeys.extract_claims("about 5.4 million samples")
    assert numkeys.claim_supported(c, numkeys.normalize_haystack("some 5.4 million samples moved"))


def test_billion_decimal_precision():
    # B1 regression: 1.07 * 1e9 in float leaves artifacts
    # ("1070000000.0000001"); Decimal must give the clean key.
    (c,) = numkeys.extract_claims("$1.07 billion")
    assert c["key"] == "1070000000"
    assert numkeys.claim_supported(
        c, numkeys.normalize_haystack("revenue of $1.07 billion"))


def test_trailing_decimal_zeros_stripped():
    # B1 regression: "2.20%" must normalize to key "2.2" so it matches
    # evidence written as "2.2 percent".
    (c,) = numkeys.extract_claims("about 2.20% of receipts")
    assert c["key"] == "2.2"
    assert numkeys.claim_supported(
        c, numkeys.normalize_haystack("2.2 percent of receipts"))


def test_sentence_final_period_after_number_matches():
    # B2 regression: a sentence-final period right after the number must not
    # block the match.
    (c,) = numkeys.extract_claims("employs 175,000 staff")
    assert numkeys.claim_supported(
        c, numkeys.normalize_haystack("The company employs 175,000."))


def test_2500_matches_2_5k_alternate():
    # C4 regression: alternates gate must admit any integer >= 1000, not just
    # keys ending in "000" — "2,500" should match evidence "2.5k".
    (c,) = numkeys.extract_claims("about 2,500 users")
    assert numkeys.claim_supported(
        c, numkeys.normalize_haystack("we host 2.5k users worldwide"))


def test_short_m_suffix_alternate():
    # C4: short suffix forms (m/bn/b) join the alternates.
    (c,) = numkeys.extract_claims("about 5.4 million samples")
    assert numkeys.claim_supported(
        c, numkeys.normalize_haystack("holdings of 5.4m samples"))


def test_letter_suffix_alternate_does_not_match_inside_unit():
    # C4 regression: the "175k" alternate must NOT match inside "175kg".
    (c,) = numkeys.extract_claims("shipped 175k units")
    assert not numkeys.claim_supported(
        c, numkeys.normalize_haystack("the crate weighs 175kg fully loaded"))
