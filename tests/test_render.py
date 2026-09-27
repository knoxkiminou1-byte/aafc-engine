"""Rendered-DOM verification tests (require local Playwright + Chromium).

These tests drive the engine's OWN headless Chromium (no mocks, no
pretend browser): fixture pages are rendered for real and the DOM-sensitive
checks run against the rendered DOM. When the renderer is unavailable the
tests skip gracefully — the engine's HTTP-only fallback is covered by the
rest of the suite.
"""

from __future__ import annotations

import pytest

from aafc_engine.auditor import render as render_provider
from aafc_engine.auditor.engine import (
    PageParser,
    _dom_checks,
    _dom_from_parser,
    _reconcile_dom_findings,
    audit_site,
    check_page,
)


def _need_render():
    if not render_provider.render_available():
        pytest.skip("Playwright/Chromium not installed — render tests need a local browser")


def _write(tmp_path, name, html):
    p = tmp_path / name
    p.write_text(html, encoding="utf-8")
    return p.as_uri()


def _res_for(html, url="https://example.test/"):
    return {
        "ok": True, "status": 200, "text": html, "final_url": url,
        "headers": {"Content-Type": "text/html"}, "elapsed": 0.1,
        "bytes": len(html.encode()), "redirects": 0,
    }


def _parsed(html):
    parser = PageParser()
    parser.feed(html)
    return parser


# A page whose H1 exists only after JavaScript runs: the raw HTML has no
# H1, the rendered DOM has exactly one.
JS_H1_HTML = """<!DOCTYPE html><html><head>
<title>JS H1 fixture</title>
<meta name="description" content="A fixture page whose headline is injected by JavaScript.">
<link rel="canonical" href="https://example.test/js-h1">
</head><body>
<div id="app"></div>
<script>document.getElementById("app").innerHTML = "<h1>Injected headline</h1>";</script>
</body></html>"""

# Hidden images (display:none) carry empty alt in the markup; the one
# visible image has descriptive alt text. Raw HTML sees three <img>;
# the rendered DOM must see exactly one.
HIDDEN_IMG_HTML = """<!DOCTYPE html><html><head>
<title>Hidden image fixture</title>
<meta name="description" content="A fixture page with hidden images that must not count.">
<link rel="canonical" href="https://example.test/hidden-img">
</head><body>
<h1>Visible headline</h1>
<div style="display:none">
<img src="hidden1.png" alt="">
<img src="hidden2.png" alt="">
</div>
<img src="visible.png" alt="A descriptive caption of the visible photo">
</body></html>"""

# The static HTML lacks a meta description; JavaScript injects one, so the
# rendered DOM contradicts the raw parse.
JS_META_HTML = """<!DOCTYPE html><html><head>
<title>JS meta fixture</title>
<link rel="canonical" href="https://example.test/js-meta">
</head><body>
<h1>Headline</h1>
<script>
var m = document.createElement("meta");
m.name = "description";
m.content = "Injected description that is long enough to satisfy the length check comfortably.";
document.head.appendChild(m);
</script>
</body></html>"""


def test_js_injected_h1_seen_in_rendered_dom(tmp_path):
    _need_render()
    file_url = _write(tmp_path, "js-h1.html", JS_H1_HTML)
    rendered = render_provider.render_pages([file_url])
    assert file_url in rendered, "fixture page did not render"
    dom = render_provider.dom_from_rendered(file_url, rendered[file_url])
    assert dom["h1s"] == ["Injected headline"]


def test_hidden_images_excluded_from_rendered_dom(tmp_path):
    _need_render()
    file_url = _write(tmp_path, "hidden-img.html", HIDDEN_IMG_HTML)
    rendered = render_provider.render_pages([file_url])
    assert file_url in rendered, "fixture page did not render"
    dom = render_provider.dom_from_rendered(file_url, rendered[file_url])
    # Only the visible image survives; its alt is descriptive -> no finding.
    assert [i["src"] for i in dom["images"]] == [file_url.rsplit("/", 1)[0] + "/visible.png"]
    findings = _dom_checks(file_url, dom, False, lambda c: False, "rendered")
    assert "img_alt" not in {f["check"] for f in findings}


def test_rendered_h1_overrides_static_html(tmp_path):
    """Full check_page: the raw parse screams 'no H1'; the rendered DOM has
    one, so the HTTP finding must be dropped, not scored."""
    _need_render()
    file_url = _write(tmp_path, "js-h1.html", JS_H1_HTML)
    rendered = render_provider.render_pages([file_url])
    assert file_url in rendered
    parser = _parsed(JS_H1_HTML)
    findings, _stats = check_page(
        file_url, _res_for(JS_H1_HTML), parser,
        rendered=rendered[file_url],
    )
    h1_findings = [f for f in findings if f["check"] == "h1"]
    assert h1_findings == [], f"rendered H1 should have killed the raw finding: {h1_findings}"


def test_rendered_meta_contradiction_drops_http_finding():
    """Reconcile rule, unit level: rendered evidence outranks raw HTML."""
    _need_render()
    http_dom = _dom_from_parser("https://example.test/js-meta", _parsed(JS_META_HTML))
    http_findings = _dom_checks("https://example.test/js-meta", http_dom,
                               False, lambda c: False, "http")
    assert "meta_description" in {f["check"] for f in http_findings}
    # Simulate the rendered DOM (JS injected the description).
    rendered_dom = dict(http_dom)
    rendered_dom["description"] = (
        "Injected description that is long enough to satisfy the length check comfortably.")
    rendered_findings = _dom_checks("https://example.test/js-meta", rendered_dom,
                                   False, lambda c: False, "rendered")
    assert "meta_description" not in {f["check"] for f in rendered_findings}
    reconciled = _reconcile_dom_findings(http_findings, rendered_findings)
    assert "meta_description" not in {f["check"] for f in reconciled}
    # Rendered findings carry the honest verification label.
    for f in rendered_findings:
        assert f["verification"] == "RENDERED_DOM"


def test_rendered_findings_marked_rendered_dom():
    _need_render()
    dom = {
        "title": "", "description": "", "canonical_count": 0,
        "h1s": [], "headings": [], "images": [],
    }
    findings = _dom_checks("https://example.test/x", dom, False,
                           lambda c: False, "rendered")
    assert findings, "expected findings for an empty DOM"
    assert {f["verification"] for f in findings} == {"RENDERED_DOM"}
    http_findings = _dom_checks("https://example.test/x", dom, False,
                               lambda c: False, "http")
    assert {f["verification"] for f in http_findings} == {"HTTP_FETCH"}


def test_audit_site_reports_render_mode(monkeypatch, tmp_path):
    """audit_site(render=True) against a local fixture server: the result
    must say how its DOM findings were verified."""
    _need_render()
    (tmp_path / "index.html").write_text(JS_H1_HTML, encoding="utf-8")
    import functools
    import http.server
    import threading
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            "aafc_engine.auditor.engine.check_robots_and_sitemap",
            lambda base, **kw: ([], {"robots": False, "sitemap": None}),
        )
        site = audit_site(f"http://127.0.0.1:{port}/index.html",
                          max_pages=1, render=True)
    finally:
        server.shutdown()
        thread.join()
    assert site["render"]["mode"] == "rendered"
    assert site["render"]["pages_rendered"] == 1
    assert "h1" not in {f["check"] for f in site["findings"]}


def test_render_available_probes_real_browser():
    """render_available() must reflect a launchable browser, not the import.

    Positive: with the local Chromium installed it is True. The cache is
    reset afterwards so no test leaks probe state into another.
    """
    _need_render()  # skips when there is genuinely no browser to probe
    render_provider._available = None
    try:
        assert render_provider.render_available() is True
    finally:
        render_provider._available = None


def test_render_available_false_without_playwright(monkeypatch):
    """With the Playwright import blocked, the probe reports False (and the
    engine would run HTTP-only) instead of claiming a renderer."""
    import sys

    monkeypatch.setattr(render_provider, "_available", None)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert render_provider.render_available() is False
    assert render_provider.render_pages(["https://example.test/"]) == {}


def test_audit_site_render_requested_but_no_renderer(monkeypatch, tmp_path):
    """render=True with no usable browser -> honest http_only_unavailable mode,
    never a crash, never fake rendered findings."""
    (tmp_path / "index.html").write_text(
        "<html><head><title>T</title></head><body><h1>Hi</h1></body></html>",
        encoding="utf-8",
    )
    import functools
    import http.server
    import threading
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            "aafc_engine.auditor.engine.check_robots_and_sitemap",
            lambda base, **kw: ([], {"robots": False, "sitemap": None}),
        )
        monkeypatch.setattr(render_provider, "_available", None)
        import sys
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        site = audit_site(f"http://127.0.0.1:{port}/index.html",
                          max_pages=1, render=True)
    finally:
        server.shutdown()
        thread.join()
    assert site["render"]["mode"] == "http_only_unavailable"
    assert site["render"]["pages_rendered"] == 0
    assert site["status"] == "COMPLETE"
    # No rendered findings when nothing was rendered.
    assert "RENDERED_DOM" not in {f.get("verification") for f in site["findings"]}


def test_check_broken_links_stops_when_budget_spent(monkeypatch):
    """A spent time budget truncates the broken-link check: zero fetches,
    honest stats, no crash."""
    import aafc_engine.auditor.engine as eng

    html = ("<html><head><title>T</title></head><body>"
            + "".join(f'<a href="/p{i}">x</a>' for i in range(10))
            + "</body></html>")
    parser = eng.PageParser()
    parser.feed(html)
    calls = []
    monkeypatch.setattr(
        eng, "fetch",
        lambda *a, **k: (calls.append(a),
                         {"ok": False, "status": None, "error": "x"})[1],
    )
    findings, stats = eng.check_broken_links(
        "https://example.test/", parser, time_left=lambda: 0)
    assert calls == []
    assert stats == {"links_checked": 0, "broken": 0}
    assert findings == []


def test_check_broken_links_runs_without_budget(monkeypatch):
    """Without a budget the check runs the full pool (cap-bounded)."""
    import aafc_engine.auditor.engine as eng

    html = ("<html><head><title>T</title></head><body>"
            + "".join(f'<a href="/p{i}">x</a>' for i in range(5))
            + "</body></html>")
    parser = eng.PageParser()
    parser.feed(html)
    monkeypatch.setattr(
        eng, "fetch",
        lambda *a, **k: {"ok": True, "status": 200, "error": None},
    )
    findings, stats = eng.check_broken_links(
        "https://example.test/", parser)
    assert stats["links_checked"] == 5
    assert findings == []


def test_render_pages_watchdog_bounds_hung_browser(monkeypatch):
    """A wedged browser/driver must never hang the caller: the watchdog
    returns whatever rendered (here: nothing) once total_timeout_s elapses."""
    import time

    import aafc_engine.auditor.render as rm

    monkeypatch.setattr(rm, "render_available", lambda: True)

    def _hung(urls, timeout_ms):
        time.sleep(30)
        return {"never": "reached"}

    monkeypatch.setattr(rm, "_render_batch", _hung)
    t0 = time.time()
    out = rm.render_pages(["https://example.test/"], total_timeout_s=2)
    elapsed = time.time() - t0
    assert out == {}
    assert elapsed < 15, f"watchdog did not fire in time ({elapsed:.1f}s)"


def test_render_mode_degraded_when_renderer_present_but_no_page_renders(monkeypatch):
    """Honest mode reporting: renderer available + requested, but every
    render failed/timed out -> 'render_degraded', never a false
    'http_only_unavailable' (which means no usable renderer)."""
    import aafc_engine.auditor.render as rm
    from aafc_engine.auditor import engine as eng

    monkeypatch.setattr(rm, "_available", True)
    monkeypatch.setattr(rm, "render_pages", lambda urls, **kw: {})

    site = eng.audit_site("https://example.com", max_pages=1, render=True,
                          time_budget=60)
    assert site["render"]["mode"] == "render_degraded"
    assert site["render"]["pages_rendered"] == 0
