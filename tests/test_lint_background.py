"""lint_background — OFFLINE, pure text. Exercises the quantity linter over
canonically-rendered editorial background blocks.

POSITIVE: a clean definitional block passes (no violations / exit 0).
NEGATIVE: one assertion each that a block is rejected for an Arabic digit, a "$"
figure, the word "percent", a fraction word ("roughly a third"), a word-form
decade ("the early nineteen-seventies"), and an ordinal word ("the third wave").

Both a function-API path (render -> find_background_blocks -> scan_block) and a
subprocess path (the CLI's exit code) are checked, so the convention is proven
enforceable however it is invoked.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import background
from scripts import lint_background


def _violations_in(text: str):
    """Function-API lint: render the text as a background block, extract it, and
    scan for quantity triggers. Returns the flat list of (label, match) hits."""
    md = background.render_background(text)
    hits = []
    for block in background.find_background_blocks(md):
        hits.extend(lint_background.scan_block(block))
    return hits


# --- POSITIVE ---------------------------------------------------------------

def test_clean_definitional_block_passes():
    hits = _violations_in("A widget is a small mechanical device.")
    assert hits == []


def test_clean_block_passes_via_cli(tmp_path):
    md = tmp_path / "clean.md"
    md.write_text(background.render_background("A widget is a small mechanical device."),
                  encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint_background.py"), str(md)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- NEGATIVE: one assertion per quantity form ------------------------------

@pytest.mark.parametrize("text", [
    "Adoption rose 8% last year.",            # Arabic digit (and % sign)
    "It cost roughly $5 to make.",            # currency sign
    "Adoption rose eight percent last year.", # the word percent
    "Roughly a third of firms adopted it.",   # fraction word
    "This began in the early nineteen-seventies.",  # word-form decade
    "The third wave changed everything.",     # ordinal word
    "Twenty firms adopted the standard.",     # tens cardinal (>twelve)
    "It reached the nineteenth revision.",    # ordinal >twelfth
    "Several studies report the effect.",     # vague quantifier
    "Most practitioners now agree.",          # vague quantifier
])
def test_quantity_block_is_rejected(text):
    hits = _violations_in(text)
    assert hits, f"expected a quantity violation for: {text!r}"


def test_arabic_digit_rejected_via_cli(tmp_path):
    md = tmp_path / "dirty.md"
    md.write_text(background.render_background("Adoption rose 8% last year."),
                  encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint_background.py"), str(md)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "VIOLATION" in proc.stdout


# --- CITED SUBSTANCE PASSES (uncited-only rule) -----------------------------

def test_cited_sentence_passes():
    """A fenced sentence carrying a citation marker ([Author, Year]) is licensed:
    its quantities — including the citation's own year — are NOT flagged."""
    # Citation only, no other quantity — the year 1991 must not trip the digit rule.
    assert _violations_in("Mind is unified, per [Dennett 1991].") == []
    # A real cited quantity inside the fence passes.
    assert _violations_in(
        "Consciousness splits into two streams [James, 1890].") == []


def test_uncited_number_still_fails_even_beside_a_cited_sentence():
    """GUARDRAIL INVARIANT: a citation licenses only ITS OWN sentence. An uncited
    quantity in a citation-free sentence of the same block STILL fails — a cited
    sibling never launders the block."""
    # Cited sentence alone → clean.
    assert _violations_in("Two streams merged [James, 1890].") == []
    # Uncited-number sentence alone → flagged.
    assert _violations_in("Then 47 rivals rejected it.")
    # Both in one block → the uncited 47 is still caught.
    hits = _violations_in(
        "Two streams merged [James, 1890]. Then 47 rivals rejected it.")
    assert hits, "uncited number in a citation-free sentence must still fail"


def test_bare_year_without_citation_still_fails():
    """A bare year with no bracketed/parenthesised citation is NOT licensed."""
    assert _violations_in("Adoption collapsed in 2019.")


def test_bracketed_number_without_author_is_not_a_citation():
    """A bracketed token with a 4-digit year but NO author text (interval, array,
    bare year) must NOT spoof a citation and license a sibling uncited quantity."""
    assert _violations_in("The array holds [1, 2, 2019] items and 500 uncited rows.")
    assert _violations_in("The set [1900] of items numbered 42.")
    assert _violations_in("We counted 88 [2050] cases.")


# --- MALFORMED FENCE: an unbalanced marker must FAIL, not slip through ------

def test_unclosed_fence_fails_via_cli(tmp_path):
    """An OPEN marker with no CLOSE yields zero balanced blocks, so a block-only
    scan would exit clean. The fence-balance check must catch it: the quantity
    "rose 8%" inside the malformed fence must FAIL the lint, not pass."""
    md = tmp_path / "malformed.md"
    md.write_text(
        background.BACKGROUND_OPEN + "\nAdoption rose 8% with no closing marker.\n",
        encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint_background.py"), str(md)],
        capture_output=True, text=True)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "VIOLATION" in proc.stdout
    assert str(md) in proc.stdout


def test_unclosed_fence_flagged_by_helper():
    """has_unbalanced_fences reports the imbalance directly (open with no close,
    close with no open), while a properly paired fence is balanced."""
    text = background.BACKGROUND_OPEN + "\nAdoption rose 8%.\n"
    assert background.has_unbalanced_fences(text) is True
    close_only = background.BACKGROUND_CLOSE + "\nstray close.\n"
    assert background.has_unbalanced_fences(close_only) is True
    balanced = background.render_background("A widget is a small mechanical device.")
    assert background.has_unbalanced_fences(balanced) is False


def test_specific_trigger_labels():
    """Nail the exact trigger each form fires, so the linter's taxonomy is pinned."""
    def labels(text):
        return {label for label, _ in _violations_in(text)}
    assert "arabic digit" in labels("rose 8%")
    assert "currency sign" in labels("cost $5")
    assert "the word percent" in labels("eight percent growth")
    assert "fraction word" in labels("roughly a third of firms")
    assert "word-form decade" in labels("the early nineteen-seventies")
    assert "spelled-out ordinal" in labels("the third wave")
    assert "spelled-out cardinal" in labels("twenty firms adopted it")
    assert "vague quantifier" in labels("several studies agree")


def test_abbreviation_dots_do_not_split_a_cited_sentence():
    """An abbreviation dot ("ca.", "inv.", "p.", "cf.", "e.g.") is not a sentence
    boundary. Splitting there orphaned a cited quantity into a citation-free fragment
    and produced a false violation (skeletons-japanese-art run, 2026-09-01)."""
    assert _violations_in(
        "It sold for $300 in 1893 per inv. records [Smith 2001].") == []
    assert _violations_in(
        "The panel is dated ca. 1720 by the museum, see p. 4 [Met 2019].") == []
    assert _violations_in(
        "Some 45 sheets survive, cf. the census of inv. nos. 12-57 (Tinsley, 2017).") == []
    # An initial before a surname is not a boundary either.
    assert _violations_in("The 1893 sale to J. Pierpont Morgan is documented [Smith 2001].") == []
    # Real boundaries still split: the uncited number in the second sentence must fail.
    assert _violations_in("Dated ca. 1720 [Met 2019]. Then 47 rivals rejected it.")


def test_sentence_ending_abbreviation_still_splits_before_a_capitalised_sentence():
    """'etc.' / 'et al.' end sentences as often as not: a capitalised next word is a
    real boundary there, so the uncited 47 must not ride the next sentence's citation."""
    assert _violations_in(
        "There were 47 prints, drawings, etc. The attribution is accepted [Smith 2001].")
    assert _violations_in(
        "Some 47 sheets were catalogued by Kanda et al. The census is accepted [Kanda 2015].")
    # ...but the same abbreviations followed by a lower-case or numeric continuation stay joined
    assert _violations_in("Kanda et al. counted 47 sheets [Kanda 2015].") == []


def test_dotted_initialisms_join_only_before_a_continuation():
    """'U.S.', 'Ph.D.', 'Inc.' and initials end sentences as often as they introduce
    the next word, so they join only before lower-case, a digit, or citation
    punctuation. The price is a residual false positive ('47 works in U.S. Census
    records [X]'), accepted so that 'in the U.S. The ... [X]' cannot launder 47."""
    assert _violations_in("The 47 works appear in U.S. and Japanese collections [Smith 2001].") == []
    assert _violations_in("Sold by Acme Inc. in 1893 for 47 dollars [Smith 2001].") == []
    # a real boundary after an ambiguous abbreviation must still be caught
    assert _violations_in("There were 47 branches in the U.S. The conclusion is documented [Smith 2001].")
    assert _violations_in("Some 47 lots were sold by Acme Inc. The sale is documented [Smith 2001].")
    # an initial before a capitalised surname is one sentence
    assert _violations_in("The 1893 sale to J. Pierpont Morgan is documented [Smith 2001].") == []


def test_sentence_ending_abbreviation_joins_before_citation_punctuation():
    assert _violations_in("Some 47 sheets were catalogued by Kanda et al. [Kanda 2015].") == []
    assert _violations_in("Some 47 sheets were catalogued by Kanda et al. (Kanda, 2015).") == []


def test_closing_quote_or_bracket_after_terminal_punctuation_is_a_boundary():
    assert _violations_in(
        "\u201cThere were 47 works.\u201d The attribution is accepted [Smith 2001].")
    assert _violations_in(
        "(There were 47 works.) The attribution is accepted [Smith 2001].")
    # Markdown emphasis and nested closers are boundaries too
    assert _violations_in("*There were 47 works.* The attribution is accepted [Smith 2001].")
    assert _violations_in("**There were 47 works.** The attribution is accepted [Smith 2001].")
    assert _violations_in(
        "(The catalogue says \u201cThere were 47 works.\u201d) The attribution is accepted [Smith 2001].")


def test_question_or_exclamation_before_lower_case_is_still_a_boundary():
    assert _violations_in("Were there 47? yes, according to [Smith 2001].")
    assert _violations_in("There were 47! so the record says [Smith 2001].")


def test_opening_quote_after_ambiguous_abbreviation_is_a_boundary():
    assert _violations_in(
        "There were 47 branches in the U.S. \u201cThe count is documented [Smith 2001],\u201d the report says.")
    # but a citation marker directly after it continues the sentence
    assert _violations_in("Some 47 sheets were catalogued by Kanda et al. [Kanda 2015].") == []


def test_name_suffixes_are_ambiguous_not_introducers():
    assert _violations_in("There were 47 contributors led by Smith Jr. The roster is documented [Jones 2001].")
    assert _violations_in("Smith Jr. and 47 others signed it [Jones 2001].") == []


def test_backticks_and_deep_closers_end_sentences():
    assert _violations_in("`There were 47.` The count is documented [Smith 2001].")
    assert _violations_in(
        "(The catalogue says **\u201cThere were 47.\u201d**) The attribution is documented [Smith 2001].")


def test_unicode_lower_case_continuation_and_plate_abbreviations():
    assert _violations_in("Some 45 sheets circulated in the U.S. \u00e9migr\u00e9 collections [Smith 2001].") == []
    assert _violations_in("Some 45 sheets are reproduced in pl. 4 [Smith 2001].") == []
    assert _violations_in("Some 45 sheets are discussed at n. 12 [Smith 2001].") == []


def test_ordinary_words_that_look_like_abbreviations_still_end_sentences():
    assert _violations_in("There are 47 recognized forms of art. The taxonomy is documented [Smith 2001].")
    assert _violations_in("Some 47 patients became ill. The outcome is documented [Smith 2001].")
    assert _violations_in("The answer given by 47 respondents was no. The survey is documented [Smith 2001].")


def test_digit_or_citation_start_after_ambiguous_abbreviation_is_a_sentence():
    assert _violations_in("There were 47 branches in the U.S. 2020 figures are documented [Smith 2021].")
    # a citation marker directly after 'et al.' is the common in-sentence shape and still joins
    assert _violations_in("Some 47 sheets were catalogued by Kanda et al. [Kanda 2015].") == []


def test_abbreviation_wrapped_in_markdown_closers_still_joins():
    assert _violations_in("The 47 works appear in *fig.* 2 [Smith 2001].") == []
    assert _violations_in("The 1893 sale to *J.* Pierpont Morgan is documented [Smith 2001].") == []


def test_strikethrough_and_cjk_closers_end_sentences():
    assert _violations_in("~~There were 47 works.~~ The attribution is documented [Smith 2001].")
    assert _violations_in("There were 47 works.\u300d The attribution is documented [Smith 2001].")


def test_month_abbreviations_introduce_dates_but_can_end_sentences():
    assert _violations_in("The work sold for $300 on Mar. 3 [Smith 2020].") == []
    assert _violations_in("Some 47 lots were sold in Jan. 1893 [Smith 2020].") == []
    assert _violations_in("There were 47 incidents in Jan. The trend is documented [Smith 2020].")


def test_eg_and_ie_introduce_whatever_follows():
    assert _violations_in("There were 47 candidate models, e.g. GPT-4 and Claude [Smith 2020].") == []
    assert _violations_in("Some 47 sheets are copies, i.e. Edo-period versions [Smith 2020].") == []


def test_unicode_and_hyphenated_initials_join():
    assert _violations_in("The 1893 sale to \u00c9. Zola is documented [Smith 2020].") == []
    assert _violations_in("The 1893 sale to J.-P. Morgan is documented [Smith 2020].") == []


def test_guillemets_close_sentences_and_markdown_wrapped_continuation_joins():
    assert _violations_in("\u00abThere were 47 works.\u00bb The attribution is documented [Smith 2020].")
    assert _violations_in("The 47 works appear in U.S. *and* Japanese collections [Smith 2020].") == []


def test_lowercase_styled_sentence_does_not_license_previous_quantity():
    """A legitimate lowercase-styled sentence start is still a new sentence."""
    assert _violations_in("There were 47 samples. pH was measured [Smith 2001].")


def test_grade_letter_does_not_turn_the_next_sentence_into_a_name():
    """A capital before a full stop is not automatically a person's initial."""
    assert _violations_in(
        "Some 47 samples were graded A. Then the scale was documented [Smith 2001].")


def test_sentence_final_introducer_does_not_join_a_capitalised_sentence():
    """An introducing abbreviation only joins when what follows fits its role."""
    assert _violations_in(
        "The estimate was 47, approx. This method is documented [Smith 2001].")


@pytest.mark.parametrize("text", [
    "The 47 plates appear in vol. II [Smith 2001].",
    "The 47 plates appear in fig. IV [Smith 2001].",
    "The 47 plates appear on p. A12 [Smith 2001].",
    "The 47 plates are cited in ed. Jones [Smith 2001].",
])
def test_uppercase_bibliographic_continuations_stay_in_the_cited_sentence(text):
    assert _violations_in(text) == []


@pytest.mark.parametrize("text", [
    "The 47 plates were catalogued by Charles J. Smith [Smith 2001].",
    "The 47 plates were catalogued by A. B. Smith [Smith 2001].",
    "The 47 plates were catalogued by John J.-P. Smith [Smith 2001].",
])
def test_middle_and_multi_initial_names_stay_in_the_cited_sentence(text):
    assert _violations_in(text) == []


def test_terminal_place_abbreviation_does_not_license_previous_quantity():
    assert _violations_in(
        "The 47 objects were stored on Main St. The inventory is documented [Smith 2001].")


def test_place_abbreviation_still_introduces_a_place_name():
    assert _violations_in(
        "The 47 objects were stored in St. Louis [Smith 2001].") == []
    assert _violations_in(
        "The 47 objects were found on Mt. Fuji [Smith 2001].") == []
