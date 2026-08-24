"""field-map grounding CONVENTION mechanics — OFFLINE, pure text.

SKILL.md prose is not executable, so this tests the machine-checkable half of the
"cited-or-fenced" convention: a structural claim the writer adds WITHOUT a
retrieved citation must sit inside the editorial:background fence. We prove the
mark is both (a) detectable — find_background_blocks recovers the fenced claim
out of a realistic markdown sample — and (b) enforceable — the linter passes when
the fenced content is purely definitional (no invented quantity), which is the
whole point of allowing an uncited-but-fenced synthesis line.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import background
from scripts import lint_background


DEFINITIONAL = ("A field map is an orienting synthesis of a research area's "
                "main strands, not an independently retrieved finding.")


def _sample_markdown():
    """A small Bible-shaped section: a cited paragraph, then an uncited structural
    claim placed inside the editorial:background fence via the canonical renderer."""
    return "\n".join([
        "## The field",
        "",
        "Adoption has been studied widely [1].",
        "",
        background.render_background(DEFINITIONAL),
        "",
        "The rest of the section proceeds from there [2].",
    ])


def test_fenced_claim_is_recoverable():
    md = _sample_markdown()
    blocks = background.find_background_blocks(md)
    assert len(blocks) == 1
    # the uncited structural claim is recovered verbatim from inside the fence
    assert blocks[0] == DEFINITIONAL


def test_only_fenced_content_is_recovered_not_cited_prose():
    md = _sample_markdown()
    blocks = background.find_background_blocks(md)
    # the surrounding cited prose is NOT inside any block
    assert "Adoption has been studied widely" not in blocks[0]
    assert "proceeds from there" not in blocks[0]


def test_definitional_fence_passes_lint():
    """A purely definitional fenced claim carries no quantity, so it lints clean —
    proving an uncited-but-fenced synthesis line is a permitted, enforceable state."""
    md = _sample_markdown()
    hits = []
    for block in background.find_background_blocks(md):
        hits.extend(lint_background.scan_block(block))
    assert hits == []


def test_fence_with_invented_quantity_is_caught():
    """The same convention rejects an uncited fenced claim that smuggles in a
    number — the marking makes the stricter rule enforceable."""
    md = "\n".join([
        "## The field",
        background.render_background(
            "The field is dominated by three competing schools of thought."),
    ])
    hits = []
    for block in background.find_background_blocks(md):
        hits.extend(lint_background.scan_block(block))
    assert hits, "an invented quantity inside the fence must be caught"
