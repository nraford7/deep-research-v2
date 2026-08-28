#!/usr/bin/env python3
"""coverage_audit.py, post-Round-1 coverage auditor: name the gaps, fill them, stop.

The evidence gate asks "is the corpus thick enough?"; this asks a different
question: "for THIS scope, what coverage a competent reader would expect is
absent?" An LLM enumerates expected-but-absent coverage bounded to the run's
scope, each gap paired with one scope-bounded Exa query. Each query is fired as
an ad-hoc gap slice via ``slice_search.py --add-slice`` (ledger-charged,
glob-visible to the evidence gate), then the evidence gate re-runs, then the LLM
is asked whether material gaps remain. The loop stops when no gaps remain, the
``--max-audit-rounds`` ceiling is hit, or the retrieval ledger cap trips.

WHAT THE AUDITOR WRITES: only ``round1/coverage_gaps.md`` (each gap + its query)
and the gap slices that ``--add-slice`` produces. It writes ZERO Bible prose:
naming and filling gaps is its whole job; synthesis is someone else's.

GRACEFUL exit-21: a gap fetch that trips the retrieval cap raises
``LedgerCapExceeded`` (propagated from slice_search as exit 21). We WRITE
``coverage_gaps.md`` reflecting the round's gaps FIRST, then stop the audit and
return ``LedgerCapExceeded.EXIT_CODE`` cleanly, the whole run is NEVER aborted;
a refusal here is a bounded stop, not a crash.

CHILD RETURN CODES: every shell-out's exit code is checked. A gap fetch: 21 is
the graceful cap-stop above; any OTHER non-zero (20 missing Exa key, 2 bad args,
or a crash) is NOT a successful fetch, so the audit records the reason in
``coverage_gaps.md`` + stderr and returns that code. The evidence gate: only 0
(pass) is a clean result; 22 (thin corpus / malformed rows) means the corpus is
still NOT gate-passing, so it is surfaced and returned as a nonzero audit result
(exit 0 is required before synthesis, and a thin re-gate has not earned it); any
other non-zero also stops the audit and is returned.

FAIL CLOSED: a REQUIRED audit that cannot run must never look like a clean "no
gaps" pass. Three failure modes each return a DISTINCT nonzero exit code and
write a note, so the pipeline can tell "ran, found nothing" (exit 0) apart from
"could not run": no provider configured (AUDIT_NO_PROVIDER), the model call
raised (AUDIT_LLM_ERROR), or the JSON gap-list would not parse (AUDIT_BAD_JSON).

GROUNDING: gap judgment reads the retrieved CONTENT, not just brief titles, the
per-row ``highlights`` plus the full text under ``round1/<text_path>``, bounded
to a char budget so the prompt stays finite. This includes the academic-anchor
rows' highlights + text_path content (fetch_fulltext adds text_path to anchor
rows too), so scholarly coverage is never called absent when it was retrieved.

SLUGS ACROSS ROUNDS: gap slice names are prefixed with the round index
(``r{round}_{slug}``) so a gap that recurs in a later round gets a fresh slice
name and never overwrites (discards) an earlier round's slice. WITHIN round 1, a
gap whose ``slice_gap_<name>.jsonl`` already exists on disk (e.g. from a prior
exit-21 rerun that restarts at round 1) is SKIPPED, not re-fetched, so a rerun
never overwrites and discards evidence gathered by the earlier run.

--audit-usd is a FIXED additional headroom for the WHOLE audit, not a per-fetch
replenish and not a reset below the run cap. The audit ceiling is computed ONCE
at audit start as ``spend_at_start + audit_usd`` and held fixed for every fetch,
so total audit spend cannot exceed audit_usd by handing each gap fresh headroom.
The cap passed to slice_search is ``max(existing_run_cap, ceiling)`` so it can
never LOWER a previously chosen, higher run cap.

Usage:
  python3 scripts/coverage_audit.py --run-dir DIR --topic "..."
      [--max-audit-rounds N] [--audit-usd X]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Allow running both as `python3 scripts/coverage_audit.py` and `-m scripts.coverage_audit`.
# config + llm live at the skill ROOT, not scripts/, mirror slice_search.py:52-56.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
import llm
from scripts.ledger import LedgerCapExceeded, RetrievalLedger
from scripts.helper_runtime import resolve_helper_layout
from scripts.run_layout import LayoutKind

# slice_search returns this when a gap fetch trips the retrieval cap. We catch it
# to write coverage_gaps.md before stopping, rather than aborting the whole run.
LEDGER_CAP_EXIT = LedgerCapExceeded.EXIT_CODE

# evidence_gate's documented FAIL verdict (corpus too thin / malformed rows).
# This is NOT a clean result for the audit: exit 0 is required before synthesis,
# and a thin re-gate has not earned it, so a 22 is surfaced and returned as a
# nonzero audit result rather than being treated as a successful audit.
GATE_FAIL_EXIT = 22

# FAIL-CLOSED exit codes. A REQUIRED audit that cannot RUN must never resemble a
# clean "no gaps" pass (exit 0). Each of the three "could not run" failure modes
# returns its own distinct nonzero code and writes a note, so the pipeline can
# tell "ran, found nothing" apart from "could not run".
AUDIT_NO_PROVIDER = 30  # no LLM provider configured / config error: audit cannot run
AUDIT_LLM_ERROR = 31    # the model call raised: audit cannot run
AUDIT_BAD_JSON = 32     # the LLM reply's gap-list would not parse: audit cannot run


class _AuditCannotRun(Exception):
    """A required audit leg could not run (no provider, model raised, or the
    gap-list would not parse). Carries the distinct nonzero EXIT_CODE to return
    and a human message to record, so the audit fails CLOSED instead of looking
    like a clean no-gap pass."""

    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message

# Total chars of retrieved CONTENT (highlights + full text) folded into the
# auditor's context so its gap judgment is grounded in what was actually
# retrieved, not just titles. Bounds the prompt. Override with DR_AUDIT_MAX_CHARS.
CONTEXT_MAX_CHARS = int(os.environ.get("DR_AUDIT_MAX_CHARS", str(300_000)))

# Filesystem-safe slice-name stem for a gap. slice_search prefixes it with `gap_`.
_SAFE = re.compile(r"[^a-z0-9]+")


def _slug(text, fallback):
    """A short, filesystem-safe lowercase stem for a gap slice name."""
    s = _SAFE.sub("-", str(text).lower()).strip("-")
    s = "-".join(filter(None, s.split("-")))[:40].strip("-")
    return s or fallback


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _iter_slice_rows(round1):
    """Yield parsed row dicts from every round1/slice_*.jsonl file, INCLUDING the
    academic anchor: fetch_fulltext adds highlights + text_path to anchor rows
    too, so excluding it would make the auditor call scholarly coverage absent
    when it was in fact retrieved. Best-effort: a missing or malformed file/line
    contributes nothing rather than raising."""
    for path in sorted(round1.glob("slice_*.jsonl")):
        text = _read_text(path)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _load_context(run_dir, max_chars=CONTEXT_MAX_CHARS):
    """Gather the auditor's read-only inputs and GROUND them in retrieved content.

    The briefs carry only titles/urls/dates; judging gaps from those alone means
    judging from model memory, not the corpus. So this ALSO reads, per slice row
    (the academic-anchor rows INCLUDED, since fetch_fulltext gives them a
    text_path too):
      * the row's ``highlights`` (list of extracted snippets), and
      * the retrieved full text at ``round1/<text_path>`` (sources/*.txt),
    accumulating until ``max_chars`` of CONTENT is reached (the bound that keeps
    the prompt finite).

    Returns a single context string. Every part is best-effort: a missing file
    contributes nothing rather than raising, the auditor must never die reading."""
    run_dir = Path(run_dir)
    layout = resolve_helper_layout(run_dir)
    round1 = layout.round1
    parts = []

    scope = _read_text(layout.scope)
    if scope.strip():
        parts.append("## scope.json\n" + scope.strip())

    briefs = sorted(round1.glob("brief_*.md"))
    for bp in briefs:
        body = _read_text(bp).strip()
        if body:
            parts.append(f"## {bp.name}\n" + body)

    # RETRIEVED CONTENT: highlights + full text per slice row, bounded by budget.
    content_parts = []
    used = 0
    budget_hit = False
    for row in _iter_slice_rows(round1):
        if used >= max_chars:
            budget_hit = True
            break
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        header = f"### {title or 'untitled'} — {url}".strip()
        chunk_lines = [header]

        highlights = row.get("highlights")
        if isinstance(highlights, list):
            for h in highlights:
                h = str(h).strip()
                if h:
                    chunk_lines.append(f"- {h}")

        # Full extracted page/PDF text, spilled by slice_search to sources/<file>.
        tp = row.get("text_path")
        if tp:
            source_path = layout.run_root / tp if layout.kind is LayoutKind.V2 else round1 / tp
            body = _read_text(source_path).strip()
            if body:
                chunk_lines.append(body)

        chunk = "\n".join(chunk_lines).strip()
        if chunk == header:
            # No highlights and no full text on this row — nothing to ground with.
            continue
        remaining = max_chars - used
        if len(chunk) > remaining:
            chunk = chunk[:remaining].rstrip()
            budget_hit = True
        content_parts.append(chunk)
        used += len(chunk)
        if budget_hit:
            break

    if content_parts:
        note = (" (truncated to the content budget)" if budget_hit else "")
        parts.append(
            f"## retrieved content (highlights + full text){note}\n"
            + "\n\n".join(content_parts)
        )

    return "\n\n".join(parts)


def _get_provider():
    """Resolve the utility provider for the audit LLM legs.

    FAIL CLOSED: if config is unavailable or no provider is configured, a REQUIRED
    audit cannot run. Raise ``_AuditCannotRun(AUDIT_NO_PROVIDER, ...)`` so the
    caller returns a distinct nonzero code and writes a note, rather than
    degrading to a "no gaps" pass that would look like a clean, verified run."""
    try:
        paths = config.default_toml_paths()
        env = config.load_env_files()
        providers, _ = config.load_config(paths, env)
        defaults = config.load_defaults(paths)
        provider = config.pick_provider(providers, "utility", defaults)
    except Exception as e:  # noqa: BLE001, config error is a "cannot run", not a pass
        raise _AuditCannotRun(
            AUDIT_NO_PROVIDER,
            f"config error resolving the audit provider ({type(e).__name__}: {e}); "
            f"the audit could not run and this is NOT a clean no-gap pass.") from e
    if provider is None:
        raise _AuditCannotRun(
            AUDIT_NO_PROVIDER,
            "no LLM provider configured for the audit; the audit could not run and "
            "this is NOT a clean no-gap pass.")
    return provider


def _extract_json(text):
    """Pull the first balanced top-level JSON object out of an LLM reply, tolerant
    of markdown fences. Returns the parsed dict, or None on failure."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def enumerate_gaps(provider, topic, context):
    """Ask the LLM for expected-but-absent coverage for THIS scope. Returns a list
    of {"gap": str, "query": str}.

    FAIL CLOSED: a model error raises ``_AuditCannotRun(AUDIT_LLM_ERROR, ...)`` and
    an unparseable gap-list raises ``_AuditCannotRun(AUDIT_BAD_JSON, ...)``. Neither
    is an empty gap list: an audit that could not RUN must not resemble a clean
    "no gaps" pass. A genuine, well-formed empty list (``{"gaps": []}``) is a real
    "ran, found nothing" result and returns [] normally."""
    system = (
        "You are a research coverage auditor. Given a topic, its scope, and a "
        "digest of the evidence already retrieved, identify coverage that a "
        "competent reader of THIS scope would expect but that is ABSENT from the "
        "retrieved evidence. Do NOT restate what is already covered. Stay strictly "
        "within the given scope: do not invent adjacent topics. For each gap give "
        "one concrete, scope-bounded web search query that would fill it. "
        'Output JSON only: {"gaps": [{"gap": str, "query": str}]}. '
        "Return an empty gaps list if coverage is adequate."
    )
    user = (
        f"Topic: {topic}\n\n"
        f"Evidence retrieved so far:\n{context}\n\n"
        "Return JSON only."
    )
    try:
        text = llm.call_model(provider, system, user)
    except Exception as e:  # noqa: BLE001, model error is "cannot run", not a pass
        raise _AuditCannotRun(
            AUDIT_LLM_ERROR,
            f"the audit model call raised ({type(e).__name__}: {e}); the audit "
            f"could not run and this is NOT a clean no-gap pass.") from e
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        raise _AuditCannotRun(
            AUDIT_BAD_JSON,
            "the audit model reply had no parseable JSON object; the audit could "
            "not run and this is NOT a clean no-gap pass.")
    gaps = obj.get("gaps")
    if not isinstance(gaps, list):
        raise _AuditCannotRun(
            AUDIT_BAD_JSON,
            "the audit model reply parsed but carried no 'gaps' list; the audit "
            "could not run and this is NOT a clean no-gap pass.")
    out = []
    for g in gaps:
        if not isinstance(g, dict):
            continue
        gap = (g.get("gap") or "").strip()
        query = (g.get("query") or "").strip()
        if gap and query:
            out.append({"gap": gap, "query": query})
    return out


def gaps_remain(provider, topic, context):
    """Ask the LLM whether material gaps remain after the latest fetch round.
    Returns True/False.

    FAIL CLOSED: a model error or an unparseable reply here would otherwise be
    read as "no gaps remain" and END the loop as a successful audit, which is the
    same fail-open trap. So a model error raises AUDIT_LLM_ERROR and an
    unparseable reply raises AUDIT_BAD_JSON, rather than silently returning
    False."""
    system = (
        "You are a research coverage auditor. Given a topic, its scope, and a "
        "digest of the evidence retrieved so far, decide whether MATERIAL coverage "
        "gaps remain within THIS scope. Answer with JSON only: "
        '{"material_gaps_remain": true|false}.'
    )
    user = (
        f"Topic: {topic}\n\n"
        f"Evidence retrieved so far:\n{context}\n\n"
        "Return JSON only."
    )
    try:
        text = llm.call_model(provider, system, user)
    except Exception as e:  # noqa: BLE001, model error is "cannot run", not a pass
        raise _AuditCannotRun(
            AUDIT_LLM_ERROR,
            f"the gaps-remain model call raised ({type(e).__name__}: {e}); the "
            f"audit could not run and this is NOT a clean no-gap pass.") from e
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        raise _AuditCannotRun(
            AUDIT_BAD_JSON,
            "the gaps-remain model reply had no parseable JSON object; the audit "
            "could not run and this is NOT a clean no-gap pass.")
    return bool(obj.get("material_gaps_remain"))


def _write_gaps(round1_dir, round_no, gaps):
    """Write round1/coverage_gaps.md: each gap + its scope-bounded query. This is
    the ONLY prose the auditor produces (no Bible text). Written BEFORE any fetch
    so a cap-trip mid-fetch still leaves the round's gaps recorded on disk."""
    lines = [f"# Coverage audit, gaps (round {round_no})", ""]
    if not gaps:
        lines.append("_(no material gaps found for this scope)_")
    for i, g in enumerate(gaps, 1):
        lines.append(f"## Gap {i}")
        lines.append(g["gap"])
        lines.append("")
        lines.append(f"**Query**: {g['query']}")
        lines.append("")
    round1_dir.mkdir(parents=True, exist_ok=True)
    (round1_dir / "coverage_gaps.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_failure(round1_dir, message):
    """Append a failure notice to coverage_gaps.md so a stopped audit records WHY
    it stopped on disk (not just on stderr). Best-effort: creates the file if the
    round's _write_gaps has not run yet."""
    round1_dir.mkdir(parents=True, exist_ok=True)
    path = round1_dir / "coverage_gaps.md"
    prior = ""
    try:
        prior = path.read_text(encoding="utf-8")
    except OSError:
        prior = ""
    block = f"\n## Audit stopped: failure\n\n{message}\n"
    path.write_text(prior + block, encoding="utf-8")


def _current_spend(run_dir):
    """Read the run's committed retrieval spend from retrieval_ledger.json.

    Used to turn --audit-usd into HEADROOM ABOVE existing spend rather than a
    reset of the cap. Best-effort: an absent/unreadable ledger reads as $0 so the
    audit can still run (slice_search re-validates the cap itself on charge)."""
    path = resolve_helper_layout(run_dir).ledger
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        total = 0.0
        for e in data.get("entries") or []:
            actual = e.get("actual_usd")
            wc = e.get("worst_case_usd")
            total += float(wc if actual is None else actual)
        return total
    except (OSError, ValueError, TypeError):
        return 0.0


def _current_run_cap(run_dir):
    """Read the run's own configured cap (``cap_usd``) from retrieval_ledger.json,
    or None if there is no ledger / it is unreadable / the value is not usable.

    Used so the audit never passes slice_search a --max-retrieval-usd LOWER than
    the run's existing cap. Best-effort by design: a None here means "cap unknown",
    and the caller then falls back to never letting audit_usd cut below spend."""
    path = resolve_helper_layout(run_dir).ledger
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cap = data.get("cap_usd")
        if cap is None:
            return None
        cap = float(cap)
    except (OSError, ValueError, TypeError):
        return None
    if cap != cap or cap < 0.0:  # NaN or negative is not a usable cap
        return None
    return cap


def _audit_ceiling(spend_at_start, run_cap, audit_usd):
    """Compute the FIXED --max-retrieval-usd to pass to every gap fetch.

    --audit-usd is a FIXED additional headroom for the WHOLE audit, computed ONCE
    at audit start, NOT a per-fetch replenish. The audit ceiling is
    ``spend_at_start + audit_usd`` (a fixed total for the whole audit), so total
    audit spend can never exceed audit_usd by handing each gap fresh headroom.

    It also must never LOWER a previously chosen, higher run cap, so the value
    passed is ``max(run_cap, ceiling)`` when the run cap is known. When the run
    cap is unknown, the fallback still never cuts headroom below what is already
    spent (the ceiling is already >= spend_at_start because audit_usd >= 0).

    Returns None when audit_usd is None (leave the run's own cap untouched)."""
    if audit_usd is None:
        return None
    ceiling = spend_at_start + audit_usd
    if run_cap is not None:
        ceiling = max(run_cap, ceiling)
    return ceiling


def _resolve_cap(run_dir, audit_usd):
    """Compute the audit's FIXED --max-retrieval-usd ONCE, at audit start.

    Reads the spend-at-start and the run's own cap from the ledger, then returns
    ``max(run_cap, spend_at_start + audit_usd)`` (see _audit_ceiling). This is
    called ONCE and the result held fixed for every fetch in the audit, so
    --audit-usd is a fixed total headroom, never a per-fetch replenish, and never
    lowers a higher run cap. Returns None when audit_usd is None."""
    if audit_usd is None:
        return None
    return _audit_ceiling(_current_spend(run_dir), _current_run_cap(run_dir), audit_usd)


def _fetch_gap(run_dir, topic, name, query, cap_usd):
    """Fire one gap query as an ad-hoc slice via slice_search.py --add-slice.

    Shells out so the gap slice is ledger-charged and written to
    round1/slice_gap_<name>.jsonl by the exact same code path as a roster slice.
    ``cap_usd`` is the FIXED audit ceiling (max(run_cap, spend_at_start +
    audit_usd), see _resolve_cap), computed ONCE at audit start and passed
    unchanged to every fetch, or None to leave the run cap untouched.
    Returns the child's exit code. Exit 21 (cap) is surfaced by the caller as a
    LedgerCapExceeded, so the audit can stop gracefully; any other non-zero is a
    hard failure the caller surfaces and returns."""
    cmd = [
        sys.executable, str(Path(_ROOT) / "scripts" / "slice_search.py"),
        "--add-slice", name, "--query", query,
        "--run-dir", str(run_dir), "--topic", topic,
    ]
    if cap_usd is not None:
        cmd += ["--max-retrieval-usd", str(cap_usd)]
    proc = subprocess.run(cmd)
    return proc.returncode


def _run_evidence_gate(run_dir):
    """Re-run the evidence gate over the (now larger) corpus. Only exit 0 (pass)
    is a clean result. Exit 22 (thin corpus / malformed rows) means the corpus is
    still NOT gate-passing, so the caller surfaces it and returns it as a nonzero
    audit result (exit 0 is required before synthesis, and a thin re-gate has not
    earned it). Any OTHER non-zero (bad args, crash) is also surfaced and returned.
    Returns the gate's exit code."""
    cmd = [
        sys.executable, str(Path(_ROOT) / "scripts" / "evidence_gate.py"),
        "--run-dir", str(run_dir),
    ]
    proc = subprocess.run(cmd)
    return proc.returncode


def _safe_emit_matrix(use_matrix, run_dir, round1_dir, current_year):
    """OPT-IN report emitter for --use-matrix. A no-op unless ``use_matrix`` is
    set, so the whole opt-in decision lives here (call sites stay unconditional).
    When enabled: reads the run's scope.json (best-effort), builds the coverage
    matrix from the round1 slice rows, and writes round1/coverage_matrix.json +
    one status line. The ENTIRE body is wrapped so it can raise nothing and
    returns nothing — it can never change the audit's return code. Called only on
    the audit's success paths."""
    if not use_matrix:
        return
    try:
        from scripts import coverage_matrix_adapter as cma  # lazy: keep module import light

        scope_path = resolve_helper_layout(run_dir).scope
        try:
            payload = json.loads(_read_text(scope_path)) if scope_path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
        except (json.JSONDecodeError, OSError):
            payload = {}

        matrix = cma.build_matrix(payload, _iter_slice_rows(round1_dir), current_year)
        report = cma.matrix_report(matrix)
        (Path(round1_dir) / "coverage_matrix.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        print(f"coverage-matrix: status={report['status']} "
              f"empty={len(report['empty_cells'])} "
              f"single_primary={len(report['single_primary_cells'])}")
    except Exception as exc:  # never let the opt-in report perturb the audit
        # The notice print is itself guarded: a broken/closed stderr (e.g. the
        # CLI piped into `head`) must not turn this handler's own print into an
        # escaping BrokenPipeError that would change the audit's return code.
        try:
            print(f"coverage-matrix: skipped ({exc})", file=sys.stderr)
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--max-audit-rounds", type=int, default=2)
    ap.add_argument("--audit-usd", type=float, default=None,
                    help="FIXED extra retrieval headroom (USD) for the WHOLE audit's "
                         "gap fetches. The audit ceiling is computed ONCE at start as "
                         "max(run_cap, spend_at_start + audit_usd) and passed to every "
                         "fetch unchanged, so it is a fixed total, never a per-fetch "
                         "replenish, and never lowers a higher run cap. Omit to leave "
                         "the run's own cap untouched.")
    ap.add_argument("--use-matrix", action="store_true",
                    help="OPT-IN, default off: after a successful audit, also emit "
                         "round1/coverage_matrix.json (the coverage-matrix report). "
                         "Never changes the audit's behavior or return code.")
    ap.add_argument("--current-year", type=int, default=None,
                    help="Year used for source age in the --use-matrix report. "
                         "No run artifact carries a date; omit → ages default to 0. "
                         "Passed in (never datetime.now()) to keep this module "
                         "clock-free.")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    layout = resolve_helper_layout(run_dir)
    round1_dir = layout.round1
    round1_dir.mkdir(parents=True, exist_ok=True)

    # FAIL CLOSED: every leg that "cannot run" (no provider, model raised, bad
    # JSON) raises _AuditCannotRun with its own distinct nonzero exit code. Catch
    # it here, record WHY on disk, and return that code, so a required audit that
    # could not run never resembles a clean "no gaps" pass (exit 0).
    try:
        provider = _get_provider()

        # The audit ceiling is computed ONCE, here at audit start, and held fixed
        # for every fetch. --audit-usd is a fixed additional headroom for the WHOLE
        # audit (max(run_cap, spend_at_start + audit_usd)), NOT a per-fetch
        # replenish that would hand each gap fresh headroom and let total audit
        # spend exceed audit_usd, and NOT a value that can lower a higher run cap.
        audit_cap_usd = _resolve_cap(run_dir, args.audit_usd)

        for round_no in range(1, args.max_audit_rounds + 1):
            context = _load_context(run_dir)
            gaps = enumerate_gaps(provider, args.topic, context)

            # Record this round's gaps on disk FIRST, before any fetch, so a
            # cap-trip mid-fetch still leaves coverage_gaps.md reflecting them.
            _write_gaps(round1_dir, round_no, gaps)

            if not gaps:
                print(f"Coverage audit round {round_no}: no material gaps, stopping.")
                _safe_emit_matrix(args.use_matrix, run_dir, round1_dir, args.current_year)
                return 0

            seen_names = set()
            try:
                for g in gaps:
                    # Prefix the slug with the ROUND index so a gap that recurs in
                    # a later round gets its own slice name (r2_<slug>) and never
                    # overwrites round 1's slice_gap_r1_<slug>.jsonl, discarding
                    # its evidence. Uniqueness is then enforced within the round.
                    name = f"r{round_no}_{_slug(g['gap'], 'gap')}"
                    base = name
                    n = 2
                    while name in seen_names:
                        name = f"{base}-{n}"
                        n += 1
                    seen_names.add(name)

                    # CROSS-RERUN GUARD: after an exit-21 cap-stop, the documented
                    # rerun restarts at round 1 and re-derives the SAME r1_<slug>
                    # names. If this gap's slice_gap_<name>.jsonl already exists on
                    # disk, a re-fetch would OVERWRITE and discard the earlier
                    # run's evidence. So SKIP it: log it as already-present and
                    # move on, preserving the prior slice.
                    existing = round1_dir / f"slice_gap_{name}.jsonl"
                    if existing.exists():
                        print(f"  coverage-audit: gap '{name}' already fetched "
                              f"({existing.name} on disk), skipping to preserve "
                              f"prior evidence.", file=sys.stderr)
                        continue

                    # cap_usd is the FIXED audit ceiling computed ONCE above and
                    # passed unchanged to every fetch (never recomputed per fetch).
                    rc = _fetch_gap(run_dir, args.topic, name, g["query"], audit_cap_usd)
                    if rc == LEDGER_CAP_EXIT:
                        # Cap tripped inside the child. Raise so the graceful
                        # handler below stops the audit without aborting the run.
                        raise LedgerCapExceeded(
                            f"gap fetch '{name}' hit the retrieval cap (exit {LEDGER_CAP_EXIT})")
                    if rc != 0:
                        # Any OTHER non-zero from a fetch (exit 20 missing Exa key,
                        # exit 2 bad args, or an unhandled crash) is NOT a
                        # successful fetch. Never treat it as success: record why,
                        # then stop the audit and return the child's code.
                        msg = (f"gap fetch '{name}' failed (slice_search exit {rc}). "
                               f"Query: {g['query']}. Audit stopped; this is not a "
                               f"successful fetch.")
                        print(f"  coverage-audit: {msg}", file=sys.stderr)
                        _append_failure(round1_dir, msg)
                        return rc
            except LedgerCapExceeded as exc:
                # coverage_gaps.md is already on disk (written above). Stop cleanly.
                print(f"  coverage-audit: retrieval cap reached: {exc}", file=sys.stderr)
                print("    coverage_gaps.md is written. Raise --audit-usd to continue.",
                      file=sys.stderr)
                return LedgerCapExceeded.EXIT_CODE

            # Re-score the (now larger) corpus, then ask the LLM if gaps remain.
            gate_rc = _run_evidence_gate(run_dir)
            if gate_rc != 0:
                # Only exit 0 (pass) is a clean re-gate. Exit 22 (thin corpus /
                # malformed rows) means the corpus is STILL not gate-passing, so it
                # is NOT a successful audit: exit 0 is required before synthesis.
                # Any other non-zero (bad args, crash) is likewise surfaced.
                if gate_rc == GATE_FAIL_EXIT:
                    msg = (f"evidence gate re-run returned exit {gate_rc} (corpus "
                           f"still too thin or a row failed re-validation). The "
                           f"corpus is NOT gate-passing, so the audit is not a "
                           f"success; exit 0 is required before synthesis.")
                else:
                    msg = (f"evidence gate failed with exit {gate_rc} (not a pass). "
                           f"Audit stopped.")
                print(f"  coverage-audit: {msg}", file=sys.stderr)
                _append_failure(round1_dir, msg)
                return gate_rc

            context = _load_context(run_dir)
            if not gaps_remain(provider, args.topic, context):
                print(f"Coverage audit round {round_no}: gaps filled, none remain, stopping.")
                _safe_emit_matrix(args.use_matrix, run_dir, round1_dir, args.current_year)
                return 0

        _safe_emit_matrix(args.use_matrix, run_dir, round1_dir, args.current_year)
        print(f"Coverage audit: reached --max-audit-rounds ({args.max_audit_rounds}), stopping.")
        return 0

    except _AuditCannotRun as exc:
        print(f"  coverage-audit: {exc.message}", file=sys.stderr)
        _append_failure(
            round1_dir,
            f"Audit could not RUN (exit {exc.exit_code}): {exc.message} "
            f"This is NOT a clean no-gap pass.")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
