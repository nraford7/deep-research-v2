#!/usr/bin/env python3
"""ledger.py — per-run hard money cap for metered retrieval calls.

A ``RetrievalLedger`` is one JSON file per run at
``<run_dir>/retrieval_ledger.json``::

    {"cap_usd": float,
     "entries": [{"ts", "script", "call_type", "worst_case_usd", "actual_usd"|null}]}

The retrieval scripts (Chunks 3+4) run single-process and sequentially, so
there is NO locking — one writer at a time. Every ``charge`` and ``reconcile``
writes the whole file back to disk immediately, so a crash can never lose a
spend commitment.

MONEY-SAFETY / CRASH SEMANTICS (by design, not a bug):
    Each metered call does ``charge(worst_case)`` BEFORE the call, then
    ``reconcile(index, actual)`` AFTER it returns. A crash BETWEEN charge and
    reconcile leaves a conservative PHANTOM COMMITMENT on disk — an entry with
    ``actual_usd = null`` that ``committed()`` still counts at its full
    worst-case. Every crash+resume cycle therefore erodes the remaining cap by
    the abandoned worst-case. This is intentional: over-counting spend is the
    safe failure. The recovery lever is to raise ``--max-retrieval-usd`` so a
    resumed run starts with fresh head-room. Early refusal after a crash is
    EXPECTED, not a defect.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path


def _finite_nonneg(value, label):
    """Coerce to a finite, non-negative float or raise ValueError. A retrieval
    cost/cap is never negative, NaN, or infinite — letting any of those into
    committed() would silently defeat the cap (nan > cap is False; a negative
    frees phantom head-room)."""
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise ValueError(f"{label} must be a finite, non-negative number, got {value!r}")
    return f


class LedgerCorrupt(ValueError):
    """The on-disk ledger is unreadable or malformed. Never silently reset —
    resetting would erase spend history and blow the cap."""


class LedgerCapExceeded(RuntimeError):
    """A charge would push committed spend past the run's cap. Scripts exit
    with EXIT_CODE; the orchestrator surfaces it and does NOT retry."""

    EXIT_CODE = 21


_REQUIRED_TOP_KEYS = ("cap_usd", "entries")


class RetrievalLedger:
    """Per-run retrieval spend ledger with a hard cap.

    On a fresh file the passed ``cap_usd`` sets the cap. On an existing file
    the on-disk cap + entries are adopted (the passed ``cap_usd`` updates the
    stored cap — a resumed run honours the caller's current ``--max-retrieval-usd``).
    """

    def __init__(self, run_dir: Path, cap_usd: float):
        self._path = Path(run_dir) / "retrieval_ledger.json"
        self.cap_usd = float(cap_usd)
        self.entries: list[dict] = []
        if self._path.exists():
            self._load()
            # Adopt prior entries; honour the caller's current cap on resume.
            self.cap_usd = float(cap_usd)
            self._persist()
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._persist()

    def _load(self) -> None:
        try:
            raw = self._path.read_text()
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise LedgerCorrupt(f"ledger unreadable at {self._path}: {exc}") from exc
        if not isinstance(data, dict) or any(k not in data for k in _REQUIRED_TOP_KEYS):
            raise LedgerCorrupt(f"ledger missing required keys at {self._path}: {data!r}")
        entries = data["entries"]
        if not isinstance(entries, list):
            raise LedgerCorrupt(f"ledger 'entries' not a list at {self._path}")
        for e in entries:
            if not isinstance(e, dict) or "worst_case_usd" not in e or "actual_usd" not in e:
                raise LedgerCorrupt(f"ledger entry malformed at {self._path}: {e!r}")
            # Values must be numeric/finite too — a non-numeric or NaN cost that
            # loads clean would crash or breach the cap on the first charge.
            try:
                _finite_nonneg(e["worst_case_usd"], "worst_case_usd")
                if e["actual_usd"] is not None:
                    _finite_nonneg(e["actual_usd"], "actual_usd")
            except (TypeError, ValueError) as exc:
                raise LedgerCorrupt(f"ledger entry has a bad cost at {self._path}: {e!r} ({exc})") from exc
        try:
            self.cap_usd = _finite_nonneg(data["cap_usd"], "cap_usd")
        except (TypeError, ValueError) as exc:
            raise LedgerCorrupt(f"ledger cap_usd bad at {self._path}: {data['cap_usd']!r} ({exc})") from exc
        self.entries = entries

    def _persist(self) -> None:
        # write-then-replace so a crash mid-write can't truncate the ledger
        # allow_nan=False: never let NaN/Infinity reach disk (JSON's non-standard
        # NaN literal would reload and silently defeat the cap).
        payload = json.dumps({"cap_usd": self.cap_usd, "entries": self.entries},
                             indent=2, allow_nan=False)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(self._path)

    def committed(self) -> float:
        """Total dollars committed so far. A PENDING (un-reconciled) entry
        counts at its full worst_case_usd; a RECONCILED entry counts at its
        actual_usd — so reconcile LOWERS committed when the call came in under
        worst-case and RAISES it when it ran over. (Equivalently:
        Σ max(worst_case, actual or 0) collapses to actual once reconciled,
        because worst_case is the pre-charge upper bound the actual replaces.)"""
        total = 0.0
        for e in self.entries:
            actual = e.get("actual_usd")
            total += e["worst_case_usd"] if actual is None else actual
        return total

    def charge(self, script: str, call_type: str, worst_case_usd: float) -> int:
        """Reserve ``worst_case_usd`` for an about-to-happen call. Returns the
        new entry's index. Raises LedgerCapExceeded (BEFORE persisting anything)
        when committed() + worst_case_usd > cap_usd. Exact-boundary (==) passes;
        strictly-greater refuses."""
        if self.committed() + worst_case_usd > self.cap_usd:
            raise LedgerCapExceeded(
                f"charge ${worst_case_usd:.4f} ({script}/{call_type}) would push "
                f"committed ${self.committed():.4f} past cap ${self.cap_usd:.4f}"
            )
        # datetime is called here (not at import) so the module imports cheaply.
        self.entries.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "script": script,
            "call_type": call_type,
            "worst_case_usd": float(worst_case_usd),
            "actual_usd": None,
        })
        self._persist()
        return len(self.entries) - 1

    def reconcile(self, index: int, actual_usd: float | None) -> None:
        """Record the true cost of a charged call. ``actual_usd`` replaces this
        entry's worst_case in committed(), so it may LOWER committed (call came in
        under worst-case) or RAISE it (ran over). ``None`` re-marks the entry
        pending. A non-finite or negative actual is REJECTED (ValueError) — the
        entry keeps its conservative worst_case commitment; a bad cost value can
        never free head-room or NaN-poison the cap."""
        self.entries[index]["actual_usd"] = (
            None if actual_usd is None else _finite_nonneg(actual_usd, "actual_usd")
        )
        self._persist()
