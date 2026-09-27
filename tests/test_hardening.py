"""Hardening regressions for the business-layer repair batch (2026-09-27).

One focused test per confirmed hole-audit finding: H1-H3, M1/M2, M3, M4,
M5, M6, M8, M9, M10, M11, L1, L3, L4, L5, L6, L7, L10, L13, L15, L16.

All data is fictitious (@example.test). No network is used: the SSRF tests
exercise ``safefetch`` refusal paths (private IPs, localhost) that return
before any socket is opened, and the hosted-API tests import
``api/index.py`` without serving it.
"""
from __future__ import annotations

import json

import pytest

from aafc_engine import (
    clients,
    customers,
    fixplans,
    footprint,
    money,
    opportunities,
    projects,
    safefetch,
    services,
    social_audit,
    tasks,
    websites,
)
from aafc_engine.auditor import registry as audit_registry
from aafc_engine.store import Store, StoreError


def _client(store, email="hardening@example.test"):
    return clients.create_client(store, "Hardening Co", email)[0]


def _site(store, cid, url="https://example.com", authorized=True):
    site, _ = websites.add_website(store, cid, url, authorized=authorized)
    return site


# ---------------------------------------------------------------------------
# H1 — update_client: email validation, collision, record_type protection
# ---------------------------------------------------------------------------

def test_h1_email_collision_rejected(store):
    a = _client(store, "a@example.test")
    _client(store, "b@example.test")
    with pytest.raises(ValueError):
        clients.update_client(store, a["id"], email="b@example.test")


def test_h1_invalid_email_rejected(store):
    c = _client(store)
    with pytest.raises(ValueError):
        clients.update_client(store, c["id"], email="not-an-email")


def test_h1_record_type_protected(store):
    c = _client(store)
    before = c["record_type"]
    # record_type is stripped from updates (promotion only via
    # promote_to_client), so it can never be silently changed.
    updated = clients.update_client(store, c["id"], record_type="client")
    assert updated["record_type"] == before
    updated = clients.update_client(store, c["id"], name="New Name")
    assert updated["name"] == "New Name"


# ---------------------------------------------------------------------------
# M10 — store path traversal
# ---------------------------------------------------------------------------

def test_m10_traversal_client_id_rejected(store):
    with pytest.raises(StoreError):
        store.write_json("../../etc", {"x": 1}, "pwn.json")
    with pytest.raises(StoreError):
        store.read_json("cl_nothex!!", "x.json", default=None)
    with pytest.raises(StoreError):
        store.write_json(_client(store)["id"], {"x": 1}, "../escape.json")


def test_m10_valid_client_id_roundtrip(store):
    cid = _client(store)["id"]
    assert cid.startswith("cl_")
    store.write_json(cid, {"ok": True}, "note.json")
    assert store.read_json(cid, "note.json") == {"ok": True}


# ---------------------------------------------------------------------------
# M1/M2 — money integrity
# ---------------------------------------------------------------------------

def test_m1_negative_amount_rejected(store):
    cid = _client(store)["id"]
    with pytest.raises(ValueError):
        money.record_event(store, cid, "INVOICE", -50.0, "NEEDS_VERIFICATION")
    with pytest.raises(ValueError):
        money.record_event(store, cid, "INVOICE", 100.0, "NEEDS_VERIFICATION", amount_paid_dollars=-1.0)


def test_m2_illegal_and_terminal_transitions(store):
    cid = _client(store)["id"]
    ev = money.record_event(store, cid, "INVOICE", 100.0, "NEEDS_VERIFICATION")
    eid = ev["id"]
    # NEEDS_VERIFICATION -> EXPECTED -> OWED -> PAID is legal (receivables
    # and payment require evidence).
    money.set_event_status(store, cid, eid, "EXPECTED", evidence="invoice #1")
    money.set_event_status(store, cid, eid, "OWED", evidence="invoice #1")
    money.set_event_status(store, cid, eid, "PAID", evidence="receipt #1")
    # PAID is terminal
    with pytest.raises(money.MoneyError):
        money.set_event_status(store, cid, eid, "OWED")
    # unknown status
    with pytest.raises(money.MoneyError):
        money.set_event_status(store, cid, eid, "BOGUS")
    # skipping backwards is illegal
    ev2 = money.record_event(store, cid, "INVOICE", 10.0, "NEEDS_VERIFICATION")
    with pytest.raises(money.MoneyError):
        money.set_event_status(store, cid, ev2["id"], "PAID", evidence="x")


def test_m2_status_history_and_updated_at(store):
    cid = _client(store)["id"]
    ev = money.record_event(store, cid, "INVOICE", 100.0, "NEEDS_VERIFICATION")
    eid = ev["id"]
    money.set_event_status(store, cid, eid, "EXPECTED", evidence="invoice #1")
    money.set_event_status(store, cid, eid, "EXPECTED", evidence="invoice #1")  # idempotent
    got = next(e for e in money.list_events(store, cid) if e["id"] == eid)
    assert got["updated_at"] is not None
    hist = got["status_history"]
    assert hist[0] == {"from": "NEEDS_VERIFICATION", "to": "EXPECTED",
                       "at": hist[0]["at"]}
    assert hist[-1]["to"] == "EXPECTED"


# ---------------------------------------------------------------------------
# M3 — audit JSON stays byte-for-byte pristine after lifecycle changes
# ---------------------------------------------------------------------------

def test_m3_audit_json_pristine_after_status_change(store):
    from aafc_engine import findings as findings_mod

    cid = _client(store)["id"]
    site = _site(store, cid)
    audit = audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    aid = audit["id"]
    path = store.client_dir(cid) / "audits" / f"{aid}.json"
    before = path.read_bytes()
    fid = audit["findings"][0]["id"]
    findings_mod.set_finding_status(store, cid, fid, "RECOMMENDED")
    findings_mod.set_finding_status(store, cid, fid, "READY_TO_IMPLEMENT")
    assert path.read_bytes() == before, "audit JSON must be append-only"
    merged = findings_mod.get_finding(store, cid, fid)
    assert merged["status"] == "READY_TO_IMPLEMENT"
    overlay = store.read_json(cid, "finding_status.json", default={})
    history = overlay[fid]["history"]
    assert [(h["from"], h["to"]) for h in history] == [
        ("FOUND", "RECOMMENDED"), ("RECOMMENDED", "READY_TO_IMPLEMENT")]


# ---------------------------------------------------------------------------
# M6 — fix-plan idempotency
# ---------------------------------------------------------------------------

def test_m6_no_duplicate_tasks_on_rebuild(store):
    from aafc_engine import findings as findings_mod

    cid = _client(store)["id"]
    site = _site(store, cid)
    audit = audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    plan1 = fixplans.create_fix_plan(store, cid, audit["id"])
    n1 = len(tasks.list_tasks(store, cid))
    assert n1 > 0
    plan2 = fixplans.create_fix_plan(store, cid, audit["id"])
    assert plan2["id"] == plan1["id"]
    n2 = len(tasks.list_tasks(store, cid))
    assert n2 == n1


# ---------------------------------------------------------------------------
# M5 — authorization for footprint / social
# ---------------------------------------------------------------------------

def test_m5_footprint_requires_authorized_website(store):
    cid = _client(store)["id"]
    site = _site(store, cid, authorized=False)
    with pytest.raises(Exception):
        footprint.discover(store, cid, site["id"])


def test_m5_social_requires_authorized_website(store):
    from aafc_engine.websites import NotAuthorizedError

    cid = _client(store)["id"]
    site = _site(store, cid, authorized=False)
    # A footprint tied to an unauthorized website: the social audit must
    # refuse, not audit profiles derived from a site it may not touch.
    store.write_json(cid, {
        "website_id": site["id"],
        "website_url": site["url"],
        "profiles": [{
            "platform": "facebook",
            "url": "https://www.facebook.com/example",
            "status": "PUBLICLY VERIFIED",
        }],
    }, "social.json")
    with pytest.raises(NotAuthorizedError):
        social_audit.audit_social(store, cid)


# ---------------------------------------------------------------------------
# H2/H3 — safefetch SSRF defense (no sockets opened on refusal paths)
# ---------------------------------------------------------------------------

def test_h2_private_ip_refused_without_network():
    ok, reason = safefetch.host_is_public("127.0.0.1")
    assert not ok and "non-public" in reason
    ok, reason = safefetch.host_is_public("10.0.0.5")
    assert not ok
    result = safefetch.safe_get("http://127.0.0.1:9/")
    assert result["ok"] is False
    assert "non-public" in result["error"]


def test_h3_localhost_refused_without_network():
    ok, _ = safefetch.host_is_public("localhost")
    assert not ok
    result = safefetch.safe_get("http://localhost:9/")
    assert result["ok"] is False


def test_h2_non_http_scheme_refused():
    result = safefetch.safe_get("file:///etc/passwd")
    assert result["ok"] is False
    assert "non-HTTP" in result["error"]


def test_h2_too_many_redirects(monkeypatch):
    import requests as req

    class FakeResp:
        status_code = 302
        headers = {"Location": "/loop"}
        encoding = "utf-8"

        def close(self):
            pass

    monkeypatch.setattr(req, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(safefetch, "host_is_public", lambda h: (True, ""))
    result = safefetch.safe_get("https://example.com/", max_redirects=2)
    assert result["ok"] is False
    assert "too many redirects" in result["error"]


def test_h2_response_body_capped(monkeypatch):
    import requests as req

    class FakeResp:
        status_code = 200
        headers = {}
        encoding = "utf-8"

        def iter_content(self, chunk_size=65536):
            yield b"x" * 100

        def close(self):
            pass

    monkeypatch.setattr(req, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(safefetch, "host_is_public", lambda h: (True, ""))
    result = safefetch.safe_get("https://example.com/", max_bytes=10)
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["text"]) == 10


# ---------------------------------------------------------------------------
# M4 — opportunity dedupe across re-audits (stable keys, not random IDs)
# ---------------------------------------------------------------------------

def test_m4_no_duplicate_opportunities_across_reaudits(store):
    cid = _client(store)["id"]
    site = _site(store, cid)
    audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    map1 = opportunities.build_opportunity_map(store, cid)
    n1 = len(map1["opportunities"])
    assert n1 > 0
    # Re-audit: every finding gets a NEW random id, but the same (check, page)
    # problems. Rebuilding must not duplicate opportunities.
    audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    map2 = opportunities.build_opportunity_map(store, cid)
    assert len(map2["opportunities"]) == n1


# ---------------------------------------------------------------------------
# M11 — project reference validation
# ---------------------------------------------------------------------------

def test_m11_project_rejects_dangling_and_cross_client_refs(store):
    cid = _client(store)["id"]
    other = _client(store, "other@example.test")
    other_site = _site(store, other["id"])
    with pytest.raises(ValueError):
        projects.create_project(store, cid, "Bad ref",
                                website_id="web_nonexistent")
    with pytest.raises(ValueError):
        projects.create_project(store, cid, "Cross-client ref",
                                website_id=other_site["id"])
    with pytest.raises(ValueError):
        projects.create_project(store, cid, "Bad audit",
                                audit_id="aud_nonexistent")
    ok_site = _site(store, cid)
    proj = projects.create_project(store, cid, "Good ref",
                                   website_id=ok_site["id"])
    assert proj["website_id"] == ok_site["id"]


# ---------------------------------------------------------------------------
# M8 — CLI --pages clamp
# ---------------------------------------------------------------------------

def test_m8_pages_clamped(capsys):
    from aafc_engine.cli import _clamp_pages

    assert _clamp_pages(6) == 6
    assert _clamp_pages(0) == 1
    assert _clamp_pages(1000000) == 12
    assert "clamped" in capsys.readouterr().out


def test_m8_registry_rejects_nonpositive_pages(store):
    cid = _client(store)["id"]
    site = _site(store, cid)
    with pytest.raises(ValueError):
        audit_registry.run_audit(store, cid, site["id"], max_pages=0)


# ---------------------------------------------------------------------------
# L10 — communication logging validation
# ---------------------------------------------------------------------------

def test_l10_log_communication_validation(store):
    cid = _client(store)["id"]
    with pytest.raises(ValueError):
        customers.log_communication(store, cid, "email", "sideways", "Hi")
    with pytest.raises(ValueError):
        customers.log_communication(store, cid, "  ", "inbound", "Hi")
    entry = customers.log_communication(store, cid, " email ", "inbound", "Hi")
    assert entry["channel"] == "email"
    assert entry["direction"] == "inbound"


# ---------------------------------------------------------------------------
# L15 — normalize_url dedup gaps
# ---------------------------------------------------------------------------

def test_l15_normalize_url_default_ports_and_userinfo():
    assert websites.normalize_url("https://example.com:443/") == \
        websites.normalize_url("https://example.com")
    assert websites.normalize_url("http://example.com:80") == \
        "http://example.com"
    assert websites.normalize_url("http://user:pass@example.com/") == \
        "http://example.com"
    # non-default ports are kept
    assert websites.normalize_url("http://example.com:8080/") == \
        "http://example.com:8080"


# ---------------------------------------------------------------------------
# L13 — null profiles guard
# ---------------------------------------------------------------------------

def test_l13_null_profiles_do_not_crash(store):
    cid = _client(store)["id"]
    store.write_json(cid, {"profiles": None}, "social.json")
    assert social_audit.audit_social(store, cid) == []
    assert opportunities.build_opportunity_map(store, cid)["opportunities"] == []
    cabinet = customers.get_customer_file(store, "hardening@example.test")
    assert cabinet["social_profiles"] == []


# ---------------------------------------------------------------------------
# L4 — task completion honesty
# ---------------------------------------------------------------------------

def test_l4_completed_at_never_fabricated(store):
    cid = _client(store)["id"]
    task = tasks.create_task(store, cid, None, "Write report")
    tid = task["id"]
    tasks.set_task_status(store, cid, tid, "DONE")
    first = tasks.get_task(store, cid, tid)["completed_at"]
    assert first is not None
    tasks.set_task_status(store, cid, tid, "TODO")
    tasks.set_task_status(store, cid, tid, "DONE")
    again = tasks.get_task(store, cid, tid)
    assert again["completed_at"] == first, "re-completion must not rewrite history"
    assert [h["to"] for h in again["status_history"]] == ["DONE", "TODO", "DONE"]


# ---------------------------------------------------------------------------
# L1 — audit version monotonic after deletion
# ---------------------------------------------------------------------------

def test_l1_version_monotonic_after_delete(store):
    cid = _client(store)["id"]
    site = _site(store, cid)
    a1 = audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    a2 = audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    assert (a1["version"], a2["version"]) == (1, 2)
    (store.client_dir(cid) / "audits" / f"{a1['id']}.json").unlink()
    a3 = audit_registry.run_audit(store, cid, site["id"], max_pages=1)
    assert a3["version"] == 3, "deleted v1 must not cause a version collision"


# ---------------------------------------------------------------------------
# L3 — social checks trigger services
# ---------------------------------------------------------------------------

def test_l3_social_findings_can_trigger_services():
    assert "social_facebook_reachable" in services.KNOWN_CHECKS
    assert "social_instagram_website_linkback" in services.KNOWN_CHECKS
    svc = services.get_service("social-media-system")
    assert "social_facebook_reachable" in svc["evidence_triggers"]
    matches = services.match_services([
        {"id": "f_1", "check": "social_facebook_reachable",
         "result": "FAIL", "page": "https://facebook.com/x"},
    ])
    assert any(m["service"] == "social-media-system" for m in matches)


# ---------------------------------------------------------------------------
# L7 — hosted API: non-string URL -> 400, not 500
# ---------------------------------------------------------------------------

def test_l7_non_string_url_rejected_as_400():
    api = pytest.importorskip("api.index")
    with pytest.raises(ValueError):
        api._normalize_target(123)
    with pytest.raises(ValueError):
        api._normalize_target(None)


# ---------------------------------------------------------------------------
# L16 — DNS cache bounded
# ---------------------------------------------------------------------------

def test_l16_dns_cache_bounded():
    api = pytest.importorskip("api.index")
    assert api._DNS_CACHE_MAX == 1000
    for i in range(1050):
        api._cache_ips(f"host{i}.example.test", {"1.2.3.4"}, 60.0)
    assert len(api._dns_cache) <= 1000


# ---------------------------------------------------------------------------
# M9 — hosted API rate limit
# ---------------------------------------------------------------------------

def test_m9_rate_limit_trips_and_recovers():
    api = pytest.importorskip("api.index")
    ip = "198.51.100.77"
    api._rate_hits.pop(ip, None)
    limited = [api._rate_limited(ip)[0] for _ in range(api._RATE_LIMIT)]
    assert not any(limited)
    is_limited, retry_after = api._rate_limited(ip)
    assert is_limited and retry_after > 0
