"""descriptors_of — institutional sub-classification (named / unverified / .gov)."""

from scripts.classify_sources import descriptors_of


def test_named_research_org():
    d = descriptors_of("Brookings Institution report", "institutional")
    assert d["sub"] == "named"
    assert d["stance"] == "research"


def test_unknown_generic_hint():
    d = descriptors_of("Some Unknown Institute white paper", "institutional")
    assert d["sub"] == "unverified"
    assert d["standing"] == "unknown"


def test_gov_domain_is_established_research():
    d = descriptors_of("https://treasury.gov/x", "institutional")
    assert d["sub"] == "named"
    assert d["standing"] == "established"
    assert d["stance"] == "research"


def test_named_advocacy_org():
    d = descriptors_of("Heritage Foundation brief", "institutional")
    assert d["sub"] == "named"
    assert d["stance"] == "advocacy"


def test_non_institutional_returns_empty():
    assert descriptors_of("anything", "preprint") == {}
