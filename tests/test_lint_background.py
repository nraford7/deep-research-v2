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
