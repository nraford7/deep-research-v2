"""authority_tag — transparent, middot-joined bracketed provenance string."""

from scripts.classify_sources import authority_tag


def test_rich_entry_includes_all_fields():
    tag = authority_tag({
        "tier": "preprint",
        "year": 2026,
        "cited_by": 0,
        "h_index": 87,
        "institution": "MIT",
        "replication": "unreplicated",
    })
    assert tag.startswith("[")
    assert tag.endswith("]")
    for fragment in ("preprint", "0 cited", "author h-index 87", "MIT", "unreplicated"):
        assert fragment in tag


def test_degraded_tier_and_year_only():
    # U+00B7 middot separator, no em-dash.
    assert authority_tag({"tier": "news", "year": 2025}) == "[news · 2025]"


def test_minimum_tier_only():
    assert authority_tag({"tier": "blog"}) == "[blog]"


def test_graceful_omission_of_missing_fields():
    tag = authority_tag({"tier": "peer_reviewed"})
    assert "cited" not in tag
    assert "h-index" not in tag


def test_advocacy_stance_surfaced():
    tag = authority_tag({"tier": "institutional", "stance": "advocacy"})
    assert "advocacy" in tag
