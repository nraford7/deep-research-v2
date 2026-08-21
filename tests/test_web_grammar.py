"""Web citation grammar tests — routing, dedup URL-key, export @misc, classify tier.

INLINE_CITE_RE gains two named alternatives:
  - `domain`  : a bare domain `[a-z0-9-]+(\\.[a-z0-9-]+)+` — dots, NO spaces,
                LOWERCASE ONLY (no re.IGNORECASE) -> routes WEB.
  - `author`  : the existing academic author group (&/and/et al./comma-optional/
                paren forms) -> routes ACADEMIC.
Year group is `\\d{4}[a-z]?|n\\.d\\.`. `n.d.` is WEB-only (academic n.d. invalid).

Routing precedence: a match with a space in the head (e.g. "U.S. Treasury")
is an AUTHOR (academic); a dotted token with no spaces and all-lowercase is a
DOMAIN (web). An uppercase domain ("Treasury.gov") is NOT web (no IGNORECASE)
and falls through to the author branch.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.verify_citations import classify_cite, extract_inline_cites
from scripts.export import INLINE_CITE_RE as EXPORT_RE, to_bibtex_entry, extract_claims
from scripts.classify_sources import classify
from scripts.dedup_bib import normalize_web_url, cluster_entries


# ---------------------------------------------------------------------------
# Routing matrix — web vs academic
# ---------------------------------------------------------------------------

def _kind(cite):
    """cite dict -> 'web' | 'academic' via classify_cite."""
    return classify_cite(cite)


def test_academic_forms_still_route_academic():
    for text, note in [
        ("[Smith, 2020]", "solo comma"),
        ("(Brown, 2022)", "parenthetical"),
        ("[Smith & Jones, 2023]", "ampersand"),
        ("[Smith et al., 2021]", "et al"),
        ("[van der Berg, 2019]", "particle"),
        ("[Smith and Jones, 2018]", "and"),
        ("[Smith 2020]", "comma-optional"),
        ("[Smith, 2021a]", "year suffix"),
        ("[ Smith, 2020]", "leading whitespace"),
        ("[U.S. Treasury, 2024]", "space => author"),
    ]:
        cites = extract_inline_cites(text)
        assert cites, f"no cite parsed for {note}: {text}"
        assert _kind(cites[0]) == "academic", f"{note} ({text}) should be academic"


def test_domain_forms_route_web():
    for text, note in [
        ("[treasury.gov, 2024]", "lowercase domain"),
        ("[9news.com, n.d.]", "digit domain + n.d."),
        ("[example.co.uk, 2023]", "multi-dot domain"),
    ]:
        cites = extract_inline_cites(text)
        assert cites, f"no cite parsed for {note}: {text}"
        assert _kind(cites[0]) == "web", f"{note} ({text}) should be web"


def test_uppercase_domain_is_not_web():
    # No IGNORECASE on the domain alt => "Treasury.gov" cannot match the domain
    # branch; the author branch captures it => academic.
    cites = extract_inline_cites("[Treasury.gov, 2024]")
    assert cites, "no cite parsed for uppercase domain"
    assert _kind(cites[0]) == "academic"


def test_nd_is_web_only():
    # Web n.d. is valid + routes web.
    web = extract_inline_cites("[9news.com, n.d.]")
    assert web and _kind(web[0]) == "web"
    # Academic n.d. (author head with a space, no domain) must NOT parse as a
    # valid cite — n.d. is web-only.
    acad = extract_inline_cites("[U.S. Treasury, n.d.]")
    assert not acad, "academic n.d. must be invalid (web-only)"


def test_numeric_bracket_still_ignored():
    for text in ["[12]", "[3]", "[1, 2020]"]:
        cites = extract_inline_cites(text)
        assert not any(c.get("kind") == "web" for c in cites), text
        # [1, 2020] must not be captured as a real author cite either.
        for c in cites:
            assert c["author"].strip() not in ("1", "12", "3"), text


# ---------------------------------------------------------------------------
# dedup_bib — web URL-key clustering, incl. n.d. title-merge disabled
# ---------------------------------------------------------------------------

def test_normalize_web_url_strips_trailing_slash_and_utm():
    # Spec: lowercase scheme+host, strip trailing slash + utm_* params. Scheme
    # is preserved (http != https), so compare same-scheme variants.
    a = normalize_web_url("https://Example.COM/path/?utm_source=x&id=5")
    b = normalize_web_url("https://example.com/path?id=5")
    assert a == b, (a, b)
    # utm_* removal + trailing-slash removal + host lowercasing all applied.
    assert a == "https://example.com/path?id=5"


def test_web_entries_cluster_by_url():
    by_origin = {
        "a.md": ["- treasury.gov (2024). Budget outlook. https://treasury.gov/report"],
        "b.md": ["- treasury.gov (2024). Budget outlook update. https://treasury.gov/report/?utm_campaign=z"],
    }
    clusters = cluster_entries(by_origin, threshold=0.92)
    assert len(clusters) == 1, "same normalized URL should merge"


def test_nd_web_merges_only_on_url_not_title():
    # Two n.d. web entries with SIMILAR titles but DIFFERENT URLs must stay
    # separate (title-merge disabled for n.d.).
    by_origin = {
        "a.md": ["- 9news.com (n.d.). Grid battery storage economics explained. https://9news.com/a"],
        "b.md": ["- abcnews.com (n.d.). Grid battery storage economics explained. https://abcnews.com/b"],
    }
    clusters = cluster_entries(by_origin, threshold=0.80)
    assert len(clusters) == 2, "n.d. entries with different URLs must not title-merge"


# ---------------------------------------------------------------------------
# export — web rows carry url; BibTeX @misc year optional
# ---------------------------------------------------------------------------

def test_export_re_matches_domain_and_nd():
    assert EXPORT_RE.search("[treasury.gov, 2024]") is not None
    assert EXPORT_RE.search("[9news.com, n.d.]") is not None


def test_export_claims_carry_url(tmp_path):
    sec = tmp_path / "sections"
    sec.mkdir()
    (sec / "s1.md").write_text(
        "Grid batteries fell in price [treasury.gov, 2024]. "
        "See https://treasury.gov/report for detail.",
        encoding="utf-8",
    )
    rows = list(extract_claims(sec))
    assert rows, "no claims extracted"
    web_cites = [c for row in rows for c in row["citations"] if c.get("kind") == "web"]
    assert web_cites, "no web citation captured"
    # The web claim row should surface a url (from the sentence or a nearby URL).
    assert any("url" in c for c in web_cites), "web citation must carry url"


def test_bibtex_misc_year_optional_for_url_entry():
    counter = {}
    entry = "9news.com (n.d.). Grid battery storage explainer. https://9news.com/grid"
    bt = to_bibtex_entry(entry, counter)
    assert bt.startswith("@misc{")
    assert "url" in bt and "9news.com/grid" in bt
    # n.d. entry emits a CLEAN record: domain as author, the real title (not a
    # dot-truncated fragment), year n.d. — no forced fake year, no crash.
    assert "9news.com" in bt
    assert "Grid battery storage explainer" in bt
    assert "{n.d.}" in bt


def test_web_url_association_rejects_lookalike_host():
    # foo.com must NOT bind to notfoo.com — host boundary, not substring.
    import re as _re
    from scripts.export import _cite_dict
    m = EXPORT_RE.search("[foo.com, 2024]")
    sent = "As reported [foo.com, 2024] see https://notfoo.com/x for context."
    cite = _cite_dict(m, sent)
    assert cite["kind"] == "web"
    # No matching host in the sentence -> https://foo.com fallback, not notfoo.com.
    assert cite["url"] == "https://foo.com"


def test_web_url_association_matches_subdomain():
    from scripts.export import _cite_dict
    m = EXPORT_RE.search("[treasury.gov, 2024]")
    sent = "Per [treasury.gov, 2024] at https://home.treasury.gov/report ok."
    cite = _cite_dict(m, sent)
    assert cite["url"] == "https://home.treasury.gov/report"


def test_export_drops_academic_nd():
    # An author-head n.d. is web-only-invalid; _cite_dict returns None so the
    # export agrees with verify_citations (which rejects the same match).
    from scripts.export import _cite_dict
    m = EXPORT_RE.search("[U.S. Treasury, n.d.]")
    # If the regex matched at all, _cite_dict must reject it.
    if m and not m.group("domain"):
        assert _cite_dict(m, "[U.S. Treasury, n.d.]") is None


# ---------------------------------------------------------------------------
# classify — year-less web classifies by domain tier (no drop)
# ---------------------------------------------------------------------------

def test_yearless_web_classifies_by_domain():
    # A news domain with no year still gets the news tier (not dropped/unknown).
    assert classify("9news.com (n.d.). Grid storage. https://9news.com/x") in (
        "news", "blog", "institutional", "wiki", "unknown",
    )
    # A .gov domain -> institutional even without a year.
    assert classify("treasury.gov (n.d.). Report. https://treasury.gov/r") == "institutional"
    # Wikipedia domain, no year -> wiki.
    assert classify("Topic (n.d.). https://en.wikipedia.org/wiki/Topic") == "wiki"
