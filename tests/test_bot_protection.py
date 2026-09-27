"""Regression tests: bot-blocked pages (401/403/429) are a measurement
limit, not a site defect.

Found by the 50-site live test (2026-09-27): lowes.com's WAF answered 403
to the crawler on subpages. The engine then
  (a) emitted sixteen "Page returns HTTP 403" criticals -> score floored at 0/F,
  (b) let refused pages bypass max_pages, burning the crawl budget on the wall,
  (c) called rate-limited (429) links "broken" (news.ycombinator.com).
These tests pin the honest behavior: one collapsed bot_protection finding,
refused attempts count toward max_pages, PARTIAL with withheld grade when
pages go unmeasured, and 403/429 link checks reported as refused, not broken.
"""
from aafc_engine.auditor import engine


def _page(url, status=200):
    links = "".join(f'<a href="/page{i}">p{i}</a>' for i in range(10))
    return {
        "ok": True, "status": status, "final_url": url,
        "headers": {"Content-Type": "text/html"},
        "text": ("<html><head><title>A decent title for testing purposes</title>"
                 '<meta name="description" content="A proper description here.">'
                 f"</head><body><h1>Hi</h1>{links}</body></html>"),
        "bytes": 100, "elapsed": 0.1, "redirects": 0,
        "redirect_chain": [], "error": None,
    }


def _fake_fetch_factory(first_ok=True, block_status=403):
    calls = []

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        calls.append(url)
        if first_ok and url.rstrip("/") == "https://example.com":
            return _page(url, 200)
        return _page(url, block_status)

    fake_fetch.calls = calls
    return fake_fetch


def test_refused_pages_collapse_to_one_finding(monkeypatch):
    """Sixteen refused subpages -> one bot_protection warning, not sixteen
    criticals; the score reflects the measured homepage, not a floored 0."""
    monkeypatch.setattr(engine, "fetch", _fake_fetch_factory())
    res = engine.audit_site("https://example.com", max_pages=3,
                            render=False, time_budget=60)
    bp = [f for f in res["findings"] if f["check"] == "bot_protection"]
    page_bp = [f for f in bp if f["severity"] == "warning"]
    assert len(page_bp) == 1, f"expected one collapsed page finding, got {len(page_bp)}"
    assert page_bp[0]["severity"] == "warning"
    assert "403" in page_bp[0]["evidence"]
    assert not [f for f in res["findings"] if f["check"] == "http_status"], \
        "no per-page 403 criticals allowed"
    assert res["status"] == "PARTIAL", res["status"]
    assert res["grade"] is None, "grade withheld when pages go unmeasured"
    assert res["score"] is not None and res["score"] > 50, res["score"]
    assert res["counts"]["bot_blocked_findings_excluded_from_score"] == 1


def test_refused_pages_count_toward_max_pages(monkeypatch):
    """A WAF wall must not let the crawler burn fetches past max_pages."""
    fake = _fake_fetch_factory()
    monkeypatch.setattr(engine, "fetch", fake)
    # Isolate the crawl loop: link/robots checks have their own budgets.
    monkeypatch.setattr(engine, "check_broken_links",
                        lambda *a, **k: ([], {"links_checked": 0, "broken": 0}))
    monkeypatch.setattr(engine, "check_robots_and_sitemap",
                        lambda *a, **k: ([], {}))
    engine.audit_site("https://example.com", max_pages=3,
                      render=False, time_budget=60)
    # 1 homepage + 2 refused = 3 attempts; never the 16-fetch burn seen live.
    assert len(fake.calls) == 3, f"crawl not bounded: {len(fake.calls)} fetches"


def test_all_pages_refused_is_blocked(monkeypatch):
    """Homepage itself walled -> BLOCKED, no score, no grade (mcdonalds.com)."""
    monkeypatch.setattr(engine, "fetch", _fake_fetch_factory(first_ok=False))
    res = engine.audit_site("https://example.com", max_pages=3,
                            render=False, time_budget=60)
    assert res["status"] == "BLOCKED"
    assert res["score"] is None and res["grade"] is None
    bp = [f for f in res["findings"] if f["check"] == "bot_protection"]
    assert len(bp) == 1


def test_broken_link_check_does_not_call_429_broken(monkeypatch):
    """Rate-limited link targets are reported as refused, not broken."""
    parser = engine.PageParser()
    parser.feed('<html><body><a href="/a">a</a><a href="/b">b</a>'
                '<a href="/c">c</a></body></html>')

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        return {"ok": True, "status": 429, "final_url": url, "headers": {},
                "text": "", "bytes": 0, "elapsed": 0.1, "redirects": 0,
                "redirect_chain": [], "error": None}

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    findings, stats = engine.check_broken_links("https://example.com/", parser)
    assert not [f for f in findings if f["check"] == "broken_links"], \
        "429s must not be reported as broken links"
    bp = [f for f in findings if f["check"] == "bot_protection"]
    assert len(bp) == 1
    assert "not counted as broken" in bp[0]["title"]
    assert stats["broken"] == 0


def test_genuine_404_links_still_reported_broken(monkeypatch):
    """The fix must not hide genuinely broken links."""
    parser = engine.PageParser()
    parser.feed('<html><body><a href="/gone1">a</a><a href="/gone2">b</a>'
                '<a href="/gone3">c</a></body></html>')

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        return {"ok": True, "status": 404, "final_url": url, "headers": {},
                "text": "", "bytes": 0, "elapsed": 0.1, "redirects": 0,
                "redirect_chain": [], "error": None}

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    findings, stats = engine.check_broken_links("https://example.com/", parser)
    bl = [f for f in findings if f["check"] == "broken_links"]
    assert len(bl) == 1 and bl[0]["severity"] == "critical"
    assert stats["broken"] == 3
