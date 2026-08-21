#!/usr/bin/env python3
"""evidence_gate.py — refuse synthesis over a thin or malformed Round-1 corpus.

Reads ``<run-dir>/round1/slice_*.jsonl`` and exits:

  * 0  — the corpus is thick enough AND every row re-validates
  * 22 — otherwise, printing a per-slice diagnosis to stdout

Pass conditions (ALL must hold):
  1. global_unique >= RunConfig.min_evidence_total
  2. non-empty slices >= RunConfig.min_nonempty_slices
  3. per-item re-validation: EVERY row across ALL slices has a non-empty ``url``
     and a non-empty ``tier`` (``published_date`` MAY be null)

The manifest at ``round1/evidence_manifest.json`` is DERIVED and NOT trusted:
the gate RECOMPUTES unique / global_unique straight from the jsonl files, so a
stale or hand-edited manifest cannot wave a thin corpus through.

Usage:
  python3 scripts/evidence_gate.py --run-dir DIR
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from scripts.slice_search import _norm_key

GATE_FAIL_EXIT = 22


def _load_slice(path):
    """Return (rows, malformed) where rows is the list of parsed row dicts and
    malformed is a list of (line_no, reason) for rows failing re-validation
    (bad JSON, missing/empty url, or missing/empty tier)."""
    rows = []
    malformed = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed.append((i, "invalid JSON"))
            continue
        if not isinstance(obj, dict):
            malformed.append((i, "row is not an object"))
            continue
        if not obj.get("url"):
            malformed.append((i, "missing url"))
            continue
        if not obj.get("tier"):
            malformed.append((i, "missing tier"))
            continue
        rows.append(obj)
    return rows, malformed


def evaluate(run_dir):
    """Recompute the corpus metrics from the jsonl files. Returns a diagnosis
    dict: {"passed", "global_unique", "nonempty_slices", "per_slice", "malformed"}."""
    round1_dir = Path(run_dir) / "round1"
    run_cfg = config.load_run_config()

    per_slice = {}
    malformed = {}
    global_keys = set()
    nonempty = 0

    for path in sorted(round1_dir.glob("slice_*.jsonl")):
        name = path.stem[len("slice_"):]
        rows, bad = _load_slice(path)
        if bad:
            malformed[name] = bad
        keys = {_norm_key(r["url"]) for r in rows}
        per_slice[name] = {"unique": len(keys), "rows": len(rows)}
        global_keys |= keys
        if rows:
            nonempty += 1

    global_unique = len(global_keys)
    passed = (
        global_unique >= run_cfg.min_evidence_total
        and nonempty >= run_cfg.min_nonempty_slices
        and not malformed
    )
    return {
        "passed": passed,
        "global_unique": global_unique,
        "nonempty_slices": nonempty,
        "min_evidence_total": run_cfg.min_evidence_total,
        "min_nonempty_slices": run_cfg.min_nonempty_slices,
        "per_slice": per_slice,
        "malformed": malformed,
    }


def _print_diagnosis(diag):
    print("Evidence gate — Round-1 corpus diagnosis")
    print(f"  global_unique   : {diag['global_unique']} (need >= {diag['min_evidence_total']})")
    print(f"  non-empty slices: {diag['nonempty_slices']} (need >= {diag['min_nonempty_slices']})")
    for name, s in sorted(diag["per_slice"].items()):
        print(f"    - {name}: {s['unique']} unique / {s['rows']} rows")
    if diag["malformed"]:
        print("  malformed rows (re-validation failed):")
        for name, bad in sorted(diag["malformed"].items()):
            for line_no, reason in bad:
                print(f"    - {name}:{line_no} {reason}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args(argv)

    diag = evaluate(args.run_dir)
    _print_diagnosis(diag)
    if diag["passed"]:
        print("PASS — corpus is thick enough and every row re-validates.")
        return 0
    print("FAIL — corpus too thin or a row failed re-validation; synthesis blocked.")
    return GATE_FAIL_EXIT


if __name__ == "__main__":
    sys.exit(main())
