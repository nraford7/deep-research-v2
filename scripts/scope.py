#!/usr/bin/env python3
"""
scope.py — pre-Round-1 domain scoping agent.

Takes a topic + scope, classifies the domain (medicine, law, economics, etc.),
and proposes domain-specific source priorities. The output is meant to be
injected into Round 1 model prompts so each model knows which databases /
journals / source types to weight.

Two modes:
  1. RULE-BASED  (default, offline)  — keyword classify topic, return curated priorities
  2. LLM-ASSISTED (--use-llm)        — ask Claude to propose priorities, falls back to rules

Output: domain-priorities.md (markdown) + .json (machine-readable for dispatch.py).

Usage:
  python3 scope.py --topic "central bank digital currencies" --output round0/scope.md
  python3 scope.py --topic "..." --scope "..." --use-llm --output round0/scope.md
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


DOMAIN_RULES = {
    "medicine": {
        "patterns": [r"\b(clinical|disease|patient|trial|therap|drug|vaccine|epidemiolog|public health|cancer|cardiac|surgery|covid|sars|virus|gene therapy|immunotherapy)\w*", r"\bWHO\b"],
        "priority_sources": [
            "PubMed / MEDLINE (peer-reviewed biomedical)",
            "Cochrane Reviews (systematic reviews)",
            "ClinicalTrials.gov (trial registry)",
            "WHO publications and country reports",
            "FDA / EMA regulatory filings",
            "The Lancet, NEJM, JAMA, BMJ, Nature Medicine",
        ],
        "weight_against": ["news summaries of preprints without checking the preprint", "wellness blogs", "social media"],
        "must_check": "Distinguish preprints from peer-reviewed publication. Note retractions.",
    },
    "law": {
        "patterns": [r"\b(statute|case law|court|jurisdiction|constitutional|treaty|regulation|legal|litigation|judicial|supreme court|circuit|appellate|criminal procedure|contract law|tort)\w*"],
        "priority_sources": [
            "Westlaw / LexisNexis (case law)",
            "Government legal databases (CourtListener, EUR-Lex, etc.)",
            "SSRN Legal Scholarship Network",
            "Law review articles (Harvard, Yale, Columbia, Stanford LR)",
            "Treaty texts and official UN/EU/government documents",
            "Restatements and major treatises",
        ],
        "weight_against": ["op-ed legal commentary without statutory or case grounding"],
        "must_check": "Cite specific statute / case / paragraph numbers, not legal news summaries.",
    },
    "economics": {
        "patterns": [r"\b(monetary|fiscal|GDP|inflation|trade|market|economy|economic|recession|growth|labor|wage|productivity|capital|central bank|exchange rate|tariff)\w*"],
        "priority_sources": [
            "NBER working papers",
            "SSRN Economics Network",
            "FRED (St. Louis Fed) for data series",
            "IMF Working Papers and Article IV reports",
            "World Bank / OECD / BIS publications",
            "AER, JPE, QJE, Econometrica (top-5 journals)",
            "Brookings, CEPR, PIIE policy work",
        ],
        "weight_against": ["financial-media takes without source paper", "single-blog macro forecasts"],
        "must_check": "Distinguish working papers from peer-reviewed versions. Cite data vintages.",
    },
    "policy_international": {
        "patterns": [r"\b(foreign policy|geopolitic|sanctions|alliance|diploma|treaty|UN|NATO|sovereign|sovereignty|security council|deterrence|grand strategy|hegemon)\w*"],
        "priority_sources": [
            "Foreign Affairs, Foreign Policy, International Security, ISQ",
            "RAND, Brookings, CFR, Chatham House, IISS, SIPRI reports",
            "Government foreign-policy white papers and strategy documents",
            "UN, NATO, EEAS official documents",
            "Congressional testimony and CRS reports",
        ],
        "weight_against": ["pundit takes without primary-document support"],
        "must_check": "Quote from strategy documents directly; note dates and signatories.",
    },
    "technology": {
        "patterns": [r"\b(software|hardware|AI|machine learning|deep learning|LLM|GPU|chip|semiconductor|cloud|protocol|cryptograph|blockchain|API|computer science)\w*"],
        "priority_sources": [
            "arXiv.org (cs, stat.ML, math)",
            "ACM, IEEE proceedings",
            "Major lab reports (Google Research, Meta AI, Anthropic, OpenAI, DeepMind, MSR)",
            "Patent filings (USPTO, EPO)",
            "Standards body documents (IETF, W3C, ISO/IEC, IEEE)",
            "Conference proceedings: NeurIPS, ICML, ICLR, USENIX, CCS, SOSP",
        ],
        "weight_against": ["company marketing pages presented as technical evidence"],
        "must_check": "Distinguish blog posts from peer-reviewed / archived papers. Note benchmark date.",
    },
    "physical_sciences": {
        "patterns": [r"\b(physics|chemistry|material|quantum|particle|astronom|cosmolog|battery|solar|reactor|climate|geophys|atmospher)\w*"],
        "priority_sources": [
            "arXiv (physics, cond-mat, astro-ph)",
            "PRL, Nature, Science, PRB, ApJ",
            "IPCC reports for climate",
            "NASA / NOAA / ESA datasets and reports",
            "DOE / DOE-Labs technical reports",
        ],
        "weight_against": ["press releases without paper link"],
        "must_check": "Cite the published paper, not the press release.",
    },
    "social_sciences": {
        "patterns": [r"\b(sociolog|political science|anthropolog|psycholog|cognitiv|behavior|education|demograph|inequality|race|gender|migration)\w*"],
        "priority_sources": [
            "Web of Science / Scopus for cited works",
            "JSTOR for historical articles",
            "AJS, ASR, APSR, AJPS, Annual Review series",
            "Census/EUROSTAT/national-statistical-office data",
            "Pew Research and World Values Survey for survey data",
        ],
        "weight_against": ["op-eds, single-author Substack as primary evidence"],
        "must_check": "Note study population, n, and date; flag effect-size vs significance.",
    },
}


def classify_topic(topic: str, scope: str = ""):
    text = f"{topic} {scope}".lower()
    scores = {}
    for domain, conf in DOMAIN_RULES.items():
        scores[domain] = sum(len(re.findall(p, text, flags=re.IGNORECASE)) for p in conf["patterns"])
    primary = max(scores, key=scores.get) if any(scores.values()) else None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return primary, [d for d, s in ranked if s > 0][:3]


def render_priorities(domain: str):
    conf = DOMAIN_RULES[domain]
    lines = [f"### {domain}", ""]
    lines.append("**Priority sources** (prefer these for citations):")
    for s in conf["priority_sources"]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("**Weight against**:")
    for s in conf["weight_against"]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append(f"**Must check**: {conf['must_check']}")
    return "\n".join(lines)


def llm_proposal(topic: str, scope: str, toml_paths=None):
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        import config
        import llm
    except ImportError:
        print("  --use-llm: config/llm not importable, falling back to rules", file=sys.stderr)
        return None
    try:
        paths = config.default_toml_paths() if toml_paths is None else toml_paths
        env = config.load_env_files()
        providers, _ = config.load_config(paths, env)
        defaults = config.load_defaults(paths)
        provider = config.pick_provider(providers, "utility", defaults)
    except Exception as e:
        print(f"  --use-llm: config error ({type(e).__name__}: {e}), falling back to rules", file=sys.stderr)
        return None
    if provider is None:
        print("  --use-llm: no provider configured, falling back to rules", file=sys.stderr)
        return None
    sys_prompt = (
        "You are a research methodologist. Given a topic and scope, identify the primary "
        "academic domain(s) and propose specific source priorities. Output JSON only: "
        '{"primary_domain": str, "secondary_domains": [str], "priority_sources": [str], '
        '"weight_against": [str], "must_check": str, "search_keywords": [str]}'
    )
    user = f"Topic: {topic}\nScope: {scope}\n\nReturn JSON only."
    try:
        text = llm.call_model(provider, sys_prompt, user)
    except Exception as e:
        print(f"  --use-llm: model error ({type(e).__name__}: {e}), falling back to rules", file=sys.stderr)
        return None
    # Strip markdown fences if the model added them despite "JSON only"
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    # Find the first balanced top-level object
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--scope", default="")
    ap.add_argument("--use-llm", action="store_true", help="Ask Claude to refine priorities")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    primary, ranked = classify_topic(args.topic, args.scope)
    rule_priorities = []
    rule_weight_against = []
    rule_must_check = []
    for d in ranked:
        conf = DOMAIN_RULES[d]
        rule_priorities.extend(conf["priority_sources"])
        rule_weight_against.extend(conf["weight_against"])
        rule_must_check.append(f"({d}) {conf['must_check']}")
    payload = {
        "topic": args.topic,
        "scope": args.scope,
        "primary_domain": primary,
        "ranked_domains": ranked,
        # Rule-based priorities are included here so that dispatch.py --scope-file
        # works even without --use-llm. dispatch.py reads these to build the
        # injected source-priority block in every Round 1 prompt.
        "priority_sources": rule_priorities,
        "weight_against": rule_weight_against,
        "must_check": " | ".join(rule_must_check),
    }

    if args.use_llm:
        llm = llm_proposal(args.topic, args.scope)
        if llm:
            payload["llm_proposal"] = llm

    lines = [f"# Domain scoping — {args.topic}", ""]
    if primary:
        lines += [f"**Primary domain:** {primary}",
                  f"**Ranked domains:** {', '.join(ranked) or '—'}",
                  ""]
        for d in ranked:
            lines.append(render_priorities(d))
            lines.append("")
    else:
        lines += ["No domain match. Falling back to generic strategy.", ""]

    if payload.get("llm_proposal"):
        prop = payload["llm_proposal"]
        lines += ["## LLM-refined proposal", "",
                  f"**Primary domain (LLM):** {prop.get('primary_domain', '—')}",
                  "",
                  "**Priority sources:**"]
        for s in prop.get("priority_sources", []):
            lines.append(f"- {s}")
        lines += ["", "**Weight against:**"]
        for s in prop.get("weight_against", []):
            lines.append(f"- {s}")
        lines += ["", f"**Must check:** {prop.get('must_check', '—')}", "",
                  "**Search keywords:** " + ", ".join(prop.get("search_keywords", []))]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    out_path.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Scope: {out_path}")
    print(f"JSON:  {out_path.with_suffix('.json')}")


if __name__ == "__main__":
    main()
