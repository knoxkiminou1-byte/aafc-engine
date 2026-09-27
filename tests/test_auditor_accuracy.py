"""Auditor accuracy regressions (FIX 1-7, 2026-09-26).

Fictitious HTML only -- no network except via monkeypatched fetch.
Covers: redirect following + browser-like UA + redirect cap (FIX 1),
BLOCKED status with no score/grade (FIX 4), utility-page rules (FIX 3),
JS-shell manual review (FIX 6), and the img-alt rendered-reality fix
(FIX 7, from browser ground truth on franklinbarbecue.com).
"""
from __future__ import annotations

import pytest
import requests

from aafc_engine.auditor import engine
from aafc_engine.auditor.engine import PageParser, check_page, fetch, page_kind, audit_site
from aafc_engine import findings as findings_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_res(url, html, status=200, headers=None):
    return {
        "ok": True, "status": status, "final_url": url,
        "headers": headers or {"Content-Type": "text/html; charset=utf-8"},
        "text": html, "bytes": len(html.encode()), "elapsed": 0.2,
        "redirects": 0, "redirect_chain": [], "error": None,
    }


def parse_and_check(url, html, **res_kw):
    parser = PageParser()
    parser.feed(html)
    res = make_res(url, html, **res_kw)
    found, stats = check_page(url, res, parser)
    return found, stats


def checks_of(found):
    return {f["check"] for f in found}


class FakeResponse:
    def __init__(self, status_code, url, text="", history=None, headers=None):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.content = text.encode()
        self.history = history or []
        self.headers = headers or {"Content-Type": "text/html"}


class FakeSession:
    instances = []

    def __init__(self, script):
        self.script = script
        self.captured_headers = None
        FakeSession.instances.append(self)

    def request(self, method, url, headers=None, timeout=None, allow_redirects=True):
        self.captured_headers = headers or {}
        result = self.script[url]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_transport(monkeypatch):
    FakeSession.instances = []

    def _install(script):
        monkeypatch.setattr(
            engine.requests, "Session", lambda: FakeSession(script)
        )
        return FakeSession

    return _install


# ---------------------------------------------------------------------------
# FIX 1: redirects, UA, redirect cap
# ---------------------------------------------------------------------------
def test_redirects_followed_to_final_200(fake_transport):
    redir = FakeResponse(301, "https://example.com/old")
    final = FakeResponse(200, "https://example.com/new", text="<html></html>",
                         history=[redir])
    fake_transport({"https://example.com/old": final})
    res = fetch("https://example.com/old")
    assert res["ok"] is True
    assert res["status"] == 200
    assert res["final_url"] == "https://example.com/new"
    assert res["redirects"] == 1
    assert res["redirect_chain"] == ["https://example.com/old"]


def test_fetch_uses_browser_like_honest_ua(fake_transport):
    fake_transport({"https://example.com/": FakeResponse(200, "https://example.com/")})
    fetch("https://example.com/")
    ua = FakeSession.instances[-1].captured_headers.get("User-Agent", "")
    assert "Chrome" in ua  # browser-like, not a bare bot token
    assert "AAFC" in ua    # still identifies the read-only auditor


def test_redirect_cap_is_ten(fake_transport):
    fake_transport({"https://example.com/": FakeResponse(200, "https://example.com/")})
    fetch("https://example.com/")
    assert FakeSession.instances[-1].max_redirects == engine.MAX_REDIRECTS == 10


# ---------------------------------------------------------------------------
# FIX 3: utility-page classification and rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://x.com/cart", "utility"),
    ("https://x.com/checkout/", "utility"),
    ("https://x.com/account/login", "utility"),
    ("https://x.com/sign-in", "utility"),
    ("https://x.com/wishlist", "utility"),
    ("https://x.com/", "content"),
    ("https://x.com/menu", "content"),
    ("https://x.com/cartoon-network", "content"),  # substring trap: segment match only
])
def test_page_kind_classification(url, expected):
    assert page_kind(url) == expected


UTILITY_HTML = """<html><head><title>Cart</title>
<meta name="robots" content="noindex">
</head><body><h1>Your cart</h1><p>""" + "x" * 1200 + """</p></body></html>"""


def test_utility_noindex_is_not_a_finding():
    found, stats = parse_and_check("https://x.com/cart", UTILITY_HTML)
    assert stats["page_kind"] == "utility"
    assert "indexability" not in checks_of(found)


def test_utility_missing_metadata_not_flagged():
    html = ('<html><head><title>Cart</title></head><body><h1>Cart</h1><p>'
            + "x" * 1200 + '</p></body></html>')
    found, _ = parse_and_check("https://x.com/cart", html)
    assert "meta_description" not in checks_of(found)
    assert "canonical" not in checks_of(found)


def test_content_noindex_is_still_critical():
    html = ('<html><head><title>Home</title>'
            '<meta name="robots" content="noindex"></head>'
            '<body><h1>Home</h1><p>' + "x" * 1200 + '</p></body></html>')
    found, _ = parse_and_check("https://x.com/", html)
    idx = [f for f in found if f["check"] == "indexability"]
    assert len(idx) == 1
    assert idx[0]["severity"] == "critical"


def test_utility_findings_excluded_from_score(monkeypatch):
    home_html = ('<html><head><title>Home</title></head><body><h1>Home</h1><p>'
                 + "x" * 1200 + '</p></body></html>')
    cart_html = ('<html><head><title>Cart</title>'
                 '<meta name="robots" content="noindex"></head>'
                 '<body><h1>Cart</h1></body></html>')

    def fake_fetch(url, method="GET", _retried=False, **kwargs):
        html = cart_html if "cart" in url else home_html
        return make_res(url, html)

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    monkeypatch.setattr(engine, "check_broken_links",
                        lambda url, parser, cap=25, **kwargs: ([], {}))
    site = audit_site("https://x.com/", max_pages=2)
    assert site["status"] == "COMPLETE"
    # the cart page's noindex produced no scored finding at all
    assert "indexability" not in {f["check"] for f in site["findings"]}


# ---------------------------------------------------------------------------
# FIX 4: BLOCKED with no score and no grade
# ---------------------------------------------------------------------------
def test_unreachable_site_is_blocked_without_score_or_grade(monkeypatch):
    def fake_fetch(url, method="GET", _retried=False, **kwargs):
        return {"ok": False, "status": None, "final_url": url, "headers": {},
                "text": "", "bytes": 0, "elapsed": 0.5,
                "redirects": 0, "redirect_chain": [],
                "error": "ConnectionError: DNS failed"}

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    site = audit_site("https://this-site-does-not-exist-12345.test/", max_pages=3)
    assert site["status"] == "BLOCKED"
    assert site["score"] is None
    assert site["grade"] is None
    assert site["pages_crawled"] == 0


# ---------------------------------------------------------------------------
# FIX 6: JS-shell pages get manual review, not failures
# ---------------------------------------------------------------------------
def test_js_shell_title_flagged_for_manual_review():
    html = ('<html><head></head><body><div id="root"></div>'
            '<script src="/app.js"></script></body></html>')
    found, stats = parse_and_check("https://x.com/", html)
    assert stats["likely_js_rendered"] is True
    title_hits = [f for f in found if f["check"] == "title"]
    assert title_hits and title_hits[0]["review_hint"] is True


# ---------------------------------------------------------------------------
# FIX 7: img-alt must not trust raw img counts (franklinbarbecue.com case)
# ---------------------------------------------------------------------------
FRANKLIN_LIKE_HTML = """<html><head><title>Franklin Barbecue</title>
<meta name="description" content="Serving the best barbecue in the known universe.">
<link rel="canonical" href="https://franklinbbq.com">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<h1>SERVING THE BEST BARBECUE IN THE KNOWN UNIVERSE.</h1>
<p>""" + "Real visible content. " * 200 + """</p>
<img src="/img/brisket.jpg">
<img src="/img/brisket.jpg">  <!-- duplicate of the above -->
<div hidden><img src="/img/hidden1.jpg" alt=""><img src="/img/hidden2.jpg" alt=""></div>
<div style="display:none"><img src="/img/hidden3.jpg" alt=""></div>
<noscript><img src="/img/noscript.jpg" alt=""></noscript>
<template><img src="/img/tpl.jpg" alt=""></template>
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="">
<img src="/img/pixel.gif" width="1" height="1" alt="">
<img src="/img/ribs.jpg">
<img src="/img/sausage.jpg">
</body></html>"""


def test_img_alt_excludes_non_rendered_and_duplicate_images():
    parser = PageParser()
    parser.feed(FRANKLIN_LIKE_HTML)
    # raw parser still sees every tag (honest), but the check filters them
    assert len(parser.images) == 11
    found, _ = parse_and_check("https://franklinbbq.com/", FRANKLIN_LIKE_HTML)
    alt = [f for f in found if f["check"] == "img_alt"]
    assert len(alt) == 1
    # only 3 rendered candidates remain: brisket (deduped), ribs, sausage
    assert "3 images" in alt[0]["title"]


def test_img_alt_high_empty_ratio_on_rich_page_needs_rendered_review():
    found, _ = parse_and_check("https://franklinbbq.com/", FRANKLIN_LIKE_HTML)
    alt = [f for f in found if f["check"] == "img_alt"][0]
    assert alt["severity"] == "warning"  # never critical for a count assertion
    assert alt["review_hint"] == "rendered"
    assert "needs rendered-DOM check" in alt["title"]
    assert "46 of 48 missing" not in alt["title"]


def test_img_alt_converted_to_needs_rendered_review_confidence():
    found, _ = parse_and_check("https://franklinbbq.com/", FRANKLIN_LIKE_HTML)
    alt = [f for f in found if f["check"] == "img_alt"][0]
    converted = findings_mod.from_engine_finding(
        alt, audit_id="aud_1", website_id="web_1", client_id="cli_1")
    assert converted["confidence"] == "NEEDS_RENDERED_REVIEW"
    assert converted["page_kind"] == "content"
    assert converted["verification"] == "HTTP_FETCH"


def test_img_alt_sparse_page_keeps_plain_warning():
    # 300-999 visible chars: not a JS shell, not "rich" -> plain ratio warning
    html = ('<html><head><title>T</title></head><body><h1>T</h1>'
            '<img src="/a.jpg"><img src="/b.jpg" alt="ok">'
            '<p>' + "plain content. " * 40 + '</p></body></html>')
    found, _ = parse_and_check("https://x.com/", html)
    alt = [f for f in found if f["check"] == "img_alt"][0]
    assert not alt["review_hint"]
    assert "1 of 2 images missing alt text" in alt["title"]


def test_img_alt_empty_alt_is_decorative_not_scored():
    """Explicitly empty alt="" marks a decorative image (WCAG) — it must
    never lower the score. Reported as unscored info only."""
    html = ('<html><head><title>T</title></head><body><h1>T</h1>'
            '<img src="/a.jpg" alt=""><img src="/b.jpg" alt="">'
            '<p>' + "plain content. " * 40 + '</p></body></html>')
    found, _ = parse_and_check("https://x.com/", html)
    alt = [f for f in found if f["check"] == "img_alt"]
    assert len(alt) == 1
    assert alt[0]["severity"] == "info"
    assert "not scored" in alt[0]["title"]
    assert "decorative" in alt[0]["title"]
    # info findings deduct nothing (score = 100 - 12*critical - 5*warning),
    # so a page of correctly-marked decorative images keeps a clean score


def test_img_alt_missing_vs_empty_distinction():
    """One genuinely missing alt scores; the decorative one does not."""
    html = ('<html><head><title>T</title></head><body><h1>T</h1>'
            '<img src="/missing.jpg"><img src="/deco.jpg" alt="">'
            '<p>' + "plain content. " * 40 + '</p></body></html>')
    found, _ = parse_and_check("https://x.com/", html)
    alt = {f["severity"]: f for f in found if f["check"] == "img_alt"}
    assert "1 of 2 images missing alt text" in alt["warning"]["title"]
    assert "not scored" in alt["info"]["title"]


def test_review_hint_true_maps_to_needs_manual_review():
    raw = {"page": "https://x.com/", "check": "title", "severity": "warning",
           "title": "t", "evidence": "e", "why_it_matters": "w",
           "recommended_fix": "f", "technical": "", "review_hint": True,
           "page_kind": "content", "verification": "HTTP_FETCH"}
    converted = findings_mod.from_engine_finding(
        raw, audit_id="aud_1", website_id="web_1", client_id="cli_1")
    assert converted["confidence"] == "NEEDS MANUAL REVIEW"


def test_needs_rendered_review_in_confidence_vocabulary():
    assert "NEEDS_RENDERED_REVIEW" in findings_mod.CONFIDENCES
