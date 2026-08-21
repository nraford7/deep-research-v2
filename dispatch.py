#!/usr/bin/env python3
"""
Deep Research Dispatcher — runs agent types in parallel, each via a configured provider.

Providers and agent types are defined via TOML config (~/.config/deep-research/config.toml
or ./deep-research.toml) and API keys loaded from ~/.env / .env.  Built-in providers
(claude, chatgpt, perplexity, gemini, grok) are activated automatically when their
API key environment variable is set.

Usage:
  python3 dispatch.py --topic "Oil trading" --scope "Full scope..." --output-dir ./round1/
  python3 dispatch.py --topic "AI safety" --scope "..." --output-dir ./round1/ --agents academic,contrarian
  python3 dispatch.py --topic "..." --scope "..." --output-dir ./round1/ --max-cost-usd 30 --resume
  python3 dispatch.py --topic "..." --scope "..." --output-dir ./round1/ --languages en,fr,de
  python3 dispatch.py --topic "..." --scope "..." --output-dir ./round1/ --scope-file round0/scope.json
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config as cfg
import llm

# Make sibling scripts/ importable when run as a CLI
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from scripts.cost import estimate_run, format_report, enforce_budget
except ImportError:
    estimate_run = format_report = enforce_budget = None


SHARED_RULES = """
## Non-Negotiable Research Rules
1. NEVER fabricate sources, URLs, DOIs, ISBNs, or quotes. If you cannot verify, mark "UNVERIFIED".
2. Every factual claim must have an inline citation [Author, Year] or [Source, Year].
3. If you cannot verify a source URL, mark it as "URL unverified" — do not invent one.
4. Prefer primary and institutional sources over Wikipedia.
5. When a topic is contested in the literature, present both sides with sources.
6. Include a full bibliography at the end, organized by category (Academic / Institutional / Books / Primary Sources).
7. Write in flat, factual prose. No hedging, no filler, no "in conclusion", no "it is worth noting".
8. Target 15,000-30,000 words. Be exhaustive. Cover every subtopic in depth.
9. DATE STAMPING: any claim with a year, statistic, or "current" qualifier must carry "[as of: <date>]".
   If you do not know the as-of date, write "[as of: unknown]" — do not guess.
10. CONFIDENCE: For high-stakes empirical claims, append "[confidence: high/medium/low]" with a one-line reason
    (e.g., "[confidence: medium — single source, no replication]").
"""



def build_prompt(topic, scope, strategy, languages=None, domain_priorities=None):
    lang_clause = ""
    if languages and languages != ["en"]:
        non_en = [l for l in languages if l != "en"]
        if non_en:
            lang_clause = (
                "\n\n## Multilingual sourcing\n"
                f"In addition to English sources, search for high-quality sources in: {', '.join(non_en)}.\n"
                "Cite original-language titles. Translate critical quotes into English and indicate the original language.\n"
                "Do not skip non-English primary sources just because English summaries exist.\n"
            )

    domain_clause = ""
    if domain_priorities:
        domain_clause = "\n\n## Domain-specific source priorities\n" + domain_priorities + "\n"

    return f"""You are producing a fact-checked, evidence-based deep research report.

## Topic
{topic}

## Scope
{scope}
{domain_clause}{lang_clause}

## Research Strategy
{strategy}

{SHARED_RULES}

## Output Format
- Start with an executive summary (200-300 words)
- Organize into clear sections with subsections
- Use inline citations: [Author, Year] or [Source, Year]
- Apply [as of: <date>] to time-sensitive claims
- Apply [confidence: ...] to high-stakes empirical claims
- End with a complete bibliography organized by category
- Include URLs for every source where possible
"""


def agent_filename(agent_type_name):
    return f"agent-{agent_type_name}.md"


def run_agent(provider, agent_type, prompt, output_path):
    start = time.time()
    text = llm.call_model(provider, agent_type.system_prompt, prompt)
    output_path.write_text(text, encoding="utf-8")
    return {"agent_type": agent_type.name, "provider": provider.name, "model": provider.model,
            "status": "ok", "words": len(text.split()), "seconds": round(time.time() - start, 1),
            "file": str(output_path)}


def run_job(topic, scope, output_dir, providers, agents, languages, domain_priorities,
            resume, seed):
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"

    existing = {}
    if resume and manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    prior_assignments = existing.get("assignments") if resume else None
    assignments, warnings = cfg.resolve_assignments(agents, providers, seed=seed,
                                                     prior_assignments=prior_assignments)
    for w in warnings:
        print(f"  ! {w}", flush=True)

    prior_ok = {r["agent_type"] for r in existing.get("results", [])
                if isinstance(r, dict) and r.get("status") == "ok" and "agent_type" in r} if resume else set()

    sems = {n: threading.Semaphore(p.max_concurrency) for n, p in providers.items() if p.max_concurrency}

    def _guarded(provider, agent_type, prompt, path):
        sem = sems.get(provider.name)
        if sem: sem.acquire()
        try:
            return run_agent(provider, agent_type, prompt, path)
        finally:
            if sem: sem.release()

    todo = []
    for name, at in agents.items():
        path = output_dir / agent_filename(name)
        if resume and name in prior_ok and path.exists() and path.stat().st_size > 4000:
            continue
        todo.append((name, at, path))

    degraded = {name for name in agents
                if agents[name].requires_web_search
                and "web_search" not in providers[assignments[name]].capabilities}

    todo_names = {t[0] for t in todo}
    skipped_names = [name for name in agents if name not in todo_names]
    if resume and skipped_names:
        print(f"Resume: skipping {len(skipped_names)} agent type(s) with existing output: "
              f"{', '.join(skipped_names)}", flush=True)
    if todo:
        print(f"Dispatching {len(todo)} agent type(s) in parallel: "
              f"{', '.join(name for name, _, _ in todo)}", flush=True)
    else:
        print("All selected agent types already have output (resume). Nothing to run.", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        futs = {}
        for name, at, path in todo:
            provider = providers[assignments[name]]
            prompt = build_prompt(topic, scope, at.strategy, languages=languages,
                                  domain_priorities=domain_priorities)
            futs[ex.submit(_guarded, provider, at, prompt, path)] = name
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                r = fut.result(); results.append(r)
                if r.get("status") == "ok" and name in degraded:
                    fp = Path(r["file"])
                    fp.write_text("> [no live web search — knowledge-cutoff results]\n\n"
                                  + fp.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"  ✓ {name}: {r['words']} words via {r['provider']} → {r['file']}", flush=True)
            except Exception as e:
                results.append({"agent_type": name, "provider": assignments[name], "status": "error",
                                "error": str(e)})
                print(f"  ✗ {name}: {e}", flush=True)

    # Tag degraded agents that were skipped on resume (their existing file wasn't re-run).
    for name in degraded:
        if name in todo_names:
            continue
        fp = output_dir / agent_filename(name)
        if fp.exists():
            text = fp.read_text(encoding="utf-8")
            if not text.startswith("> [no live web search"):
                fp.write_text("> [no live web search — knowledge-cutoff results]\n\n" + text,
                              encoding="utf-8")

    by_type = {r["agent_type"]: r for r in existing.get("results", []) if isinstance(r, dict) and "agent_type" in r}
    for r in results:
        by_type[r["agent_type"]] = r
    manifest = {"topic": topic, "scope": scope, "languages": languages,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "assignments": assignments, "warnings": warnings,
                "results": list(by_type.values())}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    new_ok = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "ok")
    new_words = sum(r.get("words", 0) for r in results if isinstance(r, dict) and r.get("status") == "ok")
    total_ok = sum(1 for r in manifest["results"] if isinstance(r, dict) and r.get("status") == "ok")
    print(f"\n{'='*50}", flush=True)
    print(f"Done: {new_ok} newly run ({new_words:,} words), "
          f"{len(skipped_names)} skipped; {total_ok}/{len(agents)} agent types complete.", flush=True)
    print(f"Manifest: {manifest_path}", flush=True)

    return manifest


def load_scope_brief(scope_file):
    if not scope_file:
        return None
    path = Path(scope_file)
    if not path.exists():
        print(f"  warn: --scope-file {scope_file} not found, ignoring", file=sys.stderr)
        return None
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return path.read_text(encoding="utf-8")
        lines = []
        if data.get("primary_domain"):
            lines.append(f"Primary domain: {data['primary_domain']}")
        if data.get("ranked_domains"):
            lines.append(f"Ranked domains: {', '.join(data['ranked_domains'])}")
        # LLM proposal takes precedence — it's specifically tuned for this topic.
        prop = data.get("llm_proposal")
        prio = (prop or {}).get("priority_sources") or data.get("priority_sources") or []
        against = (prop or {}).get("weight_against") or data.get("weight_against") or []
        must = (prop or {}).get("must_check") or data.get("must_check") or ""
        if prio:
            lines.append("Priority sources:")
            for s in prio:
                lines.append(f"- {s}")
        if against:
            lines.append("Weight against:")
            for s in against:
                lines.append(f"- {s}")
        if must:
            lines.append(f"Must check: {must}")
        return "\n".join(lines) if lines else None
    return path.read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Deep Research Dispatcher — multi-model parallel research",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--topic", required=True, help="Research topic")
    parser.add_argument("--scope", required=True, help="Detailed scope description")
    parser.add_argument("--output-dir", required=True, help="Output directory for round 1 files")
    parser.add_argument("--agents", default="all", help="Comma-separated agent types to run, or 'all'")
    parser.add_argument("--languages", default="en", help="Search languages, e.g. en,fr,de,zh")
    parser.add_argument("--scope-file", help="JSON or markdown from scripts/scope.py — injects domain priorities")
    parser.add_argument("--max-cost-usd", type=float, help="Hard cap on estimated full-run cost")
    parser.add_argument("--resume", action="store_true",
                        help="Skip agent types whose output file already exists in --output-dir")
    parser.add_argument("--no-confirm", action="store_true", help="Skip the interactive cost prompt")
    parser.add_argument("--estimate-only", action="store_true", help="Print cost estimate and exit")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    languages = [l.strip() for l in args.languages.split(",") if l.strip()]
    domain_priorities = load_scope_brief(args.scope_file)

    env = cfg.load_env_files()
    providers, agents = cfg.load_config(cfg.default_toml_paths(), env)

    if args.agents != "all":
        wanted = [a.strip() for a in args.agents.split(",") if a.strip()]
        unknown = [a for a in wanted if a not in agents]
        if unknown:
            raise SystemExit(f"Unknown agent types: {', '.join(unknown)}")
        agents = {k: v for k, v in agents.items() if k in wanted}

    if not providers:
        print("ERROR: No providers available. Set at least one API key or define a [providers.*] block.", file=sys.stderr)
        raise SystemExit(1)

    if not agents:
        raise SystemExit("No agent types selected — check the --agents filter.")

    # Cost preflight
    if estimate_run is not None:
        manifest_path = output_dir / "manifest.json"
        preflight_prior = None
        if args.resume and manifest_path.exists():
            try:
                preflight_prior = json.loads(manifest_path.read_text(encoding="utf-8")).get("assignments")
            except json.JSONDecodeError:
                pass
        preflight_assignments, _ = cfg.resolve_assignments(agents, providers, seed=0,
                                                            prior_assignments=preflight_prior)
        # Build a sample prompt from the first assigned agent's strategy
        first_agent_name = next(iter(preflight_assignments))
        sample_strategy = agents[first_agent_name].strategy
        prompt_sample = build_prompt(args.topic, args.scope, sample_strategy,
                                     languages=languages, domain_priorities=domain_priorities)
        prompt_words = len(prompt_sample.split())
        estimate = estimate_run(preflight_assignments, providers, prompt_words=prompt_words, output_words=25000)
        print(format_report(estimate), flush=True)
        if args.estimate_only:
            return
        if not enforce_budget(estimate, args.max_cost_usd, prompt=not args.no_confirm):
            raise SystemExit(2)
    else:
        if args.estimate_only:
            raise SystemExit("--estimate-only requires scripts/cost.py, which failed to import.")
        if args.max_cost_usd is not None:
            print("  warn: scripts/cost.py unavailable, --max-cost-usd ignored", file=sys.stderr)

    if languages != ["en"]:
        print(f"Languages: {', '.join(languages)}", flush=True)
    if domain_priorities:
        print(f"Domain scope: injected ({len(domain_priorities)} chars)", flush=True)
    print(flush=True)

    run_job(
        topic=args.topic,
        scope=args.scope,
        output_dir=output_dir,
        providers=providers,
        agents=agents,
        languages=languages,
        domain_priorities=domain_priorities,
        resume=args.resume,
        seed=0,
    )


if __name__ == "__main__":
    main()
