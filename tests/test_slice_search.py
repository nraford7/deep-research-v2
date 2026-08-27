"""slice_search — OFFLINE tests. Every Exa call is monkeypatched; NO network.

The academic anchor is also patched to a no-op so lit_search never touches
OpenAlex / Semantic Scholar.
"""

import json

import pytest

import config
from scripts import slice_search
from scripts.ledger import LedgerCapExceeded


# --- fixtures / helpers -----------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeSession:
    """Records every POST and returns queued canned bodies (or raises)."""

    def __init__(self, responses):
        # responses: list of (body_dict | Exception)
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "body": json})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)


def _exa_body(results, cost_total=None):
    body = {"results": results}
    if cost_total is not None:
        body["costDollars"] = {"total": cost_total}
    return body


def _result(url, title="T", published="2024-01-01", author=None, highlights=None):
    r = {"title": title, "url": url, "publishedDate": published}
    if author:
        r["author"] = author
    if highlights is not None:
        r["highlights"] = highlights
    return r


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Belt-and-braces: make the raw lit_search query fns explode if ever called
    without an explicit anchor patch, so a missed patch fails loudly (not silently
    hitting the network)."""
    def boom(*a, **k):
        raise AssertionError("lit_search hit the network in a test")
    monkeypatch.setattr(slice_search.lit_search, "query_openalex", boom)
    monkeypatch.setattr(slice_search.lit_search, "query_semantic_scholar", boom)


@pytest.fixture
def two_slice_cfg():
    """A RunConfig with one category slice + one includeDomains slice, both ON."""
    slices = {
        "publication": config.SliceSpec(
            query="{topic}", category="research paper",
            include_domains=None, enabled=True),
        "institutional": config.SliceSpec(
            query="{topic} policy", category=None,
            include_domains=("oecd.org", "imf.org"), enabled=True),
    }
    return config.RunConfig(
        mode="slices", max_retrieval_usd=1.0, min_evidence_total=10,
        min_nonempty_slices=2, slices=slices,
        adversary_chain=["grok"], adversary="grok", synthesizer="claude",
        adversary_warning=None)


def _patch_common(monkeypatch, run_cfg, session, anchor_empty=True):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(config, "load_run_config", lambda *a, **k: run_cfg)
    monkeypatch.setattr(slice_search, "make_session", lambda: session)
    if anchor_empty:
        monkeypatch.setattr(slice_search, "run_anchor",
                            lambda topic, round1_dir: (set(), 0, 0))


# --- request-body shape -----------------------------------------------------

def test_category_slice_uses_category_not_domains(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([_exa_body([_result("https://a.com/1")]),
                           _exa_body([_result("https://oecd.org/2")])])
    _patch_common(monkeypatch, two_slice_cfg, session)
    rc = slice_search.main(["--run-dir", str(tmp_path), "--topic", "batteries"])
    assert rc == 0
    pub = session.calls[0]["body"]
    assert pub["category"] == "research paper"
    assert "includeDomains" not in pub
    assert pub["query"] == "batteries"
    assert pub["numResults"] == 15
    assert pub["contents"] == {
        "highlights": True,
        "text": {"maxCharacters": slice_search.TEXT_MAX_CHARS},
    }


def test_domain_slice_uses_includeDomains_not_category(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([_exa_body([_result("https://a.com/1")]),
                           _exa_body([_result("https://oecd.org/2")])])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "batteries"])
    inst = session.calls[1]["body"]
    assert inst["includeDomains"] == ["oecd.org", "imf.org"]
    assert "category" not in inst


def test_news_slice_adds_startPublishedDate(monkeypatch, tmp_path):
    cfg_slices = {"news": config.SliceSpec(
        query="{topic} latest", category="news", include_domains=None, enabled=True)}
    run_cfg = config.RunConfig(
        mode="slices", max_retrieval_usd=1.0, min_evidence_total=1,
        min_nonempty_slices=1, slices=cfg_slices, adversary_chain=["grok"],
        adversary="grok", synthesizer="claude", adversary_warning=None)
    session = FakeSession([_exa_body([_result("https://n.com/1")])])
    _patch_common(monkeypatch, run_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x", "--fresh-since", "2024-01-01"])
    assert session.calls[0]["body"]["startPublishedDate"] == "2024-01-01"


# --- item validation / tiering ---------------------------------------------

def test_urlless_row_dropped_and_counted(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([_result("https://a.com/1"), {"title": "no url", "url": ""}]),
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    manifest = json.loads((tmp_path / "round1" / "evidence_manifest.json").read_text())
    assert manifest["slices"]["publication"]["dropped"] == 1
    assert manifest["slices"]["publication"]["unique"] == 1


def test_tier_assigned_via_tier_of(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([_result("https://arxiv.org/abs/1")]),
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    rows = [json.loads(l) for l in (tmp_path / "round1" / "slice_publication.jsonl")
            .read_text().splitlines() if l.strip()]
    assert rows[0]["tier"] == "preprint"


# --- normalization / dedupe -------------------------------------------------

def test_dedupe_within_slice(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([
            _result("https://a.com/page/"),
            _result("https://a.com/page"),                     # trailing slash
            _result("https://a.com/page?utm_source=x"),        # utm param
        ]),
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    manifest = json.loads((tmp_path / "round1" / "evidence_manifest.json").read_text())
    assert manifest["slices"]["publication"]["unique"] == 1


def test_global_unique_unions_across_slices(monkeypatch, tmp_path, two_slice_cfg):
    # Same URL appears in both slices → global_unique counts it once.
    session = FakeSession([
        _exa_body([_result("https://shared.com/x"), _result("https://a.com/1")]),
        _exa_body([_result("https://shared.com/x"), _result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    manifest = json.loads((tmp_path / "round1" / "evidence_manifest.json").read_text())
    assert manifest["slices"]["publication"]["unique"] == 2
    assert manifest["slices"]["institutional"]["unique"] == 2
    assert manifest["global_unique"] == 3   # shared, a.com/1, oecd.org/2


def test_doi_normalization_key(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([
            _result("https://doi.org/10.1/ABC"),
            _result("https://doi.org/10.1/abc"),   # case-fold to same DOI key
        ]),
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    manifest = json.loads((tmp_path / "round1" / "evidence_manifest.json").read_text())
    assert manifest["slices"]["publication"]["unique"] == 1


# --- ledger wiring ----------------------------------------------------------

def test_ledger_charged_then_reconciled(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([_result("https://a.com/1")], cost_total=0.011),
        _exa_body([_result("https://oecd.org/2")], cost_total=0.011),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert len(led["entries"]) == 2
    for e in led["entries"]:
        # charged the worst case ($0.04) BEFORE the call ...
        assert e["worst_case_usd"] == pytest.approx(0.04)
        # ... reconciled from costDollars.total AFTER.
        assert e["actual_usd"] == pytest.approx(0.011)


def test_ledger_reconciles_none_when_cost_absent(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        _exa_body([_result("https://a.com/1")]),        # no costDollars
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert led["entries"][0]["actual_usd"] is None


def test_ledger_refusal_exits_21_prior_slices_intact(monkeypatch, tmp_path, two_slice_cfg):
    # Cap allows exactly ONE $0.04 charge; the second slice's charge is refused.
    session = FakeSession([
        _exa_body([_result("https://a.com/1")], cost_total=0.04),
        _exa_body([_result("https://oecd.org/2")]),   # never reached
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    rc = slice_search.main(["--run-dir", str(tmp_path), "--topic", "x",
                            "--max-retrieval-usd", "0.04"])
    assert rc == LedgerCapExceeded.EXIT_CODE
    # First slice's file survives; second was never written.
    assert (tmp_path / "round1" / "slice_publication.jsonl").exists()
    assert not (tmp_path / "round1" / "slice_institutional.jsonl").exists()


# --- per-slice fail-open ----------------------------------------------------

def test_slice_fail_open_writes_empty_jsonl_exit_0(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([
        RuntimeError("network boom"),                  # first slice errors
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    rc = slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    assert rc == 0
    empty = (tmp_path / "round1" / "slice_publication.jsonl").read_text()
    assert empty.strip() == ""
    # ledger still charged + reconciled (to 0.0) for the failed call
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert led["entries"][0]["actual_usd"] == pytest.approx(0.0)


# --- resume -----------------------------------------------------------------

def test_resume_skips_parsed_slice(monkeypatch, tmp_path, two_slice_cfg):
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    # Pre-seed publication with a valid row so resume skips it.
    (round1 / "slice_publication.jsonl").write_text(
        json.dumps({"url": "https://pre.com/x", "tier": "news", "slice": "publication"}) + "\n")
    # Only the institutional slice should fire.
    session = FakeSession([_exa_body([_result("https://oecd.org/2")])])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x", "--resume"])
    assert len(session.calls) == 1                     # only one slice fired
    assert session.calls[0]["body"].get("includeDomains") == ["oecd.org", "imf.org"]
    manifest = json.loads((round1 / "evidence_manifest.json").read_text())
    assert manifest["slices"]["publication"]["unique"] == 1


def test_resume_reruns_malformed_slice(monkeypatch, tmp_path, two_slice_cfg):
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    (round1 / "slice_publication.jsonl").write_text("{not valid json\n")
    session = FakeSession([
        _exa_body([_result("https://a.com/1")]),       # publication re-run
        _exa_body([_result("https://oecd.org/2")]),
    ])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x", "--resume"])
    assert len(session.calls) == 2                     # malformed slice re-fired


# --- Retry(total=1) assertion ----------------------------------------------

def test_session_pins_retry_total_1():
    s = slice_search.make_session()
    adapter = s.get_adapter("https://api.exa.ai/search")
    assert adapter.max_retries.total == 1


# --- preflight --------------------------------------------------------------

def test_missing_exa_key_exits_20(monkeypatch, tmp_path, two_slice_cfg):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_run_config", lambda *a, **k: two_slice_cfg)
    rc = slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    assert rc == slice_search.EXA_PREFLIGHT_EXIT


# --- full-text spill (Fix #1: Exa text → sources/<file>.txt) ----------------

def test_request_body_asks_for_text():
    body = slice_search.build_request_body(
        "publication",
        config.SliceSpec(query="{topic}", category="research paper",
                         include_domains=None, enabled=True),
        "batteries")
    assert body["contents"]["highlights"] is True
    assert body["contents"]["text"] == {"maxCharacters": slice_search.TEXT_MAX_CHARS}


def test_result_text_spills_to_source_file(monkeypatch, tmp_path, two_slice_cfg):
    long_text = "Full extracted body. " * 200
    r = _result("https://a.com/1", highlights=["snip"])
    r["text"] = long_text
    session = FakeSession([_exa_body([r]),
                           _exa_body([_result("https://oecd.org/2")])])
    _patch_common(monkeypatch, two_slice_cfg, session)
    rc = slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    assert rc == 0

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_publication.jsonl").read_text().splitlines() if l.strip()]
    row = rows[0]
    # Text is NOT inlined in the index; it is pointed at a spill file.
    assert "text" not in row
    assert row["text_chars"] == len(long_text.strip())
    spill = tmp_path / "round1" / row["text_path"]
    assert spill.read_text(encoding="utf-8") == long_text.strip()


# --- _anchor_item citation-graph persistence --------------------------------

def test_anchor_item_persists_bare_openalex_id_and_refs():
    # A work carrying an OpenAlex id + referenced_works (full-URL forms) must
    # persist openalex_id and referenced_works in BARE W-form on the anchor row,
    # so downstream citation dedupe keys match.
    work = {
        "title": "Spine paper", "id": "https://openalex.org/W42",
        "doi": "10.1/spine", "year": 2019, "cited_by": 88, "authors": ["Q"],
        "venue": "Nature",
        "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"],
    }
    row = slice_search._anchor_item(work)
    assert row is not None
    assert row["openalex_id"] == "W42"
    assert row["referenced_works"] == ["W1", "W2"]
    assert row["url"] == "https://doi.org/10.1/spine"


def test_anchor_item_openalex_id_only_no_doi():
    # No DOI → url falls back to the OpenAlex id; openalex_id + refs still persist.
    work = {"title": "T", "id": "https://openalex.org/W7",
            "referenced_works": ["https://openalex.org/W3"]}
    row = slice_search._anchor_item(work)
    assert row["openalex_id"] == "W7"
    assert row["referenced_works"] == ["W3"]
    assert row["url"] == "https://openalex.org/W7"


def test_anchor_uses_openalex_without_calling_semantic_scholar(
    monkeypatch, tmp_path
):
    work = {
        "title": "Canonical work",
        "id": "https://openalex.org/W42",
        "doi": "10.1/canonical",
        "year": 1999,
    }
    monkeypatch.setattr(
        slice_search.lit_search, "query_openalex", lambda topic, limit: [work]
    )

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("Semantic Scholar fallback should not run")

    monkeypatch.setattr(
        slice_search.lit_search, "query_semantic_scholar", unexpected_fallback
    )

    keys, unique, dropped = slice_search.run_anchor("topic", tmp_path)

    assert keys == {"doi:10.1/canonical"}
    assert (unique, dropped) == (1, 0)


def test_anchor_falls_back_to_semantic_scholar_when_openalex_fails(
    monkeypatch, tmp_path
):
    calls = []

    def openalex_failure(*args, **kwargs):
        raise RuntimeError("OpenAlex quota exhausted")

    def semantic_success(topic, limit):
        calls.append((topic, limit))
        return [
            {
                "title": "Recovered work",
                "id": "https://www.semanticscholar.org/paper/abc",
                "doi": "10.2/recovered",
                "year": 2005,
            }
        ]

    monkeypatch.setattr(slice_search.lit_search, "query_openalex", openalex_failure)
    monkeypatch.setattr(
        slice_search.lit_search, "query_semantic_scholar", semantic_success
    )

    keys, unique, dropped = slice_search.run_anchor("topic", tmp_path)

    assert calls == [("topic", 15)]
    assert keys == {"doi:10.2/recovered"}
    assert (unique, dropped) == (1, 0)


def test_anchor_fails_open_only_after_both_scholarly_providers_fail(
    monkeypatch, tmp_path, capsys
):
    def provider_failure(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(slice_search.lit_search, "query_openalex", provider_failure)
    monkeypatch.setattr(
        slice_search.lit_search, "query_semantic_scholar", provider_failure
    )

    result = slice_search.run_anchor("topic", tmp_path)

    assert result == (set(), 0, 0)
    assert (tmp_path / "slice_anchor.jsonl").read_text() == ""
    assert "both providers failed" in capsys.readouterr().err


def test_result_without_text_gets_zero_chars(monkeypatch, tmp_path, two_slice_cfg):
    session = FakeSession([_exa_body([_result("https://a.com/1")]),
                           _exa_body([_result("https://oecd.org/2")])])
    _patch_common(monkeypatch, two_slice_cfg, session)
    slice_search.main(["--run-dir", str(tmp_path), "--topic", "x"])
    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_publication.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["text_chars"] == 0
    assert "text_path" not in rows[0]
    assert "text" not in rows[0]
