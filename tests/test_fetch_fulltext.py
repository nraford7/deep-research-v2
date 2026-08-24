"""fetch_fulltext — OFFLINE tests. The network (_safe_get) is monkeypatched;
no real HTTP, no DNS. Exercises extraction + the process_run wiring that fills
thin Round-1 rows and rewrites their jsonl in place.
"""

import io
import json

import pytest

from scripts import fetch_fulltext as ff


# --- extraction helpers -----------------------------------------------------

def test_html_to_text_drops_script_and_style():
    html = (b"<html><head><style>.x{color:red}</style></head>"
            b"<body><script>evil()</script><h1>Title</h1>"
            b"<p>Real body text here.</p></body></html>")
    text = ff._html_to_text(html)
    assert "Title" in text and "Real body text here." in text
    assert "evil()" not in text and "color:red" not in text


def test_looks_like_pdf_by_magic_content_type_and_suffix():
    assert ff._looks_like_pdf("https://x.com/a", "", b"%PDF-1.7 ...")
    assert ff._looks_like_pdf("https://x.com/a", "application/pdf", b"xx")
    assert ff._looks_like_pdf("https://x.com/paper.pdf", "text/html", b"xx")
    assert not ff._looks_like_pdf("https://x.com/page", "text/html", b"<html>")


def test_doi_from_url():
    assert ff._doi_from_url("https://doi.org/10.1234/abc.def") == "10.1234/abc.def"
    assert ff._doi_from_url("https://example.com/no-doi") is None


def test_pdf_to_text_roundtrip():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    # A blank page extracts to empty string but must not raise.
    assert ff._pdf_to_text(buf.getvalue()) == ""
    assert ff._pdf_to_text(b"not a pdf") == ""


# --- process_run wiring -----------------------------------------------------

def _write_slice(run_dir, name, rows):
    d = run_dir / "round1"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"slice_{name}.jsonl"
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def test_thin_row_filled_fat_row_untouched(monkeypatch, tmp_path):
    thin = {"url": "https://a.com/thin", "slice": "web", "text_chars": 0, "tier": "web"}
    fat = {"url": "https://a.com/fat", "slice": "web", "text_chars": 9000,
           "text_path": "sources/web_deadbeef.txt", "tier": "web"}
    _write_slice(tmp_path, "web", [thin, fat])

    body = "Rich full document body. " * 50
    monkeypatch.setattr(ff, "_safe_get",
                        lambda url, mb, to: (body.encode(), "text/html", url))

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 1 and summary["attempted"] == 1

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_web.jsonl").read_text().splitlines() if l.strip()]
    filled = next(r for r in rows if r["url"].endswith("/thin"))
    unchanged = next(r for r in rows if r["url"].endswith("/fat"))
    assert filled["text_chars"] >= 1200 and filled["fulltext_method"] == "html"
    assert (tmp_path / "round1" / filled["text_path"]).read_text().startswith("Rich full")
    assert unchanged["text_path"] == "sources/web_deadbeef.txt"  # not re-fetched

    manifest = json.loads((tmp_path / "round1" / "fulltext_manifest.json").read_text())
    assert manifest["by_method"] == {"html": 1}


def test_academic_row_resolves_openalex_oa_pdf(monkeypatch, tmp_path):
    row = {"url": "https://doi.org/10.1234/xyz", "slice": "anchor", "text_chars": 0, "tier": "peer"}
    _write_slice(tmp_path, "anchor", [row])

    pdf_bytes = b"%PDF-1.7 " + (b"paper text " * 100)
    oa_json = json.dumps({"best_oa_location": {"pdf_url": "https://repo.org/x.pdf"}}).encode()

    calls = []

    def fake_get(url, mb, to):
        calls.append(url)
        if "api.openalex.org" in url:
            return oa_json, "application/json", url
        return pdf_bytes, "application/pdf", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    # PDF extraction of our fake bytes yields nothing real, so stub the extractor
    # to prove the OA-resolution + method-tagging path independently of pypdf.
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "Extracted paper text " * 40)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 1
    assert any("api.openalex.org" in u for u in calls)
    assert any("repo.org/x.pdf" in u for u in calls)

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_anchor.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["fulltext_method"] == "oa:pdf"


def test_fetch_failure_is_fail_open(monkeypatch, tmp_path):
    _write_slice(tmp_path, "web",
                 [{"url": "https://a.com/x", "slice": "web", "text_chars": 0, "tier": "web"}])
    monkeypatch.setattr(ff, "_safe_get", lambda url, mb, to: (None, "http-403", None))
    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 0 and summary["skipped"] == 1
    assert summary["failures"] == {"http-403": 1}


def test_no_round1_dir_returns_zero(tmp_path, capsys):
    assert ff.main(["--run-dir", str(tmp_path)]) == 0


def test_no_verify_false_in_source():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "fetch_fulltext.py").read_text()
    assert "verify=False" not in src, "TLS verification must never be disabled"
