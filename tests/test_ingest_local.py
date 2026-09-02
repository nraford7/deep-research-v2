import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import ingest_local

import pytest
from scripts.helper_runtime import (
    ManagedHelperRequired, broker_managed_context, resolve_helper_layout)
from scripts.run_transactions import create_skeleton_transaction


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


def test_binary_content_in_txt_is_extraction_failure(tmp_path):
    # S5: binary bytes under a .txt suffix (NUL bytes / replacement-char
    # noise) must NOT mint a resolvable KB handle — extraction failure.
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "fake.txt"
    doc.write_bytes(b"\x89PNG\x00\x01binary")
    assert _run(run, doc) == 3
    assert _rows(run) == []


def test_new_explicit_slug_retires_auto_slug_row(tmp_path):
    # C1: one document = one row — re-ingesting the same file under an
    # explicit slug must retire the earlier auto-slug row for that url.
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "doc.txt"
    doc.write_text("stable content")
    assert _run(run, doc) == 0
    assert _run(run, "--slug", "new-handle", doc) == 0
    rows = _rows(run)
    assert len(rows) == 1 and rows[0]["kb_slug"] == "new-handle"


def test_foreign_jsonl_lines_preserved_verbatim(tmp_path):
    # FE5: rewriting slice_local.jsonl must never silently drop lines it
    # cannot parse — they are preserved verbatim; new rows still append.
    run = _mk_legacy_run(tmp_path)
    a = tmp_path / "a.txt"; a.write_text("doc a")
    assert _run(run, a) == 0
    jsonl = run / "round1" / "slice_local.jsonl"
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("not-json{\n")
    b = tmp_path / "b.txt"; b.write_text("doc b")
    assert _run(run, b) == 0
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert "not-json{" in lines
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert {r["kb_slug"] for r in parsed} == {"a", "b"}


def test_year_out_of_range_is_usage_error(tmp_path):
    # FE10: KB_CITE_RE only matches 4-digit years — reject the rest up front.
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "d.txt"; doc.write_text("x")
    assert _run(run, "--year", "24", doc) == 2
    assert _rows(run) == []


def test_unsupported_format_message_lists_allowlist(tmp_path, capsys):
    # C5: the usage error must name the ACTUAL allowlist.
    run = _mk_legacy_run(tmp_path)
    doc = tmp_path / "image.png"
    doc.write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
    assert _run(run, doc) == 2
    err = capsys.readouterr().err
    for fmt in ("pdf", "html", "htm", "txt", "md", "markdown", "text"):
        assert fmt in err, fmt


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


def _mk_v2_run(tmp_path):
    # Exactly how tests/test_helper_layouts.py's `v2_run` fixture builds one.
    library = tmp_path / "research"
    library.mkdir()
    return create_skeleton_transaction(library, "topic", question="Q").publish()


def test_direct_v2_execution_is_rejected(tmp_path):
    v2_run = _mk_v2_run(tmp_path)
    doc = tmp_path / "kb.txt"
    doc.write_text("content")
    with pytest.raises(ManagedHelperRequired, match="run_manager invoke-helper"):
        ingest_local.main(["--run-dir", str(v2_run), str(doc)])


def test_helper_is_registered():
    from scripts.run_manager import MANAGED_HELPERS
    module, fn, allowed = MANAGED_HELPERS["ingest-local"]
    assert (module, fn) == ("scripts.managed_helpers", "managed_ingest_local")
    assert allowed == frozenset({"files", "title", "slug", "year"})


def test_managed_adapter_ingests_v2_run(tmp_path):
    # Broker path end-to-end: inside broker_managed_context the V2 guard
    # passes and the row lands with the V2 text_path convention.
    # (broker_managed_context wraps the call here, mirroring how
    # tests/test_helper_layouts.py exercises scope.managed_scope — _guard's
    # require_managed_mutation check runs before the adapter's own context.)
    from scripts.managed_helpers import managed_ingest_local
    v2_run = _mk_v2_run(tmp_path)
    layout = resolve_helper_layout(v2_run)
    doc = tmp_path / "kb.txt"
    doc.write_text("the fee is 12,500")
    with broker_managed_context():
        result = managed_ingest_local(layout=layout, fs=None,
                                      typed_args={"files": [str(doc)]})
    assert result["exit_code"] == 0
    jsonl = layout.round1 / "slice_local.jsonl"
    (row,) = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    assert row["text_path"].startswith("Sources/Extracted/")
    assert (layout.run_root / row["text_path"]).is_file()
