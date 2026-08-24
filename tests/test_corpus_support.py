"""background.corpus_support — OFFLINE, pure text. The $0 token-overlap scorer
returns a row for a claim the corpus supports (overlapping tokens) and None for
an unrelated claim.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import background


def _rows():
    return [
        {"title": "Widget manufacturing costs",
         "text": "Widget manufacturing relies on precision mechanical tooling and steel."},
        {"title": "Ocean tides",
         "text": "The lunar cycle drives coastal tides across the pacific basin."},
    ]


def test_supported_claim_returns_matching_row():
    hit = background.corpus_support(
        "widget manufacturing uses mechanical tooling", _rows())
    assert hit is not None
    assert hit["title"] == "Widget manufacturing costs"
    assert hit["_support_score"] >= 0.3


def test_unrelated_claim_returns_none():
    hit = background.corpus_support(
        "quarterly interest rate policy at central banks", _rows())
    assert hit is None


def test_empty_claim_returns_none():
    assert background.corpus_support("", _rows()) is None


def test_reads_text_path_relative_to_run_dir(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "s1.txt").write_text(
        "Widget manufacturing relies on precision mechanical tooling and steel.",
        encoding="utf-8")
    rows = [{"title": "row", "text_path": "sources/s1.txt", "run_dir": str(tmp_path)}]
    hit = background.corpus_support(
        "widget manufacturing uses mechanical tooling", rows)
    assert hit is not None
