"""Regression tests: the audit time budget is a hard wall-clock bound.

Found by the 50-site live test (2026-09-26): a stalled target (pizzahut.com)
kept the serverless function busy past Vercel's 60s kill limit because
fetch() used a fixed 12s timeout + retry regardless of the remaining budget,
and the budget was only checked *between* pages. The user got a bare 504
instead of an honest PARTIAL result. These tests pin the fix: every network
call is bounded by the remaining budget, retries stop when the budget is
nearly spent, and the audit returns inside the budget.
"""
import time

from aafc_engine.auditor import engine


def _slow_page(url):
    # Links keep the crawler fed so the loop only stops when the budget hits.
    links = "".join(f'<a href="/page{i}">p{i}</a>' for i in range(10))
    return {
        "ok": True, "status": 200, "final_url": url,
        "headers": {"Content-Type": "text/html"},
        "text": ("<html><head><title>T</title></head>"
                 f"<body><h1>Hi</h1>{links}</body></html>"),
        "bytes": 100, "elapsed": 0.1, "redirects": 0,
        "redirect_chain": [], "error": None,
    }


def test_budget_is_hard_wall_with_stalled_fetches(monkeypatch):
    """Fake fetch honors the timeout kwarg like a real stalled socket would:
    each call blocks for the full timeout given. The audit must still finish
    inside the budget and report PARTIAL, not hang."""
    calls = []

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        calls.append({"timeout": timeout, "allow_retry": allow_retry})
        time.sleep(min(timeout if timeout else 12, 30))
        return _slow_page(url)

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    t0 = time.time()
    res = engine.audit_site("https://example.com", max_pages=10,
                            render=False, time_budget=6)
    dt = time.time() - t0
    assert res["status"] == "PARTIAL", res["status"]
    assert res["grade"] is None, "PARTIAL must withhold the grade"
    assert dt < 6 + 15, f"budget not enforced: took {dt:.1f}s"
    assert calls, "expected at least one fetch attempt"
    # No fetch was ever given an unbounded/huge timeout...
    assert all(c["timeout"] is not None and c["timeout"] <= 12 for c in calls)
    # ...and once the budget was nearly spent, retries were disabled.
    assert any(c["allow_retry"] is False for c in calls)


def test_no_budget_means_old_behavior(monkeypatch):
    """Without a time budget the engine keeps the classic fixed timeout and
    still retries transient failures (CLI behavior unchanged)."""
    calls = []

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        calls.append({"timeout": timeout, "allow_retry": allow_retry})
        return _slow_page(url)

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    res = engine.audit_site("https://example.com", max_pages=1,
                            render=False, time_budget=None)
    assert res["status"] == "COMPLETE"
    assert all(c["timeout"] == engine.TIMEOUT for c in calls)
    assert all(c["allow_retry"] is True for c in calls)


def test_robots_sitemap_skipped_when_budget_spent(monkeypatch):
    """The trailing robots.txt/sitemap.xml fetches must not run after the
    budget is exhausted — they are info-level and must not push a PARTIAL
    audit over the edge."""
    robots_calls = []

    def fake_fetch(url, method="GET", _retried=False, timeout=None,
                   allow_retry=True):
        if "robots.txt" in url or "sitemap.xml" in url:
            robots_calls.append(url)
        # Every fetch honors the timeout like a real stalled socket would.
        time.sleep(min(timeout if timeout else 12, 30))
        return _slow_page(url)

    monkeypatch.setattr(engine, "fetch", fake_fetch)
    t0 = time.time()
    res = engine.audit_site("https://example.com", max_pages=10,
                            render=False, time_budget=4)
    dt = time.time() - t0
    assert res["status"] == "PARTIAL"
    assert not robots_calls, f"robots/sitemap fetched after budget spent: {robots_calls}"
    assert dt < 4 + 15, f"took {dt:.1f}s"
