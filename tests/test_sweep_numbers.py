# tests/test_sweep_numbers.py
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import sweep_numbers


def _mk_run(tmp_path):
    run = tmp_path / "run"
    (run / "round1" / "sources").mkdir(parents=True)
    (run / "sections").mkdir()
    (run / "round2_5").mkdir()
    return run


SMTA_SECTION = """# The benefit-sharing record

The Governing Body reported roughly 75,000 SMTAs covering 5.4 million samples
[FAO, 2019]. The single mandatory payment received was US$119,083 [Tvedt, 2015].

Critics claim receipts reached only 2.2% of projections, capped at US$175,000
per year, with about 930 commercial SMTAs among more than 100,000 signed
[kb:smta-review, 2018].
"""

CORPUS = """The FAO Governing Body's 2019 report counts some 75,000 SMTAs,
moving 5.4 million samples. In 1930 the institute was founded. The sole
mandatory payment, US$119,083, came from Nunhems B.V.
"""


def _index_local_txt(run, name):
    """A slice_local.jsonl row the way ingest_local writes it for a legacy
    layout: text_path is round1-relative 'sources/<f>'. FE3: only indexed
    files count as evidence."""
    row = {"title": name, "url": f"file://kb/{name}", "slice": "local",
           "tier": "user-provided", "origin": "user-provided",
           "kb_slug": Path(name).stem.replace("_", "-"),
           "text_path": f"sources/{name}"}
    jsonl = run / "round1" / "slice_local.jsonl"
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_fabricated_numbers_flag_supported_pass(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "ii.md").write_text(SMTA_SECTION)
    (run / "round1" / "sources" / "local_abc.txt").write_text(CORPUS)
    _index_local_txt(run, "local_abc.txt")
    out = tmp_path / "sweep.md"
    rc = sweep_numbers.main(["--run-dir", str(run), "--output", str(out)])
    assert rc == 1
    report = json.loads(out.with_suffix(".json").read_text())
    flagged = {f["claim"]["key"] for f in report["flags"]}
    assert flagged == {"2.2", "175000", "930", "100000"}
    passed = {p["key"] for p in report["supported_keys"]}
    assert {"75000", "5400000", "119083"} <= passed


def test_highlight_only_evidence_counts(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("Enrollment hit 48,217 students [x.org, 2024].")
    row = {"url": "https://x.org/a", "tier": "web",
           "highlights": ["district enrollment of 48,217 students"], "text": ""}
    (run / "round1" / "slice_web.jsonl").write_text(json.dumps(row) + "\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0


def test_round25_evidence_counts(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("The pilot covered 3,140 farms.")
    (run / "round2_5" / "grounding_00.json").write_text(
        json.dumps({"results": [{"highlights": ["a survey of 3,140 farms"]}]}))
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0


def test_clean_run_exits_zero(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("No numbers here, just prose [Smith, 2020].")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0


def test_report_states_tripwire_limit(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("prose")
    out = tmp_path / "sweep.md"
    sweep_numbers.main(["--run-dir", str(run), "--output", str(out)])
    assert "existence check" in out.read_text().lower()


def test_answer_prose_cannot_self_support(tmp_path):
    # A fabricated number in the deepening ANSWER prose, copied into a
    # section, must still flag: only the ## Evidence block counts.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("Receipts reached 2.2% of projections.")
    (run / "round2_5" / "answer_00_gap.md").write_text(
        "# Round-2.5 answer\n\nReceipts reached 2.2% of projections.\n\n"
        "## Evidence\n\n- src\n  > no numbers in the actual evidence\n\n"
        "## Sources\n\n- x\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 1


def test_evidence_block_does_support(tmp_path):
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("The pilot covered 3,140 farms.")
    (run / "round2_5" / "answer_00_gap.md").write_text(
        "# Round-2.5 answer\n\nanswer prose\n\n"
        "## Evidence\n\n- src\n  > a survey of 3,140 farms\n\n## Sources\n\n- x\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0


def test_inherited_text_path_is_read(tmp_path):
    # Inherited rows commonly carry ONLY text_path — the spilled file must be
    # read for its numbers to count as evidence.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("Enrollment hit 48,217 students.")
    spill = run / "round1" / "sources" / "inh_abc.txt"
    spill.write_text("district enrollment of 48,217 students")
    row = {"url": "https://x.org/a", "tier": "web",
           "text_path": "sources/inh_abc.txt", "inherited_from_run_id": "r0"}
    (run / "round1" / "inherited_corpus.jsonl").write_text(json.dumps(row) + "\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0


def test_grounding_urls_do_not_support_claims(tmp_path):
    # Numeric ids inside grounding URLs are NOT evidence text.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("The study tracked 48,217 students.")
    (run / "round2_5" / "grounding_00.json").write_text(
        json.dumps({"results": [{"url": "https://x.org/report/48217",
                                  "highlights": ["no relevant numbers"]}]}))
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 1


def test_answer_evidence_url_bullets_do_not_support(tmp_path):
    # S1 regression: "- url" bullet lines inside ## Evidence are NOT evidence
    # text — a numeric id in the URL must not support a claim. Only the
    # ">"-quoted snippet lines count.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("Cuts hit 175,000 staff.")
    (run / "round2_5" / "answer_00_gap.md").write_text(
        "# Round-2.5 answer\n\nprose\n\n## Evidence\n\n"
        "- https://x.org/reports/175000-staff-cuts\n"
        "  > nothing numeric in the actual snippet\n\n## Sources\n\n- x\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 1


def test_stale_unindexed_txt_does_not_support(tmp_path):
    # FE3 regression: a spilled .txt under sources/ that NO corpus row
    # references (a stale leftover) is not evidence.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text("Enrollment hit 48,217 students.")
    (run / "round1" / "sources" / "stale_leftover.txt").write_text(
        "district enrollment of 48,217 students")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 1


def test_escaping_text_path_is_rejected(tmp_path):
    # FE4 regression: a text_path that resolves outside the run root must be
    # skipped — files beyond the run are never evidence.
    run = _mk_run(tmp_path)
    (tmp_path / "outside.txt").write_text("district enrollment of 48,217 students")
    (run / "sections" / "s.md").write_text("Enrollment hit 48,217 students.")
    row = {"url": "https://x.org/a", "tier": "web",
           "text_path": "../../outside.txt"}
    (run / "round1" / "slice_web.jsonl").write_text(json.dumps(row) + "\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 1


def test_wrapped_citation_year_not_a_claim(tmp_path):
    # C3 regression: a citation wrapped across a line break ("[Smith,\n2020]")
    # must be joined and stripped as ONE marker — its year is not a claim.
    run = _mk_run(tmp_path)
    (run / "sections" / "s.md").write_text(
        "Receipts fell sharply [Smith,\n2020] across the board.\n")
    out = tmp_path / "sweep.md"
    assert sweep_numbers.main(["--run-dir", str(run), "--output", str(out)]) == 0
