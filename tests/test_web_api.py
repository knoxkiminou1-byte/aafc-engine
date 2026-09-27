"""End-to-end tests for the hosted Quick Audit API.

These tests drive the REAL audit engine through the REAL API layer — no
mocks, no stubs, no faked results:

* a local fixture website is served over real HTTP,
* the Flask app from ``api/index.py`` receives a real POST /api/audit,
* ``aafc_engine.auditor.engine.audit_site`` runs for real (HTTP-only quick
  mode, as deployed),
* the test asserts a genuine audit result: status, score, per-page stats,
  and structured per-page findings.

The SSRF guard is bypassed ONLY for the loopback fixture host via
monkeypatch; a separate test asserts the guard still rejects non-public
targets when unpatched.
"""

from __future__ import annotations

import functools
import http.server
import threading
import time

import pytest

from api.index import (
    QUICK_MAX_PAGES,
    QUICK_TIME_BUDGET,
    app,
    get_job,
    run_quick_audit,
)

INDEX_HTML = """<!DOCTYPE html><html><head>
<title>Fixture Bakery — fresh bread daily</title>
<meta name="description" content="Fixture Bakery bakes fresh sourdough and croissants every morning in Testville.">
<link rel="canonical" href="{base}/">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<h1>Fresh bread daily</h1>
<p>Welcome to the Fixture Bakery, serving Testville since 2020.</p>
<a href="{base}/about.html">About us</a>
<img src="{base}/loaf.jpg" alt="A crusty sourdough loaf on a wooden table">
</body></html>"""

ABOUT_HTML = """<!DOCTYPE html><html><head>
<title>About our story — Fixture Bakery Testville</title>
<meta name="description" content="Learn about Fixture Bakery: our story, our bakers, and our ovens.">
<link rel="canonical" href="{base}/about.html">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<h1>About our bakery</h1>
<p>We bake everything from scratch.</p>
<a href="{base}/">Home</a>
</body></html>"""


@pytest.fixture()
def fixture_site(tmp_path):
    (tmp_path / "index.html").write_text(
        INDEX_HTML.format(base="__BASE__"), encoding="utf-8")
    (tmp_path / "about.html").write_text(
        ABOUT_HTML.format(base="__BASE__"), encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    # Rewrite the placeholder now that the port is known.
    for name, tpl in (("index.html", INDEX_HTML), ("about.html", ABOUT_HTML)):
        (tmp_path / name).write_text(tpl.format(base=base), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture()
def allow_loopback(monkeypatch):
    """Let the API's SSRF guard pass for the local fixture server only.

    Patches the initial-host check; the engine-level fetch guard stays
    active (scoped per audit call) and consults the same patched check, so
    redirect targets are covered too. A separate test asserts the guard
    still rejects non-public hosts in production configuration.
    """
    import api.index as api_mod
    monkeypatch.setattr(api_mod, "_host_is_public", lambda host: True)


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _assert_genuine_result(result, base):
    """A genuine audit result: status, score, per-page findings, the works."""
    assert result["status"] == "COMPLETE"
    assert result["mode"] == "quick"
    assert result["score"] is not None and 0 <= result["score"] <= 100
    assert result["grade"] in ("A", "B", "C", "D", "F")
    assert result["pages_crawled"] >= 1
    assert result["pages_crawled"] <= QUICK_MAX_PAGES
    assert result["render"]["mode"] == "http_only_requested"
    assert isinstance(result["findings"], list) and result["findings"]
    for f in result["findings"]:
        for key in ("page", "check", "severity", "title", "evidence",
                    "why_it_matters", "recommended_fix"):
            assert f[key], f"finding missing {key}: {f}"
        assert f["severity"] in ("critical", "warning", "info")
        assert f["page"].startswith(base)
    assert result["elapsed_total"] < QUICK_TIME_BUDGET


def test_run_quick_audit_uses_real_engine(fixture_site, allow_loopback):
    result = run_quick_audit(fixture_site + "/")
    _assert_genuine_result(result, fixture_site)
    # The fixture has solid SEO basics (title/description/h1/canonical all
    # present and well-formed); its deductions are the honest HTTP-only and
    # missing-CTA ones, which proves the engine really ran the checks.
    checks = {f["check"] for f in result["findings"]
              if f["severity"] in ("critical", "warning")}
    assert not ({"title", "meta_description", "h1", "canonical"} & checks)
    assert "https" in checks  # plain-http fixture: the engine noticed


def test_post_audit_end_to_end(client, fixture_site, allow_loopback):
    resp = client.post("/api/audit", json={"url": fixture_site + "/"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["id"]
    assert payload["status"] == "COMPLETE"
    assert payload["mode"] == "quick"
    _assert_genuine_result(payload["result"], fixture_site)
    # The job id resolves via GET (warm instance).
    assert get_job(payload["id"]) is not None


def test_get_audit_roundtrip(client, fixture_site, allow_loopback):
    posted = client.post("/api/audit", json={"url": fixture_site + "/"}).get_json()
    got = client.get(f"/api/audit?id={posted['id']}")
    assert got.status_code == 200
    assert got.get_json()["id"] == posted["id"]
    assert got.get_json()["status"] == "COMPLETE"


def test_get_unknown_id_is_honest_404(client):
    resp = client.get("/api/audit?id=does-not-exist")
    assert resp.status_code == 404
    body = resp.get_json()["error"]
    # Honest: says the id doesn't resolve, never pretends it is "still running".
    assert "not found" in body.lower()
    assert "running" not in body.lower()


def test_post_invalid_url_400(client):
    for bad in ("", "not a url", "ftp://example.com/x", "http://"):
        resp = client.post("/api/audit", json={"url": bad})
        assert resp.status_code == 400, bad
        assert resp.get_json()["error"]


def test_ssrf_guard_blocks_non_public_host(client):
    # Unpatched guard: loopback must be rejected by the public endpoint.
    resp = client.post("/api/audit", json={"url": "http://127.0.0.1:9/"})
    assert resp.status_code == 400


def test_fetch_guard_vetoes_private_redirect_target(monkeypatch):
    """A page that 302-redirects to a private IP must not be followed: the
    engine records a blocked fetch and never opens the private connection.
    Uses the production guard wiring (real ``_fetch_guard`` scoped via
    ``engine.fetch_guard``); only the fixture host is allow-listed so the
    redirect target stays blocked."""
    import api.index as api_mod
    from aafc_engine.auditor import engine as eng

    monkeypatch.setattr(
        api_mod, "_host_is_public", lambda host: host == "127.0.0.1")

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with eng.fetch_guard(api_mod._fetch_guard):
            res = eng.fetch(f"http://127.0.0.1:{port}/go")
    finally:
        server.shutdown()
        thread.join()
    assert res["ok"] is False
    assert res["error"] and "Blocked" in res["error"]


def test_importing_api_does_not_arm_engine_guard():
    """Regression: importing the API module must not change engine behavior
    for other callers — the guard is scoped per audit call, never set at
    import time (this once broke engine unit tests process-globally)."""
    import api.index  # noqa: F401  (import for the side-effect check)
    from aafc_engine.auditor import engine as eng

    assert eng.FETCH_GUARD is None


def test_quick_audit_limits_are_server_side(fixture_site, allow_loopback):
    """Client-supplied largesse cannot widen the quick-audit budget."""
    result = run_quick_audit(fixture_site + "/", max_pages=99, time_budget=999)
    assert result["limits"]["max_pages"] == QUICK_MAX_PAGES
    assert result["limits"]["time_budget_seconds"] == QUICK_TIME_BUDGET
    assert result["pages_crawled"] <= QUICK_MAX_PAGES


# ---------------------------------------------------------------------------
# Hardening: wedged DNS / tarpitted targets must yield PARTIAL, never a 504
# ---------------------------------------------------------------------------
def test_dns_hang_fails_closed_fast(monkeypatch):
    """socket.getaddrinfo has no timeout of its own: a wedged resolver must
    not hang the guard. Found live: pizzahut.com 504'd the function."""
    import api.index as api_mod
    import threading

    def _hang(host, port, *a, **k):
        threading.Event().wait(120)  # never answers
        raise OSError("unreachable")

    monkeypatch.setattr(api_mod.socket, "getaddrinfo", _hang)
    t0 = time.time()
    assert api_mod._host_is_public("wedged-resolver-12345.test") is False
    assert time.time() - t0 < api_mod._DNS_TIMEOUT + 5


def test_wedged_audit_returns_partial_not_hang(monkeypatch):
    """If the engine itself wedges (tarpitted socket defeating per-request
    timeouts), the hard cap still returns an honest PARTIAL quickly."""
    import time as time_mod
    import api.index as api_mod
    from aafc_engine.auditor import engine as eng

    def _wedged(*a, **k):
        time_mod.sleep(120)

    monkeypatch.setattr(eng, "audit_site", _wedged)
    monkeypatch.setattr(api_mod, "QUICK_HARD_CAP", 3.0)
    t0 = time_mod.time()
    result = run_quick_audit("https://example.com/")
    dt = time_mod.time() - t0
    assert dt < 10, f"hard cap not enforced: {dt:.1f}s"
    assert result["status"] == "PARTIAL"
    assert result["score"] is None
    assert result["grade"] is None
    assert result["mode"] == "quick"
    assert result["limits"]["time_budget_seconds"] == QUICK_TIME_BUDGET
    # Honest: exactly one explanatory finding, no fabricated checks.
    assert len(result["findings"]) == 1
    f = result["findings"][0]
    assert f["check"] == "timeout"
    for key in ("page", "severity", "title", "evidence",
                "why_it_matters", "recommended_fix"):
        assert f[key], f"timeout finding missing {key}"
