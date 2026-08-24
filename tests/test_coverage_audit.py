"""coverage_audit — OFFLINE. The LLM (llm.call_model) is monkeypatched, and the
slice_search / evidence_gate subprocess shell-outs are stubbed so NO network and
NO real child process ever run.

Covers three behaviours:
  1. coverage_gaps.md is written each round.
  2. --max-audit-rounds caps the loop even when the LLM keeps reporting gaps.
  3. a mid-fetch ledger cap (child exit 21) is a GRACEFUL exit-21: coverage_gaps.md
     is on disk BEFORE the audit stops; the run is never aborted/crashed.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import llm
from scripts import coverage_audit
from scripts.ledger import LedgerCapExceeded


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _provider():
    return config.Provider("kimi", "openai", "k", "m")


def _patch_provider(monkeypatch):
    """Make _get_provider resolve a fake provider without touching config files."""
    monkeypatch.setattr(config, "default_toml_paths", lambda: [])
    monkeypatch.setattr(config, "load_env_files", lambda: {})
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({"kimi": _provider()}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {"utility": "kimi"})
    monkeypatch.setattr(config, "pick_provider", lambda providers, role, defaults: _provider())


def _gaps_json():
    return json.dumps({"gaps": [{"gap": "missing arctic angle", "query": "widgets arctic"}]})


# --- 1 + 2: cap respected even when the LLM keeps finding gaps ---------------

def test_writes_gaps_and_respects_round_cap(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)

    calls = {"model": 0, "fetch": 0}

    # enumerate_gaps and gaps_remain both call llm.call_model. Alternate:
    #   enumerate -> gaps json ; gaps_remain -> "yes still gaps" (true)
    # so the loop would run forever if the round cap were not honoured.
    def fake_call(provider, system, user):
        calls["model"] += 1
        if "material coverage" in system.lower() or "material_gaps_remain" in system.lower():
            return json.dumps({"material_gaps_remain": True})
        return _gaps_json()
    monkeypatch.setattr(llm, "call_model", fake_call)

    # No network: stub the gap fetch (success) + evidence gate.
    def fake_fetch(run_dir, topic, name, query, cap_usd):
        calls["fetch"] += 1
        return 0
    monkeypatch.setattr(coverage_audit, "_fetch_gap", fake_fetch)
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", lambda run_dir: 0)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--max-audit-rounds", "2",
    ])
    assert rc == 0
    # coverage_gaps.md written (last round's gaps).
    gaps_md = tmp_path / "round1" / "coverage_gaps.md"
    assert gaps_md.exists()
    assert "missing arctic angle" in gaps_md.read_text()
    # LLM reported gaps every round, but the loop stopped at the ceiling: exactly
    # two enumerate+remain pairs, and two fetch rounds — not an unbounded loop.
    assert calls["fetch"] == 2


# --- 3: graceful exit-21 on a mid-fetch ledger cap --------------------------

def test_ledger_cap_writes_gaps_then_exits_21(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)

    def fake_call(provider, system, user):
        # First round enumerates a gap; a cap trips during the fetch below, so
        # gaps_remain is never reached.
        return _gaps_json()
    monkeypatch.setattr(llm, "call_model", fake_call)

    # The child (slice_search) trips the cap and exits 21. _fetch_gap shells out
    # via subprocess.run; stub that to return the cap exit code.
    monkeypatch.setattr(coverage_audit.subprocess, "run", lambda *a, **k: _Proc(LedgerCapExceeded.EXIT_CODE))

    gate_calls = {"n": 0}
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate",
                        lambda run_dir: gate_calls.__setitem__("n", gate_calls["n"] + 1) or 0)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--max-audit-rounds", "2",
    ])
    # graceful bounded stop, not an abort/crash
    assert rc == LedgerCapExceeded.EXIT_CODE
    # coverage_gaps.md written BEFORE the audit stopped
    gaps_md = tmp_path / "round1" / "coverage_gaps.md"
    assert gaps_md.exists()
    assert "missing arctic angle" in gaps_md.read_text()
    # cap tripped during fetch — the evidence gate re-run never happened
    assert gate_calls["n"] == 0


# --- a non-21 fetch failure is surfaced, NOT treated as success -------------

def test_fetch_failure_stops_and_returns_child_code(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: _gaps_json())

    # slice_search exits 20 (missing Exa key). This is NOT the cap exit (21) and
    # must NOT be treated as a successful fetch: the audit stops and returns 20.
    monkeypatch.setattr(coverage_audit, "_fetch_gap",
                        lambda run_dir, topic, name, query, cap_usd: 20)
    # gate must never run — the fetch failed first.
    def _no_gate(*a, **k):
        raise AssertionError("evidence gate must not run after a failed fetch")
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", _no_gate)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--max-audit-rounds", "2",
    ])
    assert rc == 20
    gaps_md = (tmp_path / "round1" / "coverage_gaps.md").read_text()
    assert "Audit stopped: failure" in gaps_md
    assert "exit 20" in gaps_md


# --- both a thin (22) re-gate and a crash gate are surfaced as nonzero ------

def test_thin_regate_is_not_a_success(monkeypatch, tmp_path):
    # A re-gate returning 22 (corpus STILL too thin / malformed rows) is NOT a
    # successful audit: exit 0 is required before synthesis. It must surface a
    # nonzero result, not be swallowed as informational.
    _patch_provider(monkeypatch)

    def fake_call(provider, system, user):
        if "material_gaps_remain" in system.lower() or "material coverage" in system.lower():
            return json.dumps({"material_gaps_remain": False})
        return _gaps_json()
    monkeypatch.setattr(llm, "call_model", fake_call)
    monkeypatch.setattr(coverage_audit, "_fetch_gap",
                        lambda run_dir, topic, name, query, cap_usd: 0)

    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", lambda run_dir: 22)
    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 22
    gaps_md = (tmp_path / "round1" / "coverage_gaps.md").read_text()
    assert "Audit stopped: failure" in gaps_md
    assert "still too thin" in gaps_md


def test_gate_crash_stops_and_returns_code(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)

    def fake_call(provider, system, user):
        if "material_gaps_remain" in system.lower() or "material coverage" in system.lower():
            return json.dumps({"material_gaps_remain": False})
        return _gaps_json()
    monkeypatch.setattr(llm, "call_model", fake_call)
    monkeypatch.setattr(coverage_audit, "_fetch_gap",
                        lambda run_dir, topic, name, query, cap_usd: 0)

    # A gate crash (exit 2) IS a hard failure: the audit stops and returns it.
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", lambda run_dir: 2)
    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 2
    assert "Audit stopped: failure" in (tmp_path / "round1" / "coverage_gaps.md").read_text()


# --- --audit-usd is a FIXED total; never lowers a higher run cap ------------

def test_audit_usd_never_lowers_higher_run_cap(monkeypatch, tmp_path):
    # Seed a ledger with a $1.00 run cap and $0.08 already committed.
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    (tmp_path / "retrieval_ledger.json").write_text(json.dumps({
        "cap_usd": 1.0,
        "entries": [
            {"worst_case_usd": 0.04, "actual_usd": 0.03},
            {"worst_case_usd": 0.05, "actual_usd": None},
        ],
    }))
    # committed = 0.03 (reconciled) + 0.05 (pending worst-case) = 0.08
    assert coverage_audit._current_spend(tmp_path) == pytest.approx(0.08)
    # The run's own cap is read from the ledger.
    assert coverage_audit._current_run_cap(tmp_path) == pytest.approx(1.0)
    # audit_usd 0.10 -> ceiling = max(run_cap 1.0, spend 0.08 + 0.10 = 0.18) = 1.0.
    # The audit must NEVER pass a cap lower than the run's existing $1.00 cap.
    assert coverage_audit._resolve_cap(tmp_path, 0.10) == pytest.approx(1.0)
    # No audit_usd -> None (leave the run cap untouched).
    assert coverage_audit._resolve_cap(tmp_path, None) is None


def test_audit_usd_extends_above_spend_when_run_cap_low(tmp_path):
    # Run cap is BELOW spend + audit_usd here, so the audit ceiling is the
    # extended headroom, a FIXED total for the whole audit (spend + audit_usd),
    # not a reset to just audit_usd.
    (tmp_path / "retrieval_ledger.json").write_text(json.dumps({
        "cap_usd": 0.08,
        "entries": [{"worst_case_usd": 0.08, "actual_usd": 0.08}],
    }))
    assert coverage_audit._current_spend(tmp_path) == pytest.approx(0.08)
    # ceiling = max(run_cap 0.08, spend 0.08 + audit 0.10 = 0.18) = 0.18.
    assert coverage_audit._resolve_cap(tmp_path, 0.10) == pytest.approx(0.18)


def test_audit_ceiling_is_fixed_across_fetches(monkeypatch, tmp_path):
    # The ceiling is computed ONCE at audit start and passed UNCHANGED to every
    # fetch, so it is not replenished per fetch (which would let total audit
    # spend exceed audit_usd). Two gaps in one round must both receive the SAME
    # cap value even though the first fetch would have added spend.
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    (tmp_path / "retrieval_ledger.json").write_text(json.dumps({
        "cap_usd": 0.05,
        "entries": [{"worst_case_usd": 0.05, "actual_usd": 0.05}],
    }))
    _patch_provider(monkeypatch)

    def fake_call(provider, system, user):
        if "material_gaps_remain" in system.lower() or "material coverage" in system.lower():
            return json.dumps({"material_gaps_remain": False})
        return json.dumps({"gaps": [
            {"gap": "gap one", "query": "q1"},
            {"gap": "gap two", "query": "q2"},
        ]})
    monkeypatch.setattr(llm, "call_model", fake_call)

    caps_seen = []

    def fake_fetch(run_dir, topic, name, query, cap_usd):
        caps_seen.append(cap_usd)
        # Simulate the fetch adding spend to the ledger; the ceiling must NOT
        # move in response, since it was fixed once at audit start.
        (Path(run_dir) / "retrieval_ledger.json").write_text(json.dumps({
            "cap_usd": 0.05,
            "entries": [
                {"worst_case_usd": 0.05, "actual_usd": 0.05},
                {"worst_case_usd": 0.04, "actual_usd": 0.04},
            ],
        }))
        return 0
    monkeypatch.setattr(coverage_audit, "_fetch_gap", fake_fetch)
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", lambda run_dir: 0)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets",
        "--audit-usd", "0.10", "--max-audit-rounds", "1",
    ])
    assert rc == 0
    # Both fetches saw the SAME fixed ceiling = max(0.05, 0.05 + 0.10) = 0.15.
    assert len(caps_seen) == 2
    assert caps_seen[0] == pytest.approx(0.15)
    assert caps_seen[1] == pytest.approx(0.15)


# --- _load_context grounds gap judgment in retrieved CONTENT ----------------

def test_load_context_reads_highlights_and_full_text(tmp_path):
    round1 = tmp_path / "round1"
    (round1 / "sources").mkdir(parents=True)
    (round1 / "sources" / "web_abc.txt").write_text(
        "FULLTEXT_BODY about arctic widget supply chains.")
    (round1 / "slice_web.jsonl").write_text(json.dumps({
        "title": "Arctic widgets", "url": "https://x.test/a", "tier": "news",
        "highlights": ["HIGHLIGHT_SNIPPET one", "HIGHLIGHT_SNIPPET two"],
        "text_path": "sources/web_abc.txt",
    }) + "\n")
    # Anchor stays titles-only, no highlights/full text folded from it.
    (round1 / "slice_anchor.jsonl").write_text(json.dumps({
        "title": "Some paper", "url": "https://doi.org/10.1/x", "tier": "peer",
    }) + "\n")

    ctx = coverage_audit._load_context(tmp_path)
    assert "HIGHLIGHT_SNIPPET one" in ctx
    assert "HIGHLIGHT_SNIPPET two" in ctx
    assert "FULLTEXT_BODY about arctic" in ctx
    assert "retrieved content" in ctx.lower()

    # Char budget bounds the content section.
    small = coverage_audit._load_context(tmp_path, max_chars=10)
    assert "FULLTEXT_BODY about arctic widget supply chains." not in small


# --- no-gap round stops cleanly ---------------------------------------------

def test_no_gaps_stops_first_round(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: json.dumps({"gaps": []}))
    # fetch must never be called on a clean round
    def _boom(*a, **k):
        raise AssertionError("no fetch should occur when there are no gaps")
    monkeypatch.setattr(coverage_audit, "_fetch_gap", _boom)
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", _boom)
    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0
    assert (tmp_path / "round1" / "coverage_gaps.md").exists()


# --- FAIL CLOSED: a required audit that cannot RUN is nonzero, not "no gaps" -

def test_missing_provider_fails_closed(monkeypatch, tmp_path):
    # No provider configured. This must NOT look like a clean "no gaps" pass:
    # it returns the distinct AUDIT_NO_PROVIDER code and records why on disk.
    monkeypatch.setattr(config, "default_toml_paths", lambda: [])
    monkeypatch.setattr(config, "load_env_files", lambda: {})
    monkeypatch.setattr(config, "load_config", lambda paths, env: ({}, {}))
    monkeypatch.setattr(config, "load_defaults", lambda paths: {})
    monkeypatch.setattr(config, "pick_provider", lambda providers, role, defaults: None)

    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == coverage_audit.AUDIT_NO_PROVIDER
    assert rc != 0
    gaps_md = (tmp_path / "round1" / "coverage_gaps.md").read_text()
    assert "could not RUN" in gaps_md
    assert "NOT a clean no-gap pass" in gaps_md
    # It must NOT falsely report a clean pass.
    assert "no material gaps found" not in gaps_md


def test_config_error_fails_closed(monkeypatch, tmp_path):
    # A config error resolving the provider is a "cannot run", not a pass.
    def _boom(*a, **k):
        raise RuntimeError("toml exploded")
    monkeypatch.setattr(config, "default_toml_paths", _boom)
    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == coverage_audit.AUDIT_NO_PROVIDER
    assert rc != 0


def test_model_error_fails_closed(monkeypatch, tmp_path):
    # The enumerate model call raises. This is "cannot run", NOT an empty gap
    # list reported as a clean pass: returns the distinct AUDIT_LLM_ERROR code.
    _patch_provider(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("model timeout")
    monkeypatch.setattr(llm, "call_model", _raise)

    def _boom(*a, **k):
        raise AssertionError("no fetch should occur when the audit cannot run")
    monkeypatch.setattr(coverage_audit, "_fetch_gap", _boom)

    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == coverage_audit.AUDIT_LLM_ERROR
    assert rc != 0
    assert "could not RUN" in (tmp_path / "round1" / "coverage_gaps.md").read_text()


def test_unparseable_gaps_json_fails_closed(monkeypatch, tmp_path):
    # The model reply has no parseable JSON object. This is "cannot run", NOT a
    # clean no-gap pass: returns the distinct AUDIT_BAD_JSON code.
    _patch_provider(monkeypatch)
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: "sorry, no json here")

    def _boom(*a, **k):
        raise AssertionError("no fetch should occur when the audit cannot run")
    monkeypatch.setattr(coverage_audit, "_fetch_gap", _boom)

    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == coverage_audit.AUDIT_BAD_JSON
    assert rc != 0
    assert "could not RUN" in (tmp_path / "round1" / "coverage_gaps.md").read_text()


def test_genuine_empty_gap_list_is_a_clean_pass(monkeypatch, tmp_path):
    # A well-formed {"gaps": []} is a real "ran, found nothing" result and MUST
    # still return 0, distinct from the "could not run" failures above.
    _patch_provider(monkeypatch)
    monkeypatch.setattr(llm, "call_model", lambda p, s, u: json.dumps({"gaps": []}))
    rc = coverage_audit.main(["--run-dir", str(tmp_path), "--topic", "t"])
    assert rc == 0


# --- CROSS-RERUN: an existing gap slice is skipped, not overwritten ---------

def test_existing_gap_slice_is_skipped_not_overwritten(monkeypatch, tmp_path):
    # After an exit-21 cap-stop, the documented rerun restarts at round 1 and
    # re-derives the same r1_<slug> names. If slice_gap_<name>.jsonl already
    # exists, the fetch must be SKIPPED (prior evidence preserved), not redone.
    _patch_provider(monkeypatch)

    def fake_call(provider, system, user):
        if "material_gaps_remain" in system.lower() or "material coverage" in system.lower():
            return json.dumps({"material_gaps_remain": False})
        return _gaps_json()
    monkeypatch.setattr(llm, "call_model", fake_call)

    # Pre-seed the slice that the round-1 rerun would recreate. _gaps_json's gap
    # is "missing arctic angle" -> slug -> r1_missing-arctic-angle.
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    existing = round1 / "slice_gap_r1_missing-arctic-angle.jsonl"
    existing.write_text('{"url": "https://prior.test/x", "tier": "news"}\n')
    prior_bytes = existing.read_bytes()

    fetches = {"n": 0}

    def fake_fetch(run_dir, topic, name, query, cap_usd):
        fetches["n"] += 1
        # If this ran, it would overwrite the prior slice, discarding evidence.
        (Path(run_dir) / "round1" / f"slice_gap_{name}.jsonl").write_text("OVERWRITTEN\n")
        return 0
    monkeypatch.setattr(coverage_audit, "_fetch_gap", fake_fetch)
    monkeypatch.setattr(coverage_audit, "_run_evidence_gate", lambda run_dir: 0)

    rc = coverage_audit.main([
        "--run-dir", str(tmp_path), "--topic", "widgets", "--max-audit-rounds", "1",
    ])
    assert rc == 0
    # The pre-existing slice was NOT re-fetched and NOT overwritten.
    assert fetches["n"] == 0
    assert existing.read_bytes() == prior_bytes


# --- anchor CONTENT is folded into the auditor context ----------------------

def test_load_context_includes_anchor_content(tmp_path):
    # fetch_fulltext adds highlights + text_path to anchor rows too, so the
    # auditor must see the anchor's retrieved CONTENT (not treat scholarly
    # coverage as absent when it was in fact retrieved).
    round1 = tmp_path / "round1"
    (round1 / "sources").mkdir(parents=True)
    (round1 / "sources" / "anchor_1.txt").write_text(
        "ANCHOR_FULLTEXT peer-reviewed widget metallurgy.")
    (round1 / "slice_anchor.jsonl").write_text(json.dumps({
        "title": "A peer paper", "url": "https://doi.org/10.1/x", "tier": "peer",
        "highlights": ["ANCHOR_HIGHLIGHT one"],
        "text_path": "sources/anchor_1.txt",
    }) + "\n")

    ctx = coverage_audit._load_context(tmp_path)
    assert "ANCHOR_HIGHLIGHT one" in ctx
    assert "ANCHOR_FULLTEXT peer-reviewed widget metallurgy." in ctx
