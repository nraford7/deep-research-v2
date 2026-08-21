import json

import pytest

from scripts.ledger import LedgerCapExceeded, LedgerCorrupt, RetrievalLedger


def test_exit_code_is_21():
    assert LedgerCapExceeded.EXIT_CODE == 21


def test_new_file_has_cap_and_empty_entries(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    p = tmp_path / "retrieval_ledger.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["cap_usd"] == 1.0
    assert data["entries"] == []
    assert led.committed() == 0.0


def test_reload_sees_prior_entries(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    idx = led.charge("search.py", "slice_search", 0.04)
    assert idx == 0

    reloaded = RetrievalLedger(tmp_path, cap_usd=1.0)
    assert len(reloaded.entries) == 1
    assert reloaded.committed() == pytest.approx(0.04)


def test_charge_accumulates_committed(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    led.charge("s", "slice_search", 0.04)
    led.charge("s", "slice_search", 0.06)
    assert led.committed() == pytest.approx(0.10)


def test_refuse_strictly_over_cap_at_exact_boundary(tmp_path):
    # cap 0.10: two 0.04 charges pass (committed 0.08); third 0.04 -> 0.12 > 0.10 refuses.
    led = RetrievalLedger(tmp_path, cap_usd=0.10)
    led.charge("s", "slice_search", 0.04)
    led.charge("s", "slice_search", 0.04)
    assert led.committed() == pytest.approx(0.08)
    with pytest.raises(LedgerCapExceeded):
        led.charge("s", "slice_search", 0.04)
    # the over-cap entry was NOT persisted
    assert len(led.entries) == 2
    reloaded = RetrievalLedger(tmp_path, cap_usd=0.10)
    assert len(reloaded.entries) == 2


def test_equal_boundary_passes(tmp_path):
    # cap 0.12 lets the third 0.04 through (committed hits exactly 0.12).
    led = RetrievalLedger(tmp_path, cap_usd=0.12)
    led.charge("s", "slice_search", 0.04)
    led.charge("s", "slice_search", 0.04)
    led.charge("s", "slice_search", 0.04)
    assert led.committed() == pytest.approx(0.12)


def test_reconcile_lowers_committed_when_actual_below_worst(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    idx = led.charge("s", "slice_search", 0.10)
    assert led.committed() == pytest.approx(0.10)
    led.reconcile(idx, 0.03)
    assert led.committed() == pytest.approx(0.03)
    # persisted
    reloaded = RetrievalLedger(tmp_path, cap_usd=1.0)
    assert reloaded.committed() == pytest.approx(0.03)


def test_reconcile_raises_committed_when_actual_above_worst(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    idx = led.charge("s", "slice_search", 0.10)
    led.reconcile(idx, 0.25)
    # committed uses max(worst, actual)
    assert led.committed() == pytest.approx(0.25)


def test_phantom_commitment_charge_without_reconcile(tmp_path):
    # A crash between charge and reconcile leaves a conservative phantom:
    # committed stays at the full worst_case with actual=null.
    led = RetrievalLedger(tmp_path, cap_usd=1.0)
    led.charge("s", "slice_search", 0.10)  # never reconciled
    reloaded = RetrievalLedger(tmp_path, cap_usd=1.0)
    assert reloaded.entries[0]["actual_usd"] is None
    assert reloaded.committed() == pytest.approx(0.10)


def test_corrupted_json_raises_not_reset(tmp_path):
    p = tmp_path / "retrieval_ledger.json"
    p.write_text("{ not valid json ")
    with pytest.raises(LedgerCorrupt):
        RetrievalLedger(tmp_path, cap_usd=1.0)
    # file untouched — history not erased
    assert p.read_text() == "{ not valid json "


def test_missing_required_keys_raises_not_reset(tmp_path):
    p = tmp_path / "retrieval_ledger.json"
    p.write_text(json.dumps({"cap_usd": 1.0}))  # no "entries"
    with pytest.raises(LedgerCorrupt):
        RetrievalLedger(tmp_path, cap_usd=1.0)


def test_retrieval_fees_and_retry_multiplier_importable():
    from scripts.cost import RETRIEVAL_FEES, RETRY_MULTIPLIER
    assert RETRIEVAL_FEES == {"slice_search": 0.02, "deep_reasoning": 0.02}
    assert RETRY_MULTIPLIER == 2


# --- hardening: bad cost inputs must never defeat the cap -----------------------

def test_reconcile_rejects_nan_actual(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=0.10)
    i = led.charge("s.py", "slice_search", 0.04)
    with pytest.raises(ValueError):
        led.reconcile(i, float("nan"))
    # entry stays at its conservative worst_case; cap still enforced
    assert led.committed() == 0.04
    led.charge("s.py", "slice_search", 0.04)      # 0.08, ok
    with pytest.raises(LedgerCapExceeded):
        led.charge("s.py", "slice_search", 0.04)  # 0.12 > 0.10 still refuses


def test_reconcile_rejects_infinite_actual(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=0.10)
    i = led.charge("s.py", "slice_search", 0.04)
    with pytest.raises(ValueError):
        led.reconcile(i, float("inf"))
    assert led.committed() == 0.04


def test_reconcile_rejects_negative_actual(tmp_path):
    led = RetrievalLedger(tmp_path, cap_usd=0.10)
    i = led.charge("s.py", "slice_search", 0.08)
    with pytest.raises(ValueError):
        led.reconcile(i, -0.05)
    # committed must NOT drop below the real reservation
    assert led.committed() == 0.08


def test_load_rejects_non_numeric_worst_case(tmp_path):
    p = tmp_path / "retrieval_ledger.json"
    p.write_text(json.dumps({"cap_usd": 1.0, "entries": [
        {"ts": "t", "script": "s", "call_type": "c", "worst_case_usd": "x", "actual_usd": None}]}))
    with pytest.raises(LedgerCorrupt):
        RetrievalLedger(tmp_path, cap_usd=1.0)
    # file left intact (never silently reset)
    assert "\"x\"" in p.read_text()


def test_load_rejects_non_numeric_cap(tmp_path):
    p = tmp_path / "retrieval_ledger.json"
    p.write_text(json.dumps({"cap_usd": "abc", "entries": []}))
    with pytest.raises(LedgerCorrupt):
        RetrievalLedger(tmp_path, cap_usd=1.0)


def test_load_rejects_nan_actual_in_file(tmp_path):
    # NaN persisted by a non-conforming writer must not reload as a live entry
    p = tmp_path / "retrieval_ledger.json"
    p.write_text('{"cap_usd": 1.0, "entries": [{"ts":"t","script":"s","call_type":"c","worst_case_usd":0.04,"actual_usd":NaN}]}')
    with pytest.raises(LedgerCorrupt):
        RetrievalLedger(tmp_path, cap_usd=1.0)
