import config
from scripts import cost

def _prov(name, pricing):
    return config.Provider(name, "openai", "k", "m", pricing=pricing)

def test_estimate_uses_provider_pricing_per_assignment():
    providers = {"ds": _prov("ds", {"in": 1.0, "out": 2.0})}
    assignments = {"academic": "ds", "contrarian": "ds"}
    est = cost.estimate_run(assignments, providers, prompt_words=1000, output_words=1000)
    assert len(est["per_agent"]) == 2
    assert est["total"] > 0
    assert all(r["provider"] == "ds" for r in est["per_agent"])

def test_estimate_unknown_pricing_excluded():
    providers = {"glm": _prov("glm", None)}
    est = cost.estimate_run({"academic": "glm"}, providers, prompt_words=1000, output_words=1000)
    row = est["per_agent"][0]
    assert row.get("excluded") is True and "unknown" in row.get("note", "").lower()

def test_estimate_search_surcharge_when_fields_present():
    plain = {"academic": "p"}; searcher = {"real-time": "s"}
    providers = {"p": _prov("p", {"in": 1.0, "out": 1.0}),
                 "s": _prov("s", {"in": 1.0, "out": 1.0, "reasoning": 1.0,
                                  "searches_per_run": 50, "search_per_k": 5.0})}
    no_fee = cost.estimate_run(plain, {"p": providers["p"]}, 1000, 1000)["total"]
    with_fee = cost.estimate_run(searcher, {"s": providers["s"]}, 1000, 1000)["total"]
    assert with_fee > no_fee


def test_estimate_cli_provider_is_subscription_no_cost():
    prov = config.Provider("sub", "cli", "", "", command="claude")
    est = cost.estimate_run({"academic": "sub"}, {"sub": prov}, 1000, 1000)
    row = est["per_agent"][0]
    assert row["total"] == 0.0 and "subscription" in row["note"].lower()
