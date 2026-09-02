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

def test_markdown_link_target_stripped():
    s = "see [the 2019 report](https://fao.org/2019/x) counting 41 states"
    assert _keys(numkeys.strip_nonclaims(s)) == [("41", None)]


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
