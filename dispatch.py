#!/usr/bin/env python3
"""
Deeper Research Dispatcher — thin slices entry point.

This is a slices-only orchestrator guide, not a runner. It validates the run mode,
preflights the Exa retrieval key (`EXA_API_KEY`), and prints the ordered Round-1
retrieval command sequence (scope → slice_search → evidence_gate → citation_chase →
the default squad coverage audit that
the SKILL.md orchestrator executes. `coverage_audit.py` is fallback-only, plus a reminder that lint_background belongs in the
Round-4 checklist. Round-1 live retrieval is Exa slices; there is no legacy model fleet
and no built-in web-search provider.

Usage:
  python3 dispatch.py --topic "Grid battery storage" --scope "Full scope..." --run-dir ./run1/
  python3 dispatch.py --topic "..." --scope "..." --run-dir ./run1/ --max-retrieval-usd 8
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

import config as cfg
from scripts.helper_runtime import resolve_helper_layout
from scripts.run_layout import LayoutKind
from scripts.run_manager import ManagerError, prepare_run

# Make sibling scripts/ importable when run as a CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser(
        description="Deeper Research Dispatcher — slices-only Round-1 command guide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--topic", required=True, help="Research topic")
    parser.add_argument("--scope", required=True, help="Detailed scope description")
    parser.add_argument("--run-dir", help="Existing managed, legacy, or deliberately prepared manual run")
    parser.add_argument("--broker-endpoint", help="Existing v2 manager broker endpoint")
    parser.add_argument("--lease-token", help="Existing v2 manager lease token")
    parser.add_argument("--project-dir", help="Project in which to create research/<slug>/")
    parser.add_argument("--slug", help="Topic-qualified run name (defaults from the question)")
    parser.add_argument("--question", help="Research question (defaults to --topic)")
    parser.add_argument("--collision-mode", choices=["resume", "extend", "fresh", "cancel"])
    parser.add_argument("--mode", default="slices",
                        help="Run mode — only 'slices' is supported")
    parser.add_argument("--max-retrieval-usd", type=float,
                        help="Hard cap on Round-1 Exa retrieval spend (USD)")
    args = parser.parse_args()

    # Only the slices path exists — any other mode is a hard error.
    if args.mode != "slices":
        print(f"ERROR: unsupported --mode '{args.mode}'. Only 'slices' is supported.",
              file=sys.stderr)
        raise SystemExit(2)

    # Load env (.env merge) so EXA_API_KEY set in a file is honored.
    env = cfg.load_env_files()

    # Preflight the retrieval key — slices cannot run without Exa. A custom
    # EXA_BASE_URL (proxy/gateway) may authenticate out-of-band, in which
    # case a missing EXA_API_KEY is not fatal.
    if not env.get("EXA_API_KEY") and not env.get("EXA_BASE_URL"):
        print("ERROR: EXA_API_KEY is not set. Round-1 retrieval uses Exa slices; "
              "export EXA_API_KEY (or add it to ~/.env), or set EXA_BASE_URL to "
              "an Exa-compatible endpoint with out-of-band auth.", file=sys.stderr)
        raise SystemExit(20)

    broker_endpoint = lease_token = None
    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.is_dir():
            print("ERROR: --run-dir must already exist; use --project-dir for normal creation.",
                  file=sys.stderr)
            raise SystemExit(2)
        layout = resolve_helper_layout(run_dir)
        if layout.kind is LayoutKind.V2:
            if bool(args.broker_endpoint) != bool(args.lease_token):
                print("ERROR: --broker-endpoint and --lease-token must be supplied together.",
                      file=sys.stderr)
                raise SystemExit(2)
            if args.broker_endpoint:
                broker_endpoint, lease_token = args.broker_endpoint, args.lease_token
            else:
                try:
                    prepared = prepare_run(
                        question=args.question or args.topic,
                        slug=run_dir.name,
                        library_dir=run_dir.parent,
                        mode="resume",
                    )
                except ManagerError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    raise SystemExit(int(exc.exit_code))
                if prepared.action == "complete-noop":
                    print(f"Run is already complete: {run_dir}")
                    return
                broker_endpoint = prepared.broker_endpoint
                lease_token = prepared.lease_token
    else:
        try:
            prepared = prepare_run(
                question=args.question or args.topic,
                slug=args.slug,
                project_dir=args.project_dir,
                mode=args.collision_mode,
            )
        except ManagerError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise SystemExit(int(exc.exit_code))
        if prepared.action == "mode-required":
            print("ERROR: the run already exists; choose --collision-mode "
                  + ", ".join(prepared.choices), file=sys.stderr)
            raise SystemExit(12)
        if prepared.run_dir is None:
            print("Run creation cancelled.")
            return
        run_dir = prepared.run_dir
        layout = resolve_helper_layout(run_dir)
        broker_endpoint = prepared.broker_endpoint
        lease_token = prepared.lease_token

    cap_arg = []
    if args.max_retrieval_usd is not None:
        cap_arg = ["--max-retrieval-usd", str(args.max_retrieval_usd)]

    def _fmt(parts):
        return " ".join(shlex.quote(p) for p in parts)

    if layout.kind is LayoutKind.V2 and broker_endpoint and lease_token:
        def _managed(helper, payload):
            return ["python3", "scripts/run_manager.py", "invoke-helper",
                    "--broker-endpoint", broker_endpoint,
                    "--lease-token", lease_token,
                    "--helper", helper, "--args-json",
                    json.dumps(payload, separators=(",", ":"))]
        scope_cmd = ["python3", "scripts/run_manager.py", "invoke-helper",
                     "--broker-endpoint", broker_endpoint,
                     "--lease-token", lease_token,
                     "--helper", "scope", "--args-json", json.dumps({
                         "topic": args.topic, "scope": args.scope, "use_llm": False,
                     }, separators=(",", ":"))]
        slice_payload = {"topic": args.topic}
        if args.max_retrieval_usd is not None:
            slice_payload["max_retrieval_usd"] = args.max_retrieval_usd
        slice_cmd = _managed("slice-search", slice_payload)
        chase_cmd = _managed("citation-chase", {"topic": args.topic})
        audit_cmd = _managed("coverage-audit", {"topic": args.topic})
    else:
        scope_cmd = ["python3", "scripts/scope.py",
                     "--topic", args.topic, "--scope", args.scope,
                     "--output", str(layout.scope)]
        slice_cmd = ["python3", "scripts/slice_search.py",
                     "--topic", args.topic, "--run-dir", str(run_dir)] + cap_arg
        chase_cmd = ["python3", "scripts/citation_chase.py",
                     "--run-dir", str(run_dir), "--topic", args.topic]
        audit_cmd = ["python3", "scripts/coverage_audit.py",
                     "--run-dir", str(run_dir), "--topic", args.topic]
    gate_cmd = ["python3", "scripts/evidence_gate.py",
                "--run-dir", str(run_dir)]

    print("Round-1 retrieval command sequence (run in order):")
    print(f"  1. {_fmt(scope_cmd)}")
    print(f"  2. {_fmt(slice_cmd)}")
    print(f"  3. {_fmt(gate_cmd)}")
    print(f"  4. {_fmt(chase_cmd)}")
    print("     (citation_chase cascades OpenAlex → Semantic Scholar; if both fail it "
          "returns 0 in explicit degraded mode. Inspect round1/citation_chase_status.json "
          "and carry graph_verified=false into the final report. Exit 22 remains "
          "fail-closed.)")
    print("  5. DEFAULT coverage audit: follow references/squad-audit.md "
          "(checklist + isolated panel + per-gap verification, then re-gate).")
    print(f"     FALLBACK ONLY when native subagents are unavailable: {_fmt(audit_cmd)}")
    print("     (coverage_audit exit 0 = coverage verified; a NONZERO exit means "
          "the fallback audit could not complete or the corpus is still thin: do NOT "
          "proceed to synthesis, surface and resolve it.)")
    print("Round 4 checklist step: python3 scripts/lint_background.py "
          f"{shlex.quote(str(layout.sections))}")


if __name__ == "__main__":
    main()
