#!/usr/bin/env python3
"""
cost.py — pre-flight cost estimator and budget gate.

Pricing is read from each Provider's .pricing dict (set via config.py /
TOML config).  These are best-effort estimates — check provider websites
for current rates.  The tool is conservative (rounds up) and adds a 15%
safety margin.

Usage as a library (called from dispatch.py):
  from scripts.cost import estimate_run, enforce_budget

Usage as a CLI (run from the repo root):
  python3 -m scripts.cost --models claude,chatgpt,gemini --prompt-words 1500 --output-words 25000
"""

import argparse
import sys

import config


# Rough token-per-word ratio for English prose
TOKENS_PER_WORD = 1.35
SAFETY_MARGIN = 1.15


def words_to_tokens(words: int) -> int:
    return int(words * TOKENS_PER_WORD)


def estimate_assignment(agent_type, provider, prompt_words, output_words):
    if provider.api_type == "cli":
        return {"agent_type": agent_type, "provider": provider.name, "excluded": True,
                "note": "subscription — no metered API cost", "total": 0.0}
    p = provider.pricing
    if not p or "in" not in p or "out" not in p:
        return {"agent_type": agent_type, "provider": provider.name, "excluded": True,
                "note": "unknown pricing — excluded from estimate", "total": 0.0}
    in_tokens = words_to_tokens(prompt_words)
    out_tokens = words_to_tokens(output_words)
    in_cost = (in_tokens / 1_000_000) * p["in"]
    out_cost = (out_tokens / 1_000_000) * p["out"]
    extra = 0.0
    if "reasoning" in p:
        extra += (out_tokens / 1_000_000) * p["reasoning"]
    if "searches_per_run" in p and "search_per_k" in p:
        extra += (p["searches_per_run"] / 1000) * p["search_per_k"]
    total = (in_cost + out_cost + extra) * SAFETY_MARGIN
    return {"agent_type": agent_type, "provider": provider.name, "model": provider.model,
            "in_cost": round(in_cost, 3), "out_cost": round(out_cost, 3),
            "extra": round(extra, 3), "total": round(total, 3)}


def estimate_run(assignments, providers, prompt_words, output_words):
    rows = [estimate_assignment(at, providers[pname], prompt_words, output_words)
            for at, pname in assignments.items()]
    total = round(sum(r.get("total", 0) for r in rows), 2)
    return {"per_agent": rows, "total": total}


def format_report(estimate: dict) -> str:
    lines = [
        f"Cost estimate (Round 1 only — Rounds 2–5 add ~50–100% on top):",
        "",
    ]
    lines.append(f"  {'Agent':<20}{'Provider':<14}{'Model':<26}{'In $':>10}{'Out $':>10}{'Extra $':>10}{'Total $':>12}")
    lines.append("  " + "-" * 104)
    for r in estimate["per_agent"]:
        if r.get("excluded"):
            lines.append(f"  {r['agent_type']:<20}{r['provider']:<14}{r.get('note', 'unknown — excluded')}")
            continue
        lines.append(
            f"  {r['agent_type']:<20}{r['provider']:<14}{r.get('model', ''):<26}"
            f"{r['in_cost']:>10.2f}{r['out_cost']:>10.2f}{r.get('extra', 0):>10.2f}{r['total']:>12.2f}"
        )
    lines.append("  " + "-" * 104)
    lines.append(f"  {'Round 1 total':<82}{'':>8}{estimate['total']:>12.2f}")
    lines.append("")
    lines.append(
        f"  Rounds 2–5 (comparison + integration + fact-check + optional deepening) "
        f"typically add 50–100% — budget {estimate['total']*1.5:.2f}–{estimate['total']*2.0:.2f} USD total."
    )
    return "\n".join(lines)


def enforce_budget(estimate: dict, max_cost: float | None, prompt=True) -> bool:
    """Returns True if run should proceed, False otherwise."""
    full_estimate = estimate["total"] * 1.75
    if max_cost is not None and full_estimate > max_cost:
        print(f"\n  ✗ BUDGET EXCEEDED: estimated full-run cost ${full_estimate:.2f} > --max-cost-usd ${max_cost:.2f}", file=sys.stderr)
        print(f"    To proceed: re-run with --max-cost-usd {full_estimate:.0f} (or higher), or use --agents to trim.", file=sys.stderr)
        return False
    if prompt and estimate["total"] >= 5.0:
        try:
            ans = input(f"\n  Estimated Round 1 cost: ${estimate['total']:.2f} (full run ~${full_estimate:.2f}). Proceed? [y/N] ").strip().lower()
            return ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(config.BUILTIN_PROVIDER_SPECS.keys()))
    ap.add_argument("--prompt-words", type=int, default=1500)
    ap.add_argument("--output-words", type=int, default=25000)
    ap.add_argument("--max-cost-usd", type=float)
    args = ap.parse_args()

    names = [m.strip() for m in args.models.split(",") if m.strip()]
    assignments = {}
    providers = {}
    errors = []
    for name in names:
        spec = config.BUILTIN_PROVIDER_SPECS.get(name)
        if spec is None:
            errors.append(name)
            continue
        assignments[name] = name
        providers[name] = config.Provider(
            name=name,
            api_type=spec["api_type"],
            api_key="cli",
            model=spec["model"],
            base_url=spec.get("base_url"),
            max_tokens=spec.get("max_tokens", 32768),
            capabilities=tuple(spec.get("capabilities", ())),
            pricing=spec.get("pricing"),
            fallback_models=tuple(spec.get("fallback_models", ())),
        )
    if errors:
        print(f"ERROR: unknown built-in provider name(s): {', '.join(errors)}", file=sys.stderr)
        print(f"  Known: {', '.join(config.BUILTIN_PROVIDER_SPECS.keys())}", file=sys.stderr)
        raise SystemExit(1)

    estimate = estimate_run(assignments, providers, args.prompt_words, args.output_words)
    print(format_report(estimate))
    if args.max_cost_usd is not None:
        full = estimate["total"] * 1.75
        status = "OK" if full <= args.max_cost_usd else "EXCEEDED"
        print(f"\n  Budget check vs --max-cost-usd {args.max_cost_usd:.2f}: {status} (est full ${full:.2f})")


if __name__ == "__main__":
    main()
