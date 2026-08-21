"""SSRF-hardened URL probe tests — OFFLINE ONLY.

Every test monkeypatches `socket.getaddrinfo` (DNS) and the HTTP transport
(`requests.adapters.HTTPAdapter.send`) so that NO real network call is ever
made. If a test tried to hit the network it would fail: the fake adapter is the
only transport, and getaddrinfo is stubbed to controlled answers.

The probe under test lives in scripts/verify_citations.py:

    probe_url(url) -> ProbeResult(state, reason)
    state in {"resolved", "unresolved", "indeterminate"}

Security invariants exercised here:
  - scheme allowlist (http/https), no userinfo, port allowlist (80/443)
  - getaddrinfo UP FRONT; EVERY answer must be globally routable or -> policy
  - IPv4-mapped IPv6 unwrapped before the is_global check
  - CONNECT pinned to the first vetted IP (host rewritten, Host header original)
  - TLS verification stays ON (assert_hostname / server_hostname on original host)
  - session.trust_env is False (proxy env cannot bypass the pin)
  - redirects re-parsed / re-resolved / re-vetted / re-pinned; DNS-rebinding caught
  - 512KB read cap; state mapping per spec
  - a source-grep test forbids the literal `verify=False`
"""

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import verify_citations as vc


# ---------------------------------------------------------------------------
# Fakes: DNS + HTTP transport
# ---------------------------------------------------------------------------

def make_getaddrinfo(host_to_ips):
    """Return a fake getaddrinfo. `host_to_ips` maps hostname -> list of IPs
    OR a callable(host, port) -> list of IPs (for rebinding: answer changes per
    call). Each IP string is classified AF_INET vs AF_INET6 by the presence of
    a colon. Raises socket.gaierror for an unknown host (DNS failure)."""
    call_state = {"n": 0}

    def fake(host, port, *a, **kw):
        call_state["n"] += 1
        ips = host_to_ips.get(host)
        if ips is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if callable(ips):
            ips = ips(host, port, call_state["n"])
        out = []
        for ip in ips:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
            out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    return fake


import requests


def _build_response(status_code, headers, body, request):
    """Build a real requests.Response so Session.send post-processing (history,
    cookies, url) works, but wired to an offline body — no socket is touched."""
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    resp.reason = ""
    resp.request = request
    resp.url = getattr(request, "url", "")

    class _FakeRaw:
        def __init__(self, data):
            self._data = data
            self.headers = {}

        def stream(self, amt=65536, decode_content=True):
            for i in range(0, len(self._data), amt):
                yield self._data[i:i + amt]

        def read(self, amt=None, decode_content=True, cache_content=False):
            return self._data

        def release_conn(self):
            pass

        def close(self):
            pass

    resp.raw = _FakeRaw(body)
    return resp


def make_send(responder):
    """Return a fake HTTPAdapter.send(self, request, **kw). `responder` is a
    callable(request) -> spec | Exception, where spec is a (status, headers, body)
    tuple. Raising the returned exception simulates a transport error
    (connection refused / TLS error / timeout). No real socket is ever opened."""

    def fake_send(self, request, **kw):
        result = responder(request)
        if isinstance(result, BaseException):
            raise result
        status, headers, body = result
        return _build_response(status, headers, body, request)

    return fake_send


def resp(status_code, headers=None, body=b""):
    """Shorthand for a (status, headers, body) responder spec."""
    return (status_code, headers or {}, body)


def patch_all(monkeypatch, host_to_ips, responder):
    monkeypatch.setattr(socket, "getaddrinfo", make_getaddrinfo(host_to_ips))
    monkeypatch.setattr(
        "requests.adapters.HTTPAdapter.send", make_send(responder)
    )


# ---------------------------------------------------------------------------
# Policy rejections (never connect)
# ---------------------------------------------------------------------------

def test_bad_scheme_is_policy(monkeypatch):
    patch_all(monkeypatch, {}, lambda req: resp(200))
    r = vc.probe_url("ftp://example.com/x")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


def test_userinfo_url_is_policy(monkeypatch):
    # user@host in the authority must be rejected before any DNS/connect.
    patch_all(monkeypatch, {"example.com": ["93.184.216.34"]},
              lambda req: resp(200))
    r = vc.probe_url("https://user:pass@example.com/x")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


def test_bad_port_is_policy(monkeypatch):
    patch_all(monkeypatch, {"example.com": ["93.184.216.34"]},
              lambda req: resp(200))
    r = vc.probe_url("https://example.com:8080/x")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


def test_multi_answer_one_private_is_policy_no_connect(monkeypatch):
    # getaddrinfo returns a good public IP AND a private one. Any non-global
    # answer poisons the whole set: policy-reject, and NEVER connect.
    connected = {"hit": False}

    def responder(req):
        connected["hit"] = True
        return resp(200)

    patch_all(monkeypatch,
              {"evil.example": ["93.184.216.34", "10.0.0.5"]},
              responder)
    r = vc.probe_url("https://evil.example/x")
    assert r.state == "indeterminate"
    assert r.reason == "policy"
    assert connected["hit"] is False


def test_ipv4_mapped_ipv6_loopback_is_policy(monkeypatch):
    # ::ffff:127.0.0.1 must be UNWRAPPED to 127.0.0.1 then rejected.
    patch_all(monkeypatch,
              {"sneaky.example": ["::ffff:127.0.0.1"]},
              lambda req: resp(200))
    r = vc.probe_url("https://sneaky.example/x")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


def test_link_local_is_policy(monkeypatch):
    # AWS metadata endpoint style — 169.254.x is link-local, not global.
    patch_all(monkeypatch,
              {"metadata.example": ["169.254.169.254"]},
              lambda req: resp(200))
    r = vc.probe_url("http://metadata.example/latest/meta-data/")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


# ---------------------------------------------------------------------------
# DNS-rebinding across a redirect (re-resolve + re-vet each hop)
# ---------------------------------------------------------------------------

def test_dns_rebinding_on_redirect_is_caught(monkeypatch):
    # First resolve of host1 -> good public IP; the 302 sends us to host2,
    # whose SECOND resolution returns a private IP. The redirect re-vet must
    # catch it and return policy (never following to the internal target).
    def host2_answer(host, port, n):
        return ["10.1.2.3"]

    host_map = {
        "public.example": ["93.184.216.34"],
        "rebind.example": host2_answer,
    }

    def responder(req):
        # The initial request lands on the pinned public IP; respond 302 to host2.
        if "public.example" in req.headers.get("Host", "") or "93.184.216.34" in req.url:
            return resp(302, headers={"Location": "https://rebind.example/internal"})
        # Should never reach here for host2 (policy-rejected before connect).
        return resp(200)

    patch_all(monkeypatch, host_map, responder)
    r = vc.probe_url("https://public.example/start")
    assert r.state == "indeterminate"
    assert r.reason == "policy"


def test_redirect_to_public_resolves(monkeypatch):
    host_map = {
        "public.example": ["93.184.216.34"],
        "other.example": ["93.184.216.35"],
    }

    def responder(req):
        host = req.headers.get("Host", "")
        if host == "public.example":
            return resp(302, headers={"Location": "https://other.example/final"})
        return resp(200, body=b"ok")

    patch_all(monkeypatch, host_map, responder)
    r = vc.probe_url("https://public.example/start")
    assert r.state == "resolved"


def test_too_many_redirects_is_indeterminate(monkeypatch):
    host_map = {"loop.example": ["93.184.216.34"]}

    def responder(req):
        return resp(302, headers={"Location": "https://loop.example/next"})

    patch_all(monkeypatch, host_map, responder)
    r = vc.probe_url("https://loop.example/start")
    assert r.state == "indeterminate"


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------

def test_200_resolved(monkeypatch):
    patch_all(monkeypatch, {"ok.example": ["93.184.216.34"]},
              lambda req: resp(200, body=b"hello"))
    r = vc.probe_url("https://ok.example/")
    assert r.state == "resolved"


def test_404_unresolved(monkeypatch):
    patch_all(monkeypatch, {"gone.example": ["93.184.216.34"]},
              lambda req: resp(404))
    r = vc.probe_url("https://gone.example/missing")
    assert r.state == "unresolved"


def test_410_unresolved(monkeypatch):
    patch_all(monkeypatch, {"gone.example": ["93.184.216.34"]},
              lambda req: resp(410))
    r = vc.probe_url("https://gone.example/gone")
    assert r.state == "unresolved"


def test_403_indeterminate(monkeypatch):
    patch_all(monkeypatch, {"paywall.example": ["93.184.216.34"]},
              lambda req: resp(403))
    r = vc.probe_url("https://paywall.example/")
    assert r.state == "indeterminate"


def test_500_indeterminate(monkeypatch):
    patch_all(monkeypatch, {"broken.example": ["93.184.216.34"]},
              lambda req: resp(500))
    r = vc.probe_url("https://broken.example/")
    assert r.state == "indeterminate"


def test_dns_failure_unresolved(monkeypatch):
    # host absent from the map -> gaierror -> DNS resolution failure -> unresolved.
    patch_all(monkeypatch, {}, lambda req: resp(200))
    r = vc.probe_url("https://nxdomain.invalid/")
    assert r.state == "unresolved"


def test_connection_refused_unresolved(monkeypatch):
    import requests
    patch_all(monkeypatch, {"down.example": ["93.184.216.34"]},
              lambda req: requests.exceptions.ConnectionError("refused"))
    r = vc.probe_url("https://down.example/")
    assert r.state == "unresolved"


def test_timeout_indeterminate(monkeypatch):
    import requests
    patch_all(monkeypatch, {"slow.example": ["93.184.216.34"]},
              lambda req: requests.exceptions.Timeout("timed out"))
    r = vc.probe_url("https://slow.example/")
    assert r.state == "indeterminate"


def test_tls_error_indeterminate(monkeypatch):
    import requests
    patch_all(monkeypatch, {"badtls.example": ["93.184.216.34"]},
              lambda req: requests.exceptions.SSLError("cert mismatch"))
    r = vc.probe_url("https://badtls.example/")
    assert r.state == "indeterminate"


def test_oversize_truncation_indeterminate(monkeypatch):
    # Body larger than the 512KB read cap -> oversize-truncation -> indeterminate.
    big = b"x" * (600 * 1024)
    patch_all(monkeypatch, {"huge.example": ["93.184.216.34"]},
              lambda req: resp(200, body=big,
                                       headers={"Content-Length": str(len(big))}))
    r = vc.probe_url("https://huge.example/")
    assert r.state == "indeterminate"
    assert "trunc" in r.reason.lower() or r.reason == "oversize"


# ---------------------------------------------------------------------------
# trust_env / proxy pin
# ---------------------------------------------------------------------------

def test_probe_session_ignores_proxy_env(monkeypatch):
    # A proxy would bypass the IP pin, so trust_env must be False.
    s = vc._probe_session("ok.example", "93.184.216.34")
    assert s.trust_env is False


# ---------------------------------------------------------------------------
# IP pin + SNI: connect target is the vetted IP; Host + TLS hostname stay original
# ---------------------------------------------------------------------------

def test_connect_target_is_pinned_ip_and_host_is_original(monkeypatch):
    seen = {}

    def responder(req):
        # The PreparedRequest.url is what urllib3 would connect to: it must be
        # the vetted IP, NOT the original hostname. The Host header must be the
        # ORIGINAL hostname so the server routes correctly behind the pin.
        seen["url"] = req.url
        seen["host_header"] = req.headers.get("Host")
        return resp(200, body=b"ok")

    patch_all(monkeypatch, {"pin.example": ["93.184.216.34"]}, responder)
    r = vc.probe_url("https://pin.example/page")
    assert r.state == "resolved"
    assert "93.184.216.34" in seen["url"], seen["url"]
    assert "pin.example" not in seen["url"], "host must be rewritten to the IP"
    assert seen["host_header"] == "pin.example"


def test_https_adapter_pins_sni_to_original_host():
    # The HTTPS adapter must drive SNI (server_hostname) and re-assert the cert
    # hostname (assert_hostname) against the ORIGINAL host — so TLS is verified
    # against pin.example even though we connect to the pinned IP.
    s = vc._probe_session("pin.example", "93.184.216.34")
    adapter = s.get_adapter("https://pin.example/")
    kw = adapter.poolmanager.connection_pool_kw
    assert kw.get("server_hostname") == "pin.example"
    assert kw.get("assert_hostname") == "pin.example"


# ---------------------------------------------------------------------------
# Source grep: verify=False must NOT appear anywhere in the module source
# ---------------------------------------------------------------------------

def test_no_verify_false_in_source():
    src = (ROOT / "scripts" / "verify_citations.py").read_text(encoding="utf-8")
    assert "verify=False" not in src, (
        "verify=False is FORBIDDEN in verify_citations.py — TLS verification "
        "must stay ON against the original hostname."
    )


# --- NAT64-wrapped metadata must be rejected (defense-in-depth, locks the guarantee) ---

def test_nat64_wrapped_metadata_rejected():
    from scripts.verify_citations import _unwrap_mapped, _is_globally_routable
    import ipaddress
    # 64:ff9b::169.254.169.254 (AWS metadata via NAT64). is_global is True for the
    # wrapper, so this must be caught by the unwrap AND the explicit prefix reject.
    nat64 = "64:ff9b::a9fe:a9fe"
    unwrapped = _unwrap_mapped(nat64)
    assert unwrapped == ipaddress.IPv4Address("169.254.169.254")
    assert _is_globally_routable(unwrapped) is False
    # even without unwrap, the raw NAT64 address is rejected
    assert _is_globally_routable(ipaddress.ip_address(nat64)) is False


def test_nat64_wrapped_public_ip_still_reachable():
    # a NAT64-wrapped PUBLIC ip (8.8.8.8) unwraps to a routable address
    from scripts.verify_citations import _unwrap_mapped, _is_globally_routable
    import ipaddress
    unwrapped = _unwrap_mapped("64:ff9b::808:808")
    assert unwrapped == ipaddress.IPv4Address("8.8.8.8")
    assert _is_globally_routable(unwrapped) is True
