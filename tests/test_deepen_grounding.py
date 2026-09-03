import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import deepen_questions as dq

PAYLOAD = {
    "output": {
        "content": {"answer": ("The fund collected US$119,083 in total. "
                                "Receipts reached 2.2% of projections. "
                                "The mechanism is widely considered a failure.")},
        "grounding": [{"citation": "Tvedt 2015", "url": "https://x.org/tvedt"}],
    },
    "results": [
        {"url": "https://x.org/tvedt", "title": "Tvedt",
         "highlights": ["the single payment of US$119,083 from Nunhems"]},
    ],
}


def test_collect_evidence_gathers_highlights_and_grounding_text():
    ev = dq._collect_evidence(PAYLOAD)
    snippets = [s for e in ev for s in e["snippets"]]
    assert any("119,083" in s for s in snippets)
    assert any("Tvedt 2015" in s for s in snippets)


def test_mark_unverified_flags_unsupported_marks_nothing_supported():
    evidence = "the single payment of US$119,083 from Nunhems"
    marked = dq._mark_unverified(
        "The fund collected US$119,083 in total. Receipts reached 2.2% of "
        "projections. The mechanism is widely considered a failure.", evidence)
    assert "[UNVERIFIED — not in retrieved evidence] Receipts reached 2.2%" in marked
    assert "UNVERIFIED — not in retrieved evidence] The fund" not in marked
    assert "widely considered a failure." in marked
    assert "UNVERIFIED — not in retrieved evidence] The mechanism" not in marked


def test_mark_unverified_empty_evidence_marks_all_quantity_sentences():
    marked = dq._mark_unverified("About 930 SMTAs exist. Prose only here.", "")
    assert marked.count("[UNVERIFIED") == 1
    assert "Prose only here." in marked


def test_mixed_sentence_with_one_fabricated_number_is_marked():
    # One supported + one fabricated number in the SAME sentence → marked.
    evidence = "the single payment of US$119,083 from Nunhems"
    marked = dq._mark_unverified(
        "The fund received US$119,083 across 930 commercial SMTAs.", evidence)
    assert marked.startswith(dq.UNVERIFIED_MARK)


def test_write_answer_carries_evidence_section(tmp_path):
    path = tmp_path / "answer_00_gap.md"
    dq._write_answer(path, "q?", "answer text", [("https://x.org", "X")],
                     [{"url": "https://x.org", "snippets": ["snippet one"]}])
    text = path.read_text(encoding="utf-8")
    assert "## Evidence" in text and "snippet one" in text
    assert "## Sources" in text


def test_write_answer_no_evidence_says_so(tmp_path):
    path = tmp_path / "a.md"
    dq._write_answer(path, "q?", "answer", [], [])
    assert "_(no evidence returned)_" in path.read_text(encoding="utf-8")


def test_grounding_urls_are_not_evidence():
    # S2/FE1 regression: a numeric id inside a grounding URL must NOT be
    # collected as evidence text (it would falsely suppress UNVERIFIED marks).
    payload = {"results": [],
               "output": {"content": {"answer": "x"},
                          "grounding": [{"url": "https://x.org/48217"}]}}
    ev = dq._collect_evidence(payload)
    snippets = [s for e in ev for s in e["snippets"]]
    assert not any("48217" in s for s in snippets)


def test_grounding_evidence_keys_still_collected():
    payload = {"results": [],
               "output": {"content": {"answer": "x"},
                          "grounding": [{"citation": "Tvedt 2015",
                                         "url": "https://x.org/tvedt"}]}}
    ev = dq._collect_evidence(payload)
    snippets = [s for e in ev for s in e["snippets"]]
    assert snippets == ["Tvedt 2015"]


def test_non_list_highlights_do_not_crash():
    # S2/FE1 regression: highlights must be type-guarded — an int payload
    # yields no evidence instead of a TypeError.
    payload = {"results": [{"url": "https://x.org/a", "highlights": 42}],
               "output": {"grounding": {"highlights": 42, "text": 7}}}
    assert dq._collect_evidence(payload) == []


def test_mark_unverified_preserves_bullet_marker():
    # S3 regression: the mark goes AFTER the list marker, before the sentence.
    marked = dq._mark_unverified("- Receipts hit 2.2% of target.", "")
    assert marked == ("- [UNVERIFIED — not in retrieved evidence] "
                      "Receipts hit 2.2% of target.")


def test_mark_unverified_numbered_ordinal_not_a_claim():
    # S3 regression: the ordinal of a numbered list item is structure, not a
    # numeric claim — the line stays unchanged.
    marked = dq._mark_unverified("2. Second point with no numbers.", "")
    assert marked == "2. Second point with no numbers."


def test_mark_unverified_keeps_nested_indentation():
    marked = dq._mark_unverified("  - Sub point cites 930 SMTAs.", "")
    assert marked == ("  - [UNVERIFIED — not in retrieved evidence] "
                      "Sub point cites 930 SMTAs.")


def test_run_question_fail_open_on_pathological_payload(tmp_path):
    # S4 regression: an unexpected exception in the post-reconcile evidence
    # processing must NOT kill the run — the answer is written with a leading
    # "[UNVERIFIED — evidence processing failed]" line and True is returned.
    pathological = {
        "output": {"content": {"answer": "Answer text."}},
        # a set is not JSON-serializable → grounding-file dump raises
        "results": [{"url": "u", "highlights": {"not", "serializable"}}],
        "costDollars": {"total": 0.02},
    }
    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return pathological
    class FakeSession:
        def post(self, *a, **k):
            return FakeResp()
    class FakeLedger:
        def __init__(self):
            self.charges, self.reconciles = [], []
        def charge(self, helper, kind, amount):
            self.charges.append((helper, kind, amount))
            return "entry"
        def reconcile(self, entry, actual):
            self.reconciles.append((entry, actual))
    ledger = FakeLedger()
    ok = dq.run_question(0, "gap", "q?", FakeSession(), "key", ledger, tmp_path)
    assert ok is True
    assert ledger.reconciles == [("entry", 0.02)]
    text = (tmp_path / "answer_00_gap.md").read_text(encoding="utf-8")
    assert "[UNVERIFIED — evidence processing failed]" in text


def test_run_question_ledger_invariants_untouched(tmp_path):
    """Lens test: exactly one charge + one reconcile per call, reconcile uses
    costDollars.total, answer + grounding files written."""
    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {**PAYLOAD, "costDollars": {"total": 0.02}}
    class FakeSession:
        def post(self, *a, **k):
            return FakeResp()
    class FakeLedger:
        def __init__(self):
            self.charges, self.reconciles = [], []
        def charge(self, helper, kind, amount):
            self.charges.append((helper, kind, amount))
            return "entry"
        def reconcile(self, entry, actual):
            self.reconciles.append((entry, actual))
    ledger = FakeLedger()
    ok = dq.run_question(0, "gap", "q?", FakeSession(), "key", ledger, tmp_path)
    assert ok is True
    assert ledger.charges == [("deepen_questions", "deep_reasoning", dq.DEEPEN_WORST_CASE)]
    assert ledger.reconciles == [("entry", 0.02)]
    assert (tmp_path / "answer_00_gap.md").exists()
    assert (tmp_path / "grounding_00.json").exists()
