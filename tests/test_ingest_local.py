import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import ingest_local


def _run(run_dir, *args):
    return ingest_local.main(["--run-dir", str(run_dir), *map(str, args)])


def _rows(run_dir):
    p = run_dir / "round1" / "slice_local.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _mk_legacy_run(tmp_path):
    run = tmp_path / "run"
    (run / "round1").mkdir(parents=True)
    return run


def test_ingest_txt_writes_row_and_text(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "Client Contract v2.txt"
    doc.write_text("The gross fee is US$400,000; net after costs is US$310,000.")
    assert _run(run, doc) == 0
    (row,) = _rows(run)
    assert row["kb_slug"] == "client-contract-v2"
    assert row["origin"] == "user-provided"
    assert row["tier"] == "user-provided"
    assert row["slice"] == "local"
    assert row["url"].startswith("file://")
    assert row["text_chars"] > 0
    # Legacy convention (mirrors slice_search._spill_fulltext): text_path is
    # relative to round1/ and starts with "sources/".
    assert row["text_path"].startswith("sources/")
    text_file = run / "round1" / row["text_path"]
    assert "310,000" in text_file.read_text(encoding="utf-8")
    assert len(row["sha256"]) == 64


def test_year_flag_recorded(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "report.txt"
    doc.write_text("some content here")
    assert _run(run, "--year", "2024", doc) == 0
    (row,) = _rows(run)
    assert row["year"] == 2024


def test_empty_extraction_fails_no_row(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "blank.txt"
    doc.write_text("   \n\n  ")
    assert _run(run, doc) == 3
    assert _rows(run) == []


def test_html_is_tag_stripped(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "page.html"
    doc.write_text("<html><script>x()</script><body><p>Fee schedule: 12,500</p></body></html>")
    assert _run(run, doc) == 0
    (row,) = _rows(run)
    text = (run / "round1" / row["text_path"]).read_text(encoding="utf-8")
    assert "12,500" in text and "x()" not in text


def test_reingest_same_file_is_idempotent(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "memo.txt"
    doc.write_text("version one")
    assert _run(run, doc) == 0
    doc.write_text("version two, updated")
    assert _run(run, doc) == 0
    rows = _rows(run)
    assert len(rows) == 1
    assert "version two" in (run / "round1" / rows[0]["text_path"]).read_text(encoding="utf-8")


def test_explicit_slug_replaces_row_even_from_new_path(tmp_path):
    # Spec: idempotence is by kb_slug — a replacement document supplied under
    # the same logical slug UPDATES that KB entry.
    run = _mk_legacy_run(tmp_path)
    a = tmp_path / "contract-2023.txt"; a.write_text("old terms")
    b = tmp_path / "contract-2024.txt"; b.write_text("new terms")
    assert _run(run, "--slug", "acme-contract", a) == 0
    assert _run(run, "--slug", "acme-contract", b) == 0
    rows = _rows(run)
    assert len(rows) == 1 and rows[0]["kb_slug"] == "acme-contract"
    assert "new terms" in (run / "round1" / rows[0]["text_path"]).read_text(encoding="utf-8")


def test_invalid_explicit_slug_is_usage_error(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "d.txt"; doc.write_text("x")
    assert _run(run, "--slug", "Bad_Slug!", doc) == 2


def test_unsupported_binary_format_is_usage_error(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "image.png"
    doc.write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
    assert _run(run, doc) == 2
    assert _rows(run) == []


def test_pdf_routing(tmp_path, monkeypatch):
    # pypdf output varies by fixture PDF; assert ROUTING, not extraction.
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 fake body")
    monkeypatch.setattr(ingest_local, "_pdf_to_text", lambda data: "pdf text 42")
    assert _run(run, doc) == 0
    (row,) = _rows(run)
    assert "pdf text 42" in (run / "round1" / row["text_path"]).read_text(encoding="utf-8")


def test_slug_collision_gets_suffix(tmp_path):
    run = _mk_legacy_run(tmp_path)
    a = tmp_path / "a" / "brief.txt"; a.parent.mkdir(); a.write_text("doc a")
    b = tmp_path / "b" / "brief.txt"; b.parent.mkdir(); b.write_text("doc b")
    assert _run(run, a) == 0
    assert _run(run, b) == 0
    slugs = {r["kb_slug"] for r in _rows(run)}
    assert len(slugs) == 2 and "brief" in slugs


def test_evidence_gate_accepts_local_rows(tmp_path):
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "kb.txt"
    doc.write_text("real content")
    assert _run(run, doc) == 0
    from scripts import evidence_gate
    rows, malformed = evidence_gate._load_slice(run / "round1" / "slice_local.jsonl")
    assert rows and not malformed
