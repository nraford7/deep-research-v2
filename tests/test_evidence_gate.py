"""evidence_gate — OFFLINE. Recomputes metrics from jsonl; ignores the manifest."""

import json

import pytest

import config
from scripts import evidence_gate


def _write_slice(run_dir, name, urls, tier="news"):
    round1 = run_dir / "round1"
    round1.mkdir(parents=True, exist_ok=True)
    lines = []
    for u in urls:
        lines.append(json.dumps({"title": "T", "url": u, "published_date": None,
                                 "tier": tier, "slice": name}))
    (round1 / f"slice_{name}.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))


def _cfg(min_total=3, min_slices=2):
    return config.RunConfig(
        mode="slices", max_retrieval_usd=1.0, min_evidence_total=min_total,
        min_nonempty_slices=min_slices, slices={}, adversary_chain=["grok"],
        adversary="grok", synthesizer="claude", adversary_warning=None)


@pytest.fixture
def patch_cfg(monkeypatch):
    def _apply(min_total=3, min_slices=2):
        monkeypatch.setattr(config, "load_run_config", lambda *a, **k: _cfg(min_total, min_slices))
    return _apply


def test_pass_case(tmp_path, patch_cfg):
    patch_cfg(min_total=3, min_slices=2)
    _write_slice(tmp_path, "a", ["https://a.com/1", "https://a.com/2"])
    _write_slice(tmp_path, "b", ["https://b.com/1", "https://b.com/2"])
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == 0


def test_too_few_unique_fails_22(tmp_path, patch_cfg):
    patch_cfg(min_total=10, min_slices=2)
    _write_slice(tmp_path, "a", ["https://a.com/1"])
    _write_slice(tmp_path, "b", ["https://b.com/1"])
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT


def test_one_nonempty_slice_only_fails_22(tmp_path, patch_cfg):
    patch_cfg(min_total=1, min_slices=2)
    _write_slice(tmp_path, "a", ["https://a.com/1", "https://a.com/2"])
    _write_slice(tmp_path, "b", [])                    # empty
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT


def test_malformed_row_missing_url_caught(tmp_path, patch_cfg):
    patch_cfg(min_total=1, min_slices=1)
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    (round1 / "slice_a.jsonl").write_text(
        json.dumps({"title": "T", "tier": "news"}) + "\n")   # no url
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT


def test_malformed_row_missing_tier_caught(tmp_path, patch_cfg):
    patch_cfg(min_total=1, min_slices=1)
    round1 = tmp_path / "round1"
    round1.mkdir(parents=True)
    (round1 / "slice_a.jsonl").write_text(
        json.dumps({"title": "T", "url": "https://a.com/1"}) + "\n")  # no tier
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT


def test_manifest_is_ignored_recomputes_from_jsonl(tmp_path, patch_cfg):
    # A lying manifest claims a thick corpus; the real jsonl is thin → still FAIL.
    patch_cfg(min_total=10, min_slices=2)
    _write_slice(tmp_path, "a", ["https://a.com/1"])
    _write_slice(tmp_path, "b", ["https://b.com/1"])
    (tmp_path / "round1" / "evidence_manifest.json").write_text(
        json.dumps({"slices": {"a": {"unique": 99, "dropped": 0}}, "global_unique": 999}))
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT


def test_recovery_flow_fix_then_pass(tmp_path, patch_cfg):
    patch_cfg(min_total=3, min_slices=2)
    # Start thin → fails.
    _write_slice(tmp_path, "a", ["https://a.com/1"])
    _write_slice(tmp_path, "b", ["https://b.com/1"])
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == evidence_gate.GATE_FAIL_EXIT
    # Fix: add more evidence (simulating a --resume/rerun), then re-gate → passes.
    _write_slice(tmp_path, "a", ["https://a.com/1", "https://a.com/2", "https://a.com/3"])
    assert evidence_gate.main(["--run-dir", str(tmp_path)]) == 0


def test_dedupes_across_slices_for_global_unique(tmp_path, patch_cfg):
    # Same url in both slices → global_unique counts it once.
    patch_cfg(min_total=3, min_slices=2)
    _write_slice(tmp_path, "a", ["https://shared.com/x", "https://a.com/1"])
    _write_slice(tmp_path, "b", ["https://shared.com/x", "https://b.com/1"])
    diag = evidence_gate.evaluate(tmp_path)
    assert diag["global_unique"] == 3
