"""tier_of — thin wrapper over classify() for retrieval-result URLs + venues."""

from scripts.classify_sources import tier_of


def test_arxiv_is_preprint():
    assert tier_of("https://arxiv.org/abs/2401.01234") == "preprint"


def test_rand_is_institutional():
    assert tier_of("https://www.rand.org/pubs/research_reports/RR1.html") == "institutional"


def test_medium_is_blog():
    assert tier_of("https://medium.com/@author/some-post") == "blog"


def test_wikipedia_is_wiki():
    assert tier_of("https://en.wikipedia.org/wiki/Battery") == "wiki"


def test_news_host_is_news():
    assert tier_of("https://www.nytimes.com/2024/01/01/business/x.html") == "news"


def test_unknown_host_is_unknown():
    assert tier_of("https://example.org/some/path") == "unknown"


def test_venue_lifts_tier_to_peer_reviewed():
    # A bare host is unknown, but a peer-reviewed venue name tips classify over.
    assert tier_of("https://example.org/x", venue="Nature") == "peer_reviewed"


def test_none_venue_is_safe():
    # venue=None must not crash and must not fabricate a tier.
    assert tier_of("https://example.org/x", venue=None) == "unknown"
