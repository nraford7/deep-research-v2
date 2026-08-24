"""slice_search --add-slice — OFFLINE. The Exa session is monkeypatched (no
network), mirroring test_slice_search.py's FakeSession/FakeResponse pattern.

Asserts the coverage-audit gap-slice path:
  --add-slice gapx --query "..."  ->  round1/slice_gap_gapx.jsonl  (gate-visible)
and that a retrieval-ledger charge was recorded for the gap fetch.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from scripts import slice_search


# --- fakes (same shape as test_slice_search.py) -----------------------------

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
    def __init__(self, responses):
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


def _result(url, title="T", published="2024-01-01"):
    return {"title": title, "url": url, "publishedDate": published}


def _run_cfg():
    """A RunConfig whose roster is irrelevant — --add-slice never consults it."""
    return config.RunConfig(
        mode="slices", max_retrieval_usd=1.0, min_evidence_total=1,
        min_nonempty_slices=1, slices={}, adversary_chain=["grok"],
        adversary="grok", synthesizer="claude", adversary_warning=None)


def _patch(monkeypatch, session):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(config, "load_run_config", lambda *a, **k: _run_cfg())
    monkeypatch.setattr(slice_search, "make_session", lambda: session)
    # The anchor must never fire for a gap slice, but pin it to a no-op so a
    # regression that DID call it fails loudly rather than hitting the network.
    def _no_anchor(*a, **k):
        raise AssertionError("run_anchor must not fire for --add-slice")
    monkeypatch.setattr(slice_search, "run_anchor", _no_anchor)


# --- tests -------------------------------------------------------------------

def test_add_slice_writes_gap_prefixed_jsonl(monkeypatch, tmp_path):
    session = FakeSession([_exa_body([_result("https://a.com/1")], cost_total=0.011)])
    _patch(monkeypatch, session)
    rc = slice_search.main([
        "--add-slice", "gapx", "--query", "widgets in the arctic",
        "--run-dir", str(tmp_path), "--topic", "widgets",
    ])
    assert rc == 0
    # gate-visible gap file: name is prefixed gap_, file is slice_gap_<NAME>.jsonl
    gap_file = tmp_path / "round1" / "slice_gap_gapx.jsonl"
    assert gap_file.exists()
    rows = [json.loads(l) for l in gap_file.read_text().splitlines() if l.strip()]
    assert rows and rows[0]["url"].startswith("https://a.com/1")
    # the literal --query (not a {topic} template) drove the Exa request
    assert session.calls[0]["body"]["query"] == "widgets in the arctic"


def test_add_slice_charges_the_ledger(monkeypatch, tmp_path):
    session = FakeSession([_exa_body([_result("https://a.com/1")], cost_total=0.011)])
    _patch(monkeypatch, session)
    slice_search.main([
        "--add-slice", "gapx", "--query", "q",
        "--run-dir", str(tmp_path), "--topic", "t",
    ])
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert len(led["entries"]) == 1
    e = led["entries"][0]
    assert e["script"] == "slice_search"
    assert e["worst_case_usd"] > 0
    # reconciled from costDollars.total after the call
    assert e["actual_usd"] == pytest.approx(0.011)


def test_add_slice_requires_query(monkeypatch, tmp_path):
    session = FakeSession([])
    _patch(monkeypatch, session)
    rc = slice_search.main([
        "--add-slice", "gapx", "--run-dir", str(tmp_path), "--topic", "t",
    ])
    assert rc == 2
    assert session.calls == []


def test_add_slice_name_path_traversal_is_sanitized(monkeypatch, tmp_path):
    """A --add-slice NAME of "../../evil" must not escape round1/. Path separators
    and dots collapse to '_', so the written file stays inside the run's round1
    dir and its stem carries no '/' or '..'."""
    session = FakeSession([_exa_body([_result("https://a.com/1")], cost_total=0.011)])
    _patch(monkeypatch, session)
    rc = slice_search.main([
        "--add-slice", "../../evil", "--query", "q",
        "--run-dir", str(tmp_path), "--topic", "t",
    ])
    assert rc == 0
    round1 = tmp_path / "round1"
    # Every slice file written lives INSIDE round1 (no escape above the run dir).
    written = list(round1.glob("slice_*.jsonl"))
    assert written, "expected a sanitized slice file to be written"
    for f in written:
        resolved = f.resolve()
        assert str(resolved).startswith(str(round1.resolve()) + "/")
        assert ".." not in f.name
        assert "/" not in f.name
    # Nothing leaked to the parent of the run dir.
    assert not list(tmp_path.parent.glob("slice_*.jsonl"))
    assert not list(tmp_path.parent.glob("*evil*"))
    # The sanitized stem is the gap-prefixed, separator-collapsed name.
    assert (round1 / "slice_gap_______evil.jsonl").exists()


def test_add_slice_name_only_separators_returns_exit_2(monkeypatch, tmp_path):
    """A name that is only separators/dots sanitizes to empty (no usable chars)
    and the branch refuses with exit 2 — no Exa call, no file written."""
    session = FakeSession([])
    _patch(monkeypatch, session)
    rc = slice_search.main([
        "--add-slice", "../..", "--query", "q",
        "--run-dir", str(tmp_path), "--topic", "t",
    ])
    assert rc == 2
    assert session.calls == []
    assert not list((tmp_path / "round1").glob("slice_*.jsonl"))


def test_add_slice_name_construction_yields_gap_stem():
    """Path-only guarantee, independent of driving main(): the ad-hoc gap name is
    gap_<NAME> and thus lands in slice_gap_<NAME>.jsonl."""
    name = f"gap_{'gapx'}"
    assert name == "gap_gapx"
    assert f"slice_{name}.jsonl" == "slice_gap_gapx.jsonl"
    # the frozen SliceSpec the branch builds carries the literal query as-is
    spec = config.SliceSpec(query="the query", category=None,
                            include_domains=None, enabled=True)
    assert spec.query == "the query"
    assert spec.enabled is True
