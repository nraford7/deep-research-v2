#!/usr/bin/env python3
"""
verify_citations.py — adversarial citation verification.

Extracts every inline citation [Author, Year] and every URL from a markdown
file (or all .md files in a directory), then resolves each against OpenAlex
and Crossref (free, no API key). Flags:

  - orphaned inline cites: [Author, Year] with no bibliography entry
  - unresolvable bib entries: cannot find the work in OpenAlex/Crossref
  - URL liveness: HTTP HEAD with redirects, mark dead URLs
  - suspicious entries: bib entries that resolve to a very different title

Output: a verification report in markdown.

Usage:
  python3 verify_citations.py <path> --output verify-report.md
  python3 verify_citations.py research/topic/sections/ --output factcheck/citations.md

Set CONTACT_EMAIL env var for the OpenAlex/Crossref "polite pool" — recommended.
Citations resolve OpenAlex → Crossref → Semantic Scholar; the third is a fallback
so an OpenAlex throttle spell (429s) can't leave a real citation unresolved.
Set SEMANTIC_SCHOLAR_KEY (optional) to lift that fallback off the shared free rate.
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

# Support both documented direct execution (`python3 scripts/verify_citations.py`)
# and package imports (`python3 -m scripts.verify_citations`, tests).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.helper_runtime import enclosing_layout, standalone_mutation_guard

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)


class CappedRetry(Retry):
    """Retry that honors server Retry-After headers but caps the sleep.

    urllib3 sleeps for the full Retry-After value with NO ceiling; a
    rate-limiting server (Crossref/OpenAlex 429s especially) can park the
    process for an hour inside the retry machinery, outside every request
    timeout. Cap it so a stall becomes a bounded pause. (Root cause of a
    42-minute silent hang, 2026-08-25.)"""

    RETRY_AFTER_CAP = 30.0  # seconds

    def get_retry_after(self, response):
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return retry_after
        return min(retry_after, self.RETRY_AFTER_CAP)



# --- SSRF-hardened URL probe ------------------------------------------------
# Three-state, allowlist-first URL liveness check. This is the security surface
# of the verifier: it must never be trickable into connecting to an internal /
# non-globally-routable address (SSRF), whether directly, via a multi-answer DNS
# response, via an IPv4-mapped IPv6 address, or via a redirect that re-binds DNS
# to an internal target. See probe_url for the full ruleset.

PROBE_MAX_REDIRECTS = 3
# (connect, read) tuple — a scalar would let a server trickle data below the
# read-inactivity timeout indefinitely; bounding both caps each hop near 10s.
PROBE_TIMEOUT = (10, 10)
PROBE_READ_CAP = 512 * 1024  # 512 KB read-truncation ceiling
PROBE_MAX_URLS = 60          # hard cap on URLs probed per run
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = {80, 443}


@dataclass
class ProbeResult:
    state: str   # "resolved" | "unresolved" | "indeterminate"
    reason: str  # short machine tag: "ok", "policy", "404", "http-403", "timeout", ...
    url: str = ""


# NAT64 well-known prefix (RFC 6052): 64:ff9b::/96 embeds an IPv4 in its low 32
# bits. Crucially, ipaddress reports 64:ff9b::a9fe:a9fe as is_global==True — so a
# NAT64-wrapped 169.254.169.254 (AWS metadata) would pass a naive is_global gate.
# We unwrap it to the embedded IPv4 BEFORE vetting so the real address is judged.
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def _unwrap_mapped(ip_str: str):
    """Return an ipaddress object, unwrapping IPv4-mapped IPv6 (::ffff:a.b.c.d)
    AND NAT64 (64:ff9b::/96) to the embedded IPv4 FIRST so the is_global check
    sees the real address, never the translation wrapper."""
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address):
        # ipv4_mapped is the IPv4 address for ::ffff:a.b.c.d, else None.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return mapped
        if ip in _NAT64_PREFIX:
            # low 32 bits are the embedded IPv4 (e.g. 64:ff9b::169.254.169.254)
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_globally_routable(ip) -> bool:
    """True only for a genuinely public, routable address. Rejects loopback,
    private, link-local, multicast, reserved, unspecified — belt-and-suspenders
    on top of is_global (which already excludes most of these). Also rejects the
    NAT64 prefix outright: a defensive second line so this guarantee survives even
    if _unwrap_mapped's NAT64 unwrap is ever removed (is_global alone would let
    64:ff9b::<metadata-ip> through)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_PREFIX:
        return False
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _resolve_and_vet(host: str, port: int):
    """Resolve `host` UP FRONT and vet EVERY returned A/AAAA answer.

    Returns (first_vetted_ip_str, None) when every answer is globally routable,
    else (None, ProbeResult(...)) describing the failure. A single non-global
    answer poisons the whole set (blocks DNS-rebinding partials) — we do NOT
    connect in that case."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None, ProbeResult("unresolved", "dns", "")
    except (socket.error, OSError):
        return None, ProbeResult("unresolved", "dns", "")

    if not infos:
        return None, ProbeResult("unresolved", "dns", "")

    first_ip = None
    for info in infos:
        sockaddr = info[4]
        raw_ip = sockaddr[0]
        try:
            ip = _unwrap_mapped(raw_ip)
        except ValueError:
            return None, ProbeResult("indeterminate", "policy", "")
        if not _is_globally_routable(ip):
            # ANY non-global answer -> policy reject, never connect.
            return None, ProbeResult("indeterminate", "policy", "")
        if first_ip is None:
            first_ip = str(ip)
    return first_ip, None


class _PinnedHTTPSAdapter(HTTPAdapter):
    """HTTPS adapter that keeps TLS cert verification ON against the ORIGINAL
    hostname even though we connect to a pinned IP. urllib3's PoolManager
    forwards unknown kwargs to the connection pools as connection_pool_kw, so
    `server_hostname` drives SNI + the default cert-hostname check, and
    `assert_hostname` re-asserts the cert matches the original host.

    Disabling TLS verification is FORBIDDEN here (and a source-grep test
    enforces its absence): we pin the IP but still validate the certificate
    against the real host."""

    def __init__(self, server_hostname: str, *args, **kwargs):
        self._server_hostname = server_hostname
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self._server_hostname
        kwargs["assert_hostname"] = self._server_hostname
        return super().init_poolmanager(*args, **kwargs)


def _probe_session(original_host: str, pinned_ip: str) -> requests.Session:
    """A session that connects to `pinned_ip` but presents/verifies TLS for
    `original_host`. trust_env is False so proxy env vars cannot bypass the pin.
    NOTE: TLS verification stays ON — it is never disabled."""
    s = requests.Session()
    s.trust_env = False  # ignore HTTP(S)_PROXY / NO_PROXY — a proxy bypasses the IP pin
    s.headers.update({"User-Agent": f"deeper-research/2.0 (mailto:{CONTACT})"})
    s.mount("https://", _PinnedHTTPSAdapter(original_host))
    # For http:// there is no TLS to verify; the IP pin (host rewritten below)
    # plus the original Host header are what matter.
    s.mount("http://", HTTPAdapter())
    return s


def _parse_probe_target(url: str):
    """Parse + policy-check a single URL. Returns (host, port, pinned_ip, pinned_url)
    on success, else (None, ProbeResult). Enforces scheme/userinfo/port allowlists
    and resolves+vets DNS UP FRONT, pinning the connection to the first vetted IP."""
    try:
        p = urlparse(url)
    except ValueError:
        return None, ProbeResult("indeterminate", "policy", url)

    if p.scheme not in _ALLOWED_SCHEMES:
        return None, ProbeResult("indeterminate", "policy", url)
    if p.username or p.password:
        # userinfo in the authority (user:pass@host) is rejected outright.
        return None, ProbeResult("indeterminate", "policy", url)

    host = p.hostname
    if not host:
        return None, ProbeResult("indeterminate", "policy", url)

    default_port = 443 if p.scheme == "https" else 80
    try:
        port = p.port if p.port is not None else default_port
    except ValueError:
        return None, ProbeResult("indeterminate", "policy", url)
    if port not in _ALLOWED_PORTS:
        return None, ProbeResult("indeterminate", "policy", url)

    pinned_ip, err = _resolve_and_vet(host, port)
    if err is not None:
        err.url = url
        return None, err

    # Rewrite the netloc to the pinned IP (bracket IPv6) so we connect exactly
    # to the vetted address; the Host header (set by the caller) stays original.
    try:
        ipaddress.IPv6Address(pinned_ip)
        netloc_ip = f"[{pinned_ip}]"
    except (ipaddress.AddressValueError, ValueError):
        netloc_ip = pinned_ip
    if p.port is not None:
        netloc_ip = f"{netloc_ip}:{port}"
    pinned_url = urlunparse((p.scheme, netloc_ip, p.path or "/", p.params, p.query, ""))
    return (host, port, pinned_ip, pinned_url), None


def _classify_status(code: int) -> ProbeResult:
    if 200 <= code < 300:
        return ProbeResult("resolved", "ok")
    if code in (404, 410):
        return ProbeResult("unresolved", str(code))
    # 401/403/405/429/5xx -> indeterminate (flag + keep, never drop).
    return ProbeResult("indeterminate", f"http-{code}")


def probe_url(url: str) -> ProbeResult:
    """Three-state SSRF-hardened liveness probe.

    state -> "resolved" | "unresolved" | "indeterminate"
      2xx                                   -> resolved
      404 / 410 / DNS-failure / conn-refused -> unresolved
      401/403/405/429/5xx / TLS / timeout /
        oversize-truncation / policy-reject  -> indeterminate (with reason)

    SSRF rules (all enforced): scheme http/https only; no userinfo; port 80/443
    only; DNS resolved + EVERY answer vetted globally-routable UP FRONT (IPv4-
    mapped IPv6 unwrapped first); connection PINNED to the first vetted IP with
    the original Host header and TLS verified against the original hostname
    (disabling TLS verification is forbidden); proxy env ignored
    (trust_env=False); each redirect
    (<=3) re-parsed / re-resolved / re-vetted / re-pinned; 512KB read cap; 10s
    per-hop timeout. Flag + KEEP, never silently drop."""
    current = url
    last = ProbeResult("indeterminate", "policy", url)
    for _hop in range(PROBE_MAX_REDIRECTS + 1):
        target, err = _parse_probe_target(current)
        if err is not None:
            return err
        host, port, pinned_ip, pinned_url = target
        session = _probe_session(host, pinned_ip)
        try:
            resp = session.get(
                pinned_url,
                headers={"Host": host},
                allow_redirects=False,     # manual — every hop must be re-vetted
                timeout=PROBE_TIMEOUT,
                stream=True,
            )
        except requests.exceptions.SSLError:
            return ProbeResult("indeterminate", "tls", url)
        except requests.exceptions.Timeout:
            return ProbeResult("indeterminate", "timeout", url)
        except requests.exceptions.ConnectionError:
            return ProbeResult("unresolved", "conn-refused", url)
        except requests.exceptions.RequestException:
            return ProbeResult("indeterminate", "request-error", url)
        finally:
            pass

        try:
            code = resp.status_code
            # Redirect: re-resolve + re-vet the target on the NEXT loop.
            if code in (301, 302, 303, 307, 308) and "location" in {k.lower() for k in resp.headers}:
                location = resp.headers.get("Location") or resp.headers.get("location")
                if not location:
                    return _classify_status(code)
                current = requests.compat.urljoin(current, location)
                last = ProbeResult("indeterminate", "redirect", url)
                continue

            result = _classify_status(code)
            if result.state == "resolved":
                # Enforce the read cap: pull up to CAP+1 bytes; if the body
                # exceeds the cap, mark oversize-truncation -> indeterminate.
                read = 0
                oversize = False
                try:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        read += len(chunk)
                        if read > PROBE_READ_CAP:
                            oversize = True
                            break
                except requests.exceptions.RequestException:
                    return ProbeResult("indeterminate", "read-error", url)
                if oversize:
                    return ProbeResult("indeterminate", "oversize-truncation", url)
            result.url = url
            return result
        finally:
            resp.close()

    # Exhausted the redirect budget without a terminal response.
    last.reason = "too-many-redirects"
    last.url = url
    return last


CONTACT = os.environ.get("CONTACT_EMAIL", "anonymous@example.com")
OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1"
SS_KEY = os.environ.get("SEMANTIC_SCHOLAR_KEY")  # optional; raises the shared free-tier rate
OA_KEY = os.environ.get("OPENALEX_KEY")  # OpenAlex premium key; metered credit pool

_S2_MIN_INTERVAL = 1.5  # seconds; S2 limit is 1 req/s CUMULATIVE across all endpoints (margin over 1.0s for their bursty fixed-window enforcement; CappedRetry absorbs stragglers)
_s2_lock = threading.Lock()
_s2_last_request = 0.0


def _s2_throttle():
    """Serialize Semantic Scholar requests to >= _S2_MIN_INTERVAL apart. The
    S2 resolver runs inside the ThreadPoolExecutor, so multiple worker threads
    can reach the S2 fallback at once; the lock (held across the sleep) enforces
    the 1 req/s cumulative limit globally instead of per-thread."""
    global _s2_last_request
    with _s2_lock:
        wait = _S2_MIN_INTERVAL - (time.monotonic() - _s2_last_request)
        if wait > 0:
            time.sleep(wait)
        _s2_last_request = time.monotonic()


def _oa_params(params: dict) -> dict:
    """Attach the OpenAlex API key when configured, scoped to OpenAlex requests
    (query param, never a shared session header)."""
    if OA_KEY:
        return {**params, "api_key": OA_KEY}
    return params

# Citation patterns — broadened to handle:
#   [Smith, 2020]                 — solo author            (ACADEMIC)
#   [Smith et al., 2020]          — et al                  (ACADEMIC)
#   [Smith & Jones, 2020]         — two-author ampersand   (ACADEMIC)
#   [Smith and Jones, 2020]       — two-author and         (ACADEMIC)
#   [van der Berg, 2020]          — lowercase particles    (ACADEMIC)
#   [U.S. Treasury, 2024]         — institutional w/ dots  (ACADEMIC — has a space)
#   (Smith, 2020) and (Smith et al., 2020)  — parenthetical APA (ACADEMIC)
#   [treasury.gov, 2024]          — bare domain            (WEB)
#   [9news.com, n.d.]             — digit domain, no date  (WEB — n.d. web-only)
#
# The pattern carries TWO named alternatives for the citation HEAD:
#   `domain` — a bare domain: dot-separated lowercase/digit/hyphen labels, NO
#              spaces, NO uppercase (deliberately NOT re.IGNORECASE) => WEB.
#   `author` — the existing academic author group (surname, &/and/et al., dots,
#              spaces, particles) => ACADEMIC.
# The domain alternative is tried FIRST so a clean lowercase domain routes web;
# anything with a space (e.g. "U.S. Treasury") or any uppercase (e.g.
# "Treasury.gov") cannot match `domain` and falls through to `author`.
#
# Year group: `\d{4}[a-z]?` (optional suffix like 2021a) OR `n.d.`. `n.d.` is
# WEB-ONLY — enforced downstream in classify_cite / extract_inline_cites (a
# `n.d.` head that is NOT a domain is rejected as invalid).
#
# NOTE: the whole regex is compiled WITHOUT re.IGNORECASE. The academic author
# group already spells out both cases explicitly, so case-sensitivity is what
# makes "Treasury.gov" (uppercase) route academic while "treasury.gov" routes web.
_AUTHOR_GROUP = r"(?:[A-Za-z][A-Za-z\.\-' ]{0,80}?)"
_AUTHOR_ALT = rf"{_AUTHOR_GROUP}(?:\s+(?:et\s+al\.?|&\s+{_AUTHOR_GROUP}|and\s+{_AUTHOR_GROUP}))?"
_DOMAIN_ALT = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"
_YEAR_ALT = r"\d{4}[a-z]?|n\.d\."
INLINE_CITE_RE = re.compile(
    rf"[\[\(]\s*(?:(?P<domain>{_DOMAIN_ALT})|(?P<author>{_AUTHOR_ALT})),?\s*(?P<year>{_YEAR_ALT})\s*[\]\)]"
)
URL_RE = re.compile(r"https?://[^\s\)\]\>]+")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
BIB_HEADER_RE = re.compile(r"^(#{1,6})\s+.*\b(bibliography|references|works cited|sources)\b",
                           re.IGNORECASE | re.MULTILINE)

# KB citations — user-provided documents ingested by ingest_local.py.
# SEPARATE from INLINE_CITE_RE (which stays byte-for-byte aligned with
# export.py). This regex is ALSO copied byte-for-byte into export.py; if the
# two diverge, KB cites never reach claims.jsonl.
KB_CITE_RE = re.compile(r"\[\s*kb:(?P<slug>[a-z0-9][a-z0-9-]*)\s*,\s*(?P<kbyear>\d{4}|n\.d\.)\s*\]")

# Bracketed spans that are LEGAL non-citations (metadata tags the pipeline
# mandates, markdown constructs) — excluded from the unparseable-cite check.
_BRACKET_EXCLUDE_RE = re.compile(
    r"^\s*(?:as of:|confidence:|disputed:|UNVERIFIED|r2\.5-|\^)", re.IGNORECASE)
_BRACKET_SPAN_RE = re.compile(r"\[[^\]]{1,200}\]")
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# Any 4-digit year (1776 too), suffixed years (2020a), and case-variant n.d.
# (N.D.). Deliberately broader than INLINE_CITE_RE's year alt: this is the
# "citation-ish" prefilter, so near-miss years must land here, not slip by.
_HAS_YEAR_RE = re.compile(r"\b\d{4}[a-z]?\b|\bn\.d\.", re.IGNORECASE)


def extract_kb_cites(text: str):
    out, seen = [], set()
    for m in KB_CITE_RE.finditer(text):
        key = (m.group("slug"), m.group("kbyear"), m.start())
        if key in seen:
            continue
        seen.add(key)
        out.append({"slug": m.group("slug"), "year": m.group("kbyear"), "kind": "kb"})
    return out


def find_unparseable_cites(text: str):
    """Citation-ish bracketed spans no verifier can check: contains a year,
    matches neither the academic/web nor the kb grammar, and is not an
    excluded metadata tag / markdown link / footnote / code span.
    Inverts the old failure mode: unparseable used to be invisible."""
    text = _CODE_SPAN_RE.sub(" ", text)
    flags = []
    for m in _BRACKET_SPAN_RE.finditer(text):
        span = m.group(0)
        inner = span[1:-1]
        if text[m.end():m.end() + 1] == "(":
            # Markdown link [text](url) — UNLESS the label itself is cite-ish
            # (semicolon AND a year, e.g. "[Invented Source; 2020](url)"):
            # that's a malformed citation hiding as a link, so flag it.
            if not (";" in inner and _HAS_YEAR_RE.search(inner)):
                continue
        if _BRACKET_EXCLUDE_RE.match(inner):     # metadata tags / footnotes
            continue
        if not _HAS_YEAR_RE.search(inner):
            continue
        if KB_CITE_RE.fullmatch(span):
            continue
        cite_m = INLINE_CITE_RE.fullmatch(span)
        if cite_m:
            # fullmatch of the existing grammar == verifiable; skip. (An
            # academic head with n.d. is invalid — extract_inline_cites
            # rejects it — so treat that one case as unparseable.)
            if not (cite_m.group("author") and cite_m.group("year") == "n.d."):
                continue
        flags.append(span)
    return flags


def load_kb_manifest(paths):
    """FIRST-wins across ``paths``: slice_local.jsonl is passed before
    inherited_corpus.jsonl, so the child run's local entry beats an inherited
    parent duplicate. Returns (manifest, duplicate_slugs) — every slug seen
    more than once is recorded, never silently shadowed."""
    manifest = {}
    duplicates = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = row.get("kb_slug")
            if not slug:
                continue
            if slug in manifest:
                if slug not in duplicates:
                    duplicates.append(slug)
                continue
            manifest[slug] = row
    return manifest, duplicates


def check_kb_cites(kb_cites, manifest):
    """Split kb cites into (known, unknown, year_mismatch, year_unchecked).

    Row HAS a year  → cite year must equal it; anything else (a different
                       year OR n.d.) is a mismatch (hard-ish flag).
    Row has NO year → n.d. is known; a specific year is 'unchecked' (info —
                       the verifier cannot confirm it, run ingest with --year)."""
    known, unknown, mismatch, unchecked = [], [], [], []
    for c in kb_cites:
        row = manifest.get(c["slug"])
        if row is None:
            unknown.append(c)
        elif row.get("year") is not None:
            (known if c["year"] == str(row["year"]) else mismatch).append(c)
        elif c["year"] == "n.d.":
            known.append(c)
        else:
            unchecked.append(c)
    return known, unknown, mismatch, unchecked


def first_surname(author_field: str) -> str:
    """Pull the first author's surname from messy citation text."""
    s = re.sub(r"\bet\s+al\.?", "", author_field, flags=re.IGNORECASE)
    s = re.sub(r"\s+(?:&|and)\s+.*$", "", s, flags=re.IGNORECASE)
    s = s.strip(" ,.").rstrip(",")
    tokens = [t for t in re.split(r"\s+", s) if t]
    if not tokens:
        return ""
    return tokens[-1].lower().strip(".,")


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": f"deeper-research/1.0 (mailto:{CONTACT})"})
    retry = CappedRetry(
        total=4,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def find_md_files(path: Path):
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.md"))


def classify_cite(cite: dict) -> str:
    """Route a parsed citation to 'web' or 'academic'.

    A citation whose HEAD matched the `domain` alternative (bare lowercase
    domain, no spaces) is WEB; anything that matched the `author` alternative is
    ACADEMIC. `n.d.` is only ever produced for web citations (see
    extract_inline_cites), so an `n.d.` year implies web too."""
    if cite.get("kind"):
        return cite["kind"]
    if cite.get("domain"):
        return "web"
    if cite.get("year") == "n.d.":
        return "web"
    return "academic"


def extract_inline_cites(text: str):
    cites = []
    seen = set()
    for m in INLINE_CITE_RE.finditer(text):
        domain = m.group("domain")
        author = m.group("author")
        year = m.group("year")
        if domain:
            # WEB citation — bare domain head.
            head = domain.strip()
            key = ("web:" + head.lower(), year, m.start())
            if key in seen:
                continue
            seen.add(key)
            cites.append({"author": head, "year": year, "kind": "web", "domain": head})
            continue
        # ACADEMIC citation — author head.
        author = (author or "").strip()
        # `n.d.` is web-only: an academic author head with n.d. is NOT a valid cite.
        if year == "n.d.":
            continue
        # Skip noise that looks like a citation but isn't (e.g. "[1, 2020]").
        if not re.search(r"[A-Za-z]{2}", author):
            continue
        key = (author.lower(), year, m.start())
        if key in seen:
            continue
        seen.add(key)
        cites.append({"author": author, "year": year, "kind": "academic"})
    return cites


def extract_urls(text: str):
    return list({m.group(0).rstrip(".,;") for m in URL_RE.finditer(text)})


def extract_bibliography(text: str):
    m = BIB_HEADER_RE.search(text)
    if not m:
        return []
    level = len(m.group(1))
    tail = text[m.end():]
    # Stop only at the next heading of the same or higher level, so deeper category
    # subheadings (e.g. "### A. Formal narratology") stay inside the bibliography.
    for hm in re.finditer(r"^(#{1,6})\s+\S", tail, re.MULTILINE):
        if len(hm.group(1)) <= level:
            tail = tail[:hm.start()]
            break
    # Drop inner heading lines (category subheadings) before splitting into entries.
    tail = "\n".join(ln for ln in tail.splitlines() if not re.match(r"^\s*#{1,6}\s", ln))
    entries = []
    for raw in re.split(r"\n(?=\s*[-*]\s|\s*\d+\.\s)", tail):
        raw = raw.strip(" -*\t\n")
        if len(raw) < 20:
            continue
        entries.append(re.sub(r"\s+", " ", raw))
    return entries


def resolve_openalex(s, entry: str):
    doi_match = DOI_RE.search(entry)
    try:
        if doi_match:
            r = s.get(f"{OPENALEX}/works/doi:{doi_match.group(0).lower()}", params=_oa_params({"mailto": CONTACT}), timeout=15)
            if r.ok:
                return r.json()
        title = entry[:240].replace("\n", " ")
        r = s.get(f"{OPENALEX}/works", params=_oa_params({"search": title, "per-page": 1, "mailto": CONTACT}), timeout=15)
        if r.ok:
            results = r.json().get("results", [])
            return results[0] if results else None
    except requests.RequestException:
        return None
    return None


def resolve_crossref(s, entry: str):
    doi_match = DOI_RE.search(entry)
    try:
        if doi_match:
            r = s.get(f"{CROSSREF}/works/{doi_match.group(0)}", params={"mailto": CONTACT}, timeout=15)
            if r.ok:
                return r.json().get("message")
        r = s.get(f"{CROSSREF}/works", params={"query.bibliographic": entry[:240], "rows": 1, "mailto": CONTACT}, timeout=15)
        if r.ok:
            items = r.json().get("message", {}).get("items", [])
            return items[0] if items else None
    except requests.RequestException:
        return None
    return None


def resolve_semantic_scholar(s, entry: str):
    """Third resolver, tried only when OpenAlex AND Crossref both miss — an
    independent index so an OpenAlex throttle spell (429s) can't leave an
    otherwise-real citation unresolved. DOI lookup first, then title search.
    Uses SEMANTIC_SCHOLAR_KEY (x-api-key header) when set for a higher rate."""
    headers = {"x-api-key": SS_KEY} if SS_KEY else {}
    fields = "title,year,citationCount,externalIds"
    doi_match = DOI_RE.search(entry)
    try:
        if doi_match:
            _s2_throttle()
            r = s.get(f"{SEMANTIC_SCHOLAR}/paper/DOI:{doi_match.group(0)}",
                      params={"fields": fields}, headers=headers, timeout=15)
            if r.ok:
                return r.json()
        title = entry[:240].replace("\n", " ")
        _s2_throttle()
        r = s.get(f"{SEMANTIC_SCHOLAR}/paper/search",
                  params={"query": title, "limit": 1, "fields": fields},
                  headers=headers, timeout=15)
        if r.ok:
            data = r.json().get("data") or []
            return data[0] if data else None
    except requests.RequestException:
        return None
    return None


def title_match(entry: str, resolved_title: str) -> float:
    if not resolved_title:
        return 0.0
    a = re.sub(r"[^a-z0-9 ]+", "", entry.lower())
    b = re.sub(r"[^a-z0-9 ]+", "", resolved_title.lower())
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(b_tokens)


def resolve_entry(s, entry: str):
    oa = resolve_openalex(s, entry)
    if oa:
        title = (oa.get("title") or "").strip()
        match = title_match(entry, title)
        return {
            "source": "openalex",
            "title": title,
            "doi": oa.get("doi"),
            "id": oa.get("id"),
            "cited_by": oa.get("cited_by_count"),
            "year": oa.get("publication_year"),
            "title_match": round(match, 2),
        }
    cr = resolve_crossref(s, entry)
    if cr:
        title = (cr.get("title") or [""])[0]
        match = title_match(entry, title)
        return {
            "source": "crossref",
            "title": title,
            "doi": cr.get("DOI"),
            "year": (cr.get("issued", {}).get("date-parts") or [[None]])[0][0],
            "title_match": round(match, 2),
        }
    ss = resolve_semantic_scholar(s, entry)
    if ss:
        title = (ss.get("title") or "").strip()
        match = title_match(entry, title)
        return {
            "source": "semantic_scholar",
            "title": title,
            "doi": (ss.get("externalIds") or {}).get("DOI"),
            "id": ss.get("paperId"),
            "cited_by": ss.get("citationCount"),
            "year": ss.get("year"),
            "title_match": round(match, 2),
        }
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Markdown file or directory")
    ap.add_argument("--output", default="verify-report.md", help="Where to write the report")
    ap.add_argument("--check-urls", action="store_true", help="HEAD-check every URL (slow)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--kb-manifest", action="append", default=[])
    ap.add_argument("--fail-on", default="",
                    help="comma list: kb-unknown,unparseable — exit 1 when non-empty")
    args = ap.parse_args()

    # Validate --fail-on UP FRONT (before any network/report work): a typo'd
    # class name silently disabling the strict gate is worse than a hard stop.
    valid_fail_classes = {"kb-unknown", "unparseable"}
    fail_classes = {c.strip() for c in args.fail_on.split(",") if c.strip()}
    invalid_classes = fail_classes - valid_fail_classes
    if invalid_classes:
        print(f"unknown --fail-on class(es): {', '.join(sorted(invalid_classes))}"
              f" — valid classes: {', '.join(sorted(valid_fail_classes))}",
              file=sys.stderr)
        sys.exit(2)

    s = session()
    files = find_md_files(Path(args.path))
    if not files:
        sys.exit(f"No .md files found at {args.path}")

    all_cites, all_urls, all_bib = [], [], []
    all_kb, all_unparseable = [], []
    bib_origin = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for c in extract_inline_cites(text):
            c["file"] = str(f)
            all_cites.append(c)
        # KB cites go through their own extractor/checker — they must never
        # enter the academic orphan loop or the OpenAlex/Crossref resolver pool.
        for c in extract_kb_cites(text):
            c["file"] = str(f)
            all_kb.append(c)
        for span in find_unparseable_cites(text):
            all_unparseable.append((span, str(f)))
        for u in extract_urls(text):
            all_urls.append((u, str(f)))
        for entry in extract_bibliography(text):
            all_bib.append(entry)
            bib_origin.setdefault(entry, []).append(str(f))

    # KB manifest: explicit --kb-manifest paths win; otherwise derive from the
    # scanned path's enclosing run layout (inherited rows keep their kb_slug,
    # so extended runs resolve).
    manifest_paths = [Path(p) for p in args.kb_manifest]
    if not manifest_paths:
        layout = enclosing_layout(Path(args.path))
        if layout is not None:
            manifest_paths = [layout.round1 / "slice_local.jsonl",
                              layout.round1 / "inherited_corpus.jsonl"]
    kb_manifest, kb_duplicates = load_kb_manifest(manifest_paths)
    kb_known, kb_unknown, kb_mismatch, kb_unchecked = check_kb_cites(
        all_kb, kb_manifest)

    bib_unique = list(dict.fromkeys(all_bib))
    # A WEB-only bibliography entry has no 4-digit academic year but carries a
    # URL or an explicit n.d. marker. These are verified through the URL probe
    # (their URLs are already in all_urls), NOT the academic OpenAlex/Crossref
    # resolvers — sending them there would falsely flag them as unresolved.
    academic_bib = [e for e in bib_unique
                    if re.search(r"\b(19|20)\d{2}\b", e)]
    print(f"Files scanned: {len(files)}", flush=True)
    print(f"Inline citations: {len(all_cites)}  Bibliography entries: {len(bib_unique)} "
          f"(academic: {len(academic_bib)})  URLs: {len(all_urls)}", flush=True)

    resolutions = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(resolve_entry, s, e): e for e in academic_bib}
        done = 0
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                resolutions[entry] = fut.result()
            except Exception as exc:
                resolutions[entry] = {"error": str(exc)}
            done += 1
            if done % 10 == 0:
                print(f"  resolved {done}/{len(academic_bib)}", flush=True)

    bib_keys = []
    for entry in bib_unique:
        # First author surname: tolerate "Smith, J.", "Smith J", "van der Berg, A.",
        # "Smith, J., Jones, B., & Brown, C." — take everything before the first comma
        # that's followed by an initial, OR before the first ( year.
        head = re.split(r"\s*\(?\d{4}\)?", entry, maxsplit=1)[0]
        head = re.split(r",\s*(?=[A-Z]\.|[A-Z][a-z]*\s*[A-Z]\.)", head, maxsplit=1)[0]
        surname = first_surname(head)
        year_m = re.search(r"\b(19|20)\d{2}\b", entry)
        if surname and year_m:
            bib_keys.append((surname, year_m.group(0), entry))

    orphans = []
    for c in all_cites:
        # Web citations ([domain, year]) are not matched against the academic
        # bibliography keys — they route to the URL probe instead.
        if c.get("kind") == "web":
            continue
        first_sn = first_surname(c["author"])
        if first_sn and not any(k[0] == first_sn and k[1] == c["year"] for k in bib_keys):
            orphans.append(c)

    # Three-state SSRF-hardened URL probe. Every URL is flagged + KEPT, never
    # dropped. Capped at PROBE_MAX_URLS per run; the overflow is noted.
    probe_unresolved, probe_indeterminate = [], []
    url_truncated = 0
    if args.check_urls:
        # Bare web citations like [treasury.gov, 2024] carry no explicit URL in
        # the prose, so synthesize https://<domain> for them — otherwise a web
        # citation with no bibliography entry AND no inline URL would slip
        # through both orphan detection and the probe entirely.
        web_cite_urls = {
            f"https://{c['domain']}"
            for c in all_cites
            if c.get("kind") == "web" and c.get("domain")
        }
        unique_urls = list({u for u, _ in all_urls} | web_cite_urls)
        if len(unique_urls) > PROBE_MAX_URLS:
            url_truncated = len(unique_urls) - PROBE_MAX_URLS
            unique_urls = unique_urls[:PROBE_MAX_URLS]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(probe_url, u): u for u in unique_urls}
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    pr = fut.result()
                except Exception as exc:
                    pr = ProbeResult("indeterminate", f"probe-error:{exc}", url)
                if pr.state == "unresolved":
                    probe_unresolved.append((url, pr.reason))
                elif pr.state == "indeterminate":
                    probe_indeterminate.append((url, pr.reason))
    n_flagged_urls = len(probe_unresolved) + len(probe_indeterminate)

    unresolved = [e for e, r in resolutions.items() if not r or r.get("error")]
    weak_match = [(e, r) for e, r in resolutions.items() if r and not r.get("error") and r.get("title_match", 0) < 0.4]
    resolved = [(e, r) for e, r in resolutions.items() if r and not r.get("error") and r.get("title_match", 0) >= 0.4]

    out = [
        "# Citation Verification Report",
        "",
        f"- Files scanned: **{len(files)}**",
        f"- Inline citations found: **{len(all_cites)}**",
        f"- Bibliography entries: **{len(bib_unique)}**",
        f"- URLs: **{len(all_urls)}**" + ("" if not args.check_urls else f" — flagged (unresolved+indeterminate): **{n_flagged_urls}**"),
        "",
        "## Summary",
        "",
        f"| Outcome | Count |",
        f"|---|---|",
        f"| Resolved (title match ≥ 0.4) | {len(resolved)} |",
        f"| Weak match (< 0.4) | {len(weak_match)} |",
        f"| Unresolved | {len(unresolved)} |",
        f"| Orphaned inline cites | {len(orphans)} |",
        f"| Links unresolved | {len(probe_unresolved) if args.check_urls else 'not checked'} |",
        f"| Links indeterminate | {len(probe_indeterminate) if args.check_urls else 'not checked'} |",
        f"| KB citations | {len(all_kb)} |",
        f"| Unknown KB citations | {len(kb_unknown)} |",
        f"| KB year mismatch | {len(kb_mismatch)} |",
        f"| KB year unchecked | {len(kb_unchecked)} |",
        f"| Unverifiable citation shapes | {len(all_unparseable)} |",
        "",
    ]

    if kb_unknown:
        out += ["## ⚠ Unknown KB citations", "",
                "No ingested document carries this slug — run ingest_local.py or fix the slug.", ""]
        for c in kb_unknown[:200]:
            out.append(f"- `[kb:{c['slug']}, {c['year']}]` in `{c.get('file', '?')}`")
        out.append("")

    if kb_mismatch:
        out += ["## ⚠ KB year mismatch", "",
                "A cited year contradicts the ingested document's recorded year.", ""]
        for c in kb_mismatch[:200]:
            out.append(f"- `[kb:{c['slug']}, {c['year']}]` in `{c.get('file', '?')}`")
        out.append("")

    if kb_duplicates:
        out += ["## KB duplicate slugs", "",
                "Same slug in more than one manifest (first file wins — the "
                "run's local entry beats an inherited duplicate): "
                + ", ".join(f"`{s_}`" for s_ in kb_duplicates), ""]

    if kb_unchecked:
        out += ["## KB year unchecked", "",
                "The ingested document has no recorded year, so the cited year cannot be "
                "confirmed (info — re-ingest with `--year` to let the verifier confirm).", ""]
        for c in kb_unchecked[:200]:
            out.append(f"- `[kb:{c['slug']}, {c['year']}]` in `{c.get('file', '?')}`")
        out.append("")

    if all_unparseable:
        out += ["## ⚠ Unverifiable citation shapes", "",
                "Citation-ish bracketed spans no verifier can check — they match neither "
                "the academic/web nor the kb citation grammar.", ""]
        for span, fname in all_unparseable[:200]:
            out.append(f"- `{span[:200]}` in `{fname}`")
        out.append("")

    if unresolved:
        out += ["## ⚠ Unresolved bibliography entries", "", "Could not match against OpenAlex or Crossref. Likely hallucinated or non-academic.", ""]
        for e in unresolved[:200]:
            out.append(f"- `{e[:300]}`")
        out.append("")

    if weak_match:
        out += ["## ⚠ Weak title match", "", "Resolved to a work whose title shares few tokens with the citation. Possible misattribution.", ""]
        for e, r in weak_match[:200]:
            out.append(f"- `{e[:200]}` → **{r['title'][:200]}** (match {r['title_match']}, {r['source']})")
        out.append("")

    if orphans:
        out += ["## ⚠ Orphaned inline citations", "", "Inline `[Author, Year]` with no matching bibliography entry.", ""]
        for c in orphans[:200]:
            out.append(f"- `[{c['author']}, {c['year']}]` in `{c['file']}`")
        out.append("")

    if args.check_urls and (probe_unresolved or probe_indeterminate or url_truncated):
        out += [
            "## ⚠ Unresolved links",
            "",
            "Three-state link probe (SSRF-hardened). Every link is **kept** — this "
            "is a flag, not a deletion. `unresolved` = the page is gone (404/410) "
            "or unreachable (DNS/connection). `indeterminate` = we could not "
            "confirm liveness (auth wall, blocked method, rate limit, server "
            "error, TLS problem, timeout, oversize body, or a policy rejection "
            "such as a non-globally-routable host).",
            "",
        ]
        out += ["### Unresolved (page gone / unreachable)", ""]
        if probe_unresolved:
            for u, reason in sorted(probe_unresolved)[:200]:
                out.append(f"- ({reason}) {u}")
        else:
            out.append("- none")
        out.append("")
        out += ["### Indeterminate (could not confirm)", ""]
        if probe_indeterminate:
            for u, reason in sorted(probe_indeterminate)[:200]:
                out.append(f"- ({reason}) {u}")
        else:
            out.append("- none")
        out.append("")
        out += ["### Truncation note", ""]
        if url_truncated:
            out.append(
                f"- {url_truncated} URL(s) beyond the per-run cap of "
                f"{PROBE_MAX_URLS} were not probed (kept, unchecked)."
            )
        else:
            out.append(f"- All URLs within the per-run cap of {PROBE_MAX_URLS} were probed.")
        out.append("")

    if resolved:
        out += ["## ✓ Resolved entries (sample of 50)", ""]
        for e, r in resolved[:50]:
            cited_by = r.get("cited_by", "—")
            out.append(f"- `{e[:120]}` → **{r['title'][:120]}** ({r.get('year','?')}, cited {cited_by}× via {r['source']})")

    output_path = Path(args.output)
    json_path = output_path.with_suffix(".json")
    with standalone_mutation_guard(output_path, operation="verify citations"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(out), encoding="utf-8")
        json_path.write_text(json.dumps({
            "files": [str(f) for f in files],
            "stats": {
                "inline_cites": len(all_cites),
                "bib_entries": len(bib_unique),
                "urls": len(all_urls),
                "resolved": len(resolved),
                "weak_match": len(weak_match),
                "unresolved": len(unresolved),
                "orphans": len(orphans),
                "links_unresolved": len(probe_unresolved) if args.check_urls else None,
                "links_indeterminate": len(probe_indeterminate) if args.check_urls else None,
                "links_truncated": url_truncated if args.check_urls else None,
            },
            "unresolved": unresolved,
            "orphans": orphans,
            "links_unresolved": [{"url": u, "reason": r} for u, r in probe_unresolved],
            "links_indeterminate": [{"url": u, "reason": r} for u, r in probe_indeterminate],
            "kb_cites": len(all_kb),
            "kb_unknown": kb_unknown,
            "kb_year_mismatch": kb_mismatch,
            "kb_year_unchecked": kb_unchecked,
            "kb_duplicate_slugs": kb_duplicates,
            "unparseable_cites": [{"span": span, "file": fname} for span, fname in all_unparseable],
        }, indent=2), encoding="utf-8")
    print(f"\nReport: {args.output}", flush=True)
    print(f"JSON: {json_path}", flush=True)

    # Opt-in strictness: the default exit path stays 0 regardless of findings.
    # (fail_classes was validated up front, before any network/report work.)
    if ("kb-unknown" in fail_classes and kb_unknown) or \
            ("unparseable" in fail_classes and all_unparseable):
        sys.exit(1)


if __name__ == "__main__":
    main()
