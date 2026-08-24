"""lit_search citation-graph helpers — OFFLINE. Every OpenAlex GET is served by a
FakeSession; NO network, NO DNS. Covers query_openalex carrying referenced_works,
openalex_works_by_id batching (<=50 ids/request), openalex_cites parsing, and the
bare-id helper.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import lit_search


# --- fakes ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self._body


class FakeSession:
    """A GET-only session that records each request and returns canned bodies.

    ``bodies`` is either a single dict (served for every GET) or a list served
    in order. Headers dict is present so lit_search's header updates do not fail.
    """

    def __init__(self, bodies):
        self._bodies = bodies
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        if isinstance(self._bodies, list):
            body = self._bodies[min(len(self.calls) - 1, len(self._bodies) - 1)]
        else:
            body = self._bodies
        return FakeResponse(body)


def _oa_work(wid, refs=None, cited=0, doi=None, title="T"):
    return {
        "id": wid,
        "title": title,
        "publication_year": 2020,
        "cited_by_count": cited,
        "doi": doi,
        "referenced_works": refs or [],
        "authorships": [],
        "type": "article",
    }


# --- _bare_openalex_id ------------------------------------------------------

def test_bare_openalex_id_strips_url():
    assert lit_search._bare_openalex_id("https://openalex.org/W9") == "W9"
    assert lit_search._bare_openalex_id("W9") == "W9"


# --- query_openalex carries referenced_works --------------------------------

def test_query_openalex_carries_referenced_works(monkeypatch):
    body = {"results": [
        _oa_work("https://openalex.org/W100",
                 refs=["https://openalex.org/W1", "https://openalex.org/W2"],
                 cited=42, doi="https://doi.org/10.1/x", title="Anchor work"),
    ]}
    session = FakeSession(body)
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    out = lit_search.query_openalex("batteries", limit=1)
    assert len(out) == 1
    row = out[0]
    assert row["referenced_works"] == [
        "https://openalex.org/W1", "https://openalex.org/W2"]
    assert row["id"] == "https://openalex.org/W100"
    assert row["cited_by"] == 42


# --- openalex_works_by_id batches at <=50 ids/request -----------------------

def test_openalex_works_by_id_batches_120_into_3_requests(monkeypatch):
    session = FakeSession({"results": []})  # bodies are irrelevant; count requests
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    ids = [f"https://openalex.org/W{i}" for i in range(120)]
    lit_search.openalex_works_by_id(ids)

    # 120 unique bare ids, batched at 50 → ceil(120/50) == 3 requests, not 120.
    assert len(session.calls) == 3
    for call in session.calls:
        joined = call["params"]["filter"]
        assert joined.startswith("openalex_id:")
        n_in_batch = len(joined.split(":", 1)[1].split("|"))
        assert n_in_batch <= 50


def test_openalex_works_by_id_dedupes_and_parses(monkeypatch):
    body = {"results": [
        _oa_work("https://openalex.org/W1", cited=5, doi="10.1/a"),
        _oa_work("https://openalex.org/W2", cited=3),
    ]}
    session = FakeSession(body)
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    # Duplicate + URL/bare mix collapse to two unique bare ids → one request.
    out = lit_search.openalex_works_by_id(
        ["https://openalex.org/W1", "W1", "https://openalex.org/W2"])
    assert len(session.calls) == 1
    assert len(session.calls[0]["params"]["filter"].split(":", 1)[1].split("|")) == 2
    assert {w["id"] for w in out} == {
        "https://openalex.org/W1", "https://openalex.org/W2"}


def test_openalex_works_by_id_empty_makes_no_request(monkeypatch):
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    assert lit_search.openalex_works_by_id([]) == []
    assert session.calls == []


# --- openalex_cites parses --------------------------------------------------

def test_openalex_cites_parses_citing_works(monkeypatch):
    body = {"results": [
        _oa_work("https://openalex.org/W200", cited=9, title="Citing paper"),
    ]}
    session = FakeSession(body)
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    out = lit_search.openalex_cites("https://openalex.org/W5", limit=10)
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["filter"] == "cites:W5"
    assert out[0]["id"] == "https://openalex.org/W200"
    assert out[0]["title"] == "Citing paper"


def test_openalex_cites_batches_a_list_of_seeds(monkeypatch):
    # FIX F3: a LIST of seeds is OR'd into ONE request (cites:W1|W2|...), bare
    # ids, sorted by cited_by desc — that IS the forward candidate pool.
    body = {"results": [
        _oa_work("https://openalex.org/W200", cited=9, title="Citing paper"),
    ]}
    session = FakeSession(body)
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    out = lit_search.openalex_cites(
        ["https://openalex.org/W5", "W6", "https://openalex.org/W7"], limit=25)
    assert len(session.calls) == 1  # one batched request, not three
    assert session.calls[0]["params"]["filter"] == "cites:W5|W6|W7"
    assert session.calls[0]["params"]["sort"] == "cited_by_count:desc"
    assert out[0]["id"] == "https://openalex.org/W200"


def test_openalex_cites_splits_over_50_seeds(monkeypatch):
    # >50 seeds → ceil(#ids/50) requests, never one per id.
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    ids = [f"W{i}" for i in range(120)]
    lit_search.openalex_cites(ids, limit=25)
    assert len(session.calls) == 3  # 50 + 50 + 20


def test_openalex_cites_empty_list_makes_no_request(monkeypatch):
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    assert lit_search.openalex_cites([]) == []
    assert session.calls == []


def test_openalex_cites_http_error_raises(monkeypatch):
    # BUG S2: a real HTTP error must RAISE (fail closed) so citation_chase's
    # oa_failures counter fires exit 40, not silently return [].
    class ErrSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            return FakeResponse({}, status=503)
    session = ErrSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    with pytest.raises(lit_search.OpenAlexError):
        lit_search.openalex_cites("W5")


# --- BUG S1: DOI inputs use a doi: filter, never a mangled openalex_id: -------

def test_works_by_id_doi_input_uses_doi_filter(monkeypatch):
    # A DOI input like a doi.org URL must be batched via filter=doi:... — NEVER
    # run through _bare_openalex_id (which would rsplit "10.1/abc" → "abc" and
    # build a bogus openalex_id filter that 400s the whole batch).
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    lit_search.openalex_works_by_id(["https://doi.org/10.1/abc"])

    assert len(session.calls) == 1
    filt = session.calls[0]["params"]["filter"]
    assert filt.startswith("doi:"), f"DOI input must use a doi: filter, got {filt!r}"
    assert "openalex_id:" not in filt
    # The normalized DOI is carried whole (slashes intact), not mangled to a tail.
    assert filt == "doi:10.1/abc"


def test_works_by_id_doi_and_wid_do_not_share_a_batch(monkeypatch):
    # BUG S1 core: a DOI seed and a W-id seed must land in SEPARATE filters, so a
    # DOI can never 400 the batch a valid W-id shares. Count/inspect the requests.
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    lit_search.openalex_works_by_id(
        ["https://doi.org/10.1103/physrevlett.116.061102",
         "https://openalex.org/W42"])

    # Two disjoint groups → two requests, one doi:, one openalex_id:.
    assert len(session.calls) == 2
    filters = [c["params"]["filter"] for c in session.calls]
    doi_filters = [f for f in filters if f.startswith("doi:")]
    wid_filters = [f for f in filters if f.startswith("openalex_id:")]
    assert len(doi_filters) == 1 and len(wid_filters) == 1
    # No filter mixes the two — the W-id filter never contains the mangled DOI tail
    # and the DOI filter never contains the W-id.
    assert doi_filters[0] == "doi:10.1103/physrevlett.116.061102"
    assert wid_filters[0] == "openalex_id:W42"
    for f in filters:
        assert "physrevlett" not in f or f.startswith("doi:")


def test_works_by_id_http_error_raises(monkeypatch):
    # BUG S2: openalex_works_by_id must also RAISE on a non-ok response.
    class ErrSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            return FakeResponse({}, status=400)
    session = ErrSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    with pytest.raises(lit_search.OpenAlexError):
        lit_search.openalex_works_by_id(["https://openalex.org/W1"])


def test_works_by_id_genuine_empty_does_not_raise(monkeypatch):
    # A valid query returning 0 works is r.ok True + empty results — returns []
    # normally, must NOT raise.
    session = FakeSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    assert lit_search.openalex_works_by_id(["https://openalex.org/W1"]) == []


# --- G5: partial-batch failure is tolerated; only a TOTAL outage raises -------

def test_works_by_id_partial_batch_failure_returns_partial(monkeypatch):
    # 120 W-ids → 3 batches. First batch OK (returns one work), the rest fail.
    # G5: an early success must NOT be discarded and a partial outage must NOT
    # raise — it returns whatever succeeded.
    class PartialSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            if len(self.calls) == 1:
                return FakeResponse({"results": [_oa_work("https://openalex.org/W1")]})
            return FakeResponse({}, status=503)
    session = PartialSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    out = lit_search.openalex_works_by_id(
        [f"https://openalex.org/W{i}" for i in range(120)])
    assert len(session.calls) == 3          # all batches attempted
    assert [w["id"] for w in out] == ["https://openalex.org/W1"]  # partial kept


def test_works_by_id_total_outage_raises(monkeypatch):
    # 120 W-ids → 3 batches, EVERY batch fails → total outage → raise (exit 40).
    class DeadSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            return FakeResponse({}, status=503)
    session = DeadSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)
    with pytest.raises(lit_search.OpenAlexError):
        lit_search.openalex_works_by_id(
            [f"https://openalex.org/W{i}" for i in range(120)])


def test_cites_partial_batch_failure_returns_partial(monkeypatch):
    # 60 seed ids → 2 cites batches. First OK, second fails → partial return,
    # no raise (G5).
    class PartialSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            if len(self.calls) == 1:
                return FakeResponse({"results": [_oa_work("https://openalex.org/WC1")]})
            return FakeResponse({}, status=503)
    session = PartialSession({"results": []})
    monkeypatch.setattr(lit_search, "_make_session", lambda: session)

    out = lit_search.openalex_cites(
        [f"https://openalex.org/W{i}" for i in range(60)], limit=25)
    assert len(session.calls) == 2
    assert [w["id"] for w in out] == ["https://openalex.org/WC1"]
