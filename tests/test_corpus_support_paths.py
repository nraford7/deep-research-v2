"""background.corpus_support — path resolution + highlights. OFFLINE, pure text.

Production rows do NOT carry a run_dir key, so a relative text_path must resolve
against the round1_dir the caller passes, NOT against CWD. And a row's
"highlights" list (extracted snippet strings on retrieval rows) must be read as
row text. Both are covered here without touching the network.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import background

_TEXT = "Widget manufacturing relies on precision mechanical tooling and steel."


def test_text_path_resolves_against_passed_round1_dir(tmp_path):
    # A production-shape row: relative text_path, NO run_dir key. The read must be
    # anchored on the round1_dir argument, not CWD.
    round1 = tmp_path / "round1"
    (round1 / "sources").mkdir(parents=True)
    (round1 / "sources" / "x.txt").write_text(_TEXT, encoding="utf-8")
    rows = [{"title": "row", "text_path": "sources/x.txt"}]
    hit = background.corpus_support(
        "widget manufacturing uses mechanical tooling", rows,
        round1_dir=str(round1))
    assert hit is not None
    assert hit["title"] == "row"


def test_text_path_not_read_without_round1_dir(monkeypatch, tmp_path):
    # Same relative text_path but no round1_dir and no run_dir key: the file is
    # NOT found relative to CWD, so the row carries no overlapping tokens and is
    # not returned. Guards against silent CWD resolution.
    round1 = tmp_path / "round1"
    (round1 / "sources").mkdir(parents=True)
    (round1 / "sources" / "x.txt").write_text(_TEXT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # CWD is the run dir, not round1 — path won't match
    rows = [{"title": "row", "text_path": "sources/x.txt"}]
    hit = background.corpus_support(
        "widget manufacturing uses mechanical tooling", rows)
    assert hit is None


def test_reads_highlights_list(tmp_path):
    # No text/text_path — the overlapping tokens live only in the highlights list.
    rows = [{"title": "row",
             "highlights": ["Widget manufacturing relies on mechanical tooling.",
                            "Also mentions steel."]}]
    hit = background.corpus_support(
        "widget manufacturing uses mechanical tooling", rows)
    assert hit is not None
    assert hit["title"] == "row"
