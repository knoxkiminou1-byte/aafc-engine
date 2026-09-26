"""Extension layer tests (T-A through T-G).

Fictitious data only ("Desert Bloom Barbershop", @example.test emails).
T-D runs the REAL audit through the registry against https://example.com
(max_pages=2) exactly as the task requires; all other audit tests are
hand-made or stubbed (no network).
"""
from __future__ import annotations

import pytest

from aafc_engine import (
    clients,
    crosscheck,
    customers,
    footprint,
    opportunities,
    social_audit,
    sources,
    websites,
)
from aafc_engine.auditor import registry as audit_registry

from conftest import hand_audit


def test_ta_existing_email_resolves_fully(store):
    """T-A: an existing email resolves with source/website/offerings/history."""
    email = "dana@desertbloom.test"
    customers.register_customer(
        store,
        "Dana Bloom",
        email,
        business_name="Desert Bloom Barbershop",
        source="referral",
        source_detail="walk-in",
    )
    client = clients.find_client_by_email(store, email)
    cid = client["id"]
    websites.add_website(store, cid, "https://example.com", authorized=True)
    customers.add_offering(store, cid, "Website Design & Build", status="current")
    customers.log_communication(
        store, cid, "email", "inbound", "Intro call", body_ref="msg-001"
    )

    res = customers.resolve_customer(store, "DANA@desertbloom.test")
    assert res["resolution"] == "FOUND"
    assert res["email"] == email
    cabinet = res["customer"]
    assert cabinet["source"]["source"] == "referral"
    assert cabinet["source"]["detail"] == "walk-in"
    assert cabinet["business"]["business_name"] == "Desert Bloom Barbershop"
    assert len(cabinet["websites"]) == 1
    assert cabinet["offerings"][0]["status"] == "current"
    events = {h["event"] for h in cabinet["history"]}
    assert {"customer_registered", "source_set"} <= events

    intake = customers.self_service_intake(store, "Dana Bloom", email)
    assert intake["resolution"] == "FOUND"
    assert intake["needs"] == []
    assert intake["primary_website"] is not None
    assert intake["primary_website"]["url"] == "https://example.com"


def test_tb_unknown_email_is_new_without_fabrication(store):
    """T-B: an unknown email resolves NEW; nothing is fabricated."""
    res = customers.resolve_customer(store, "  Nobody@Example.Test ")
    assert res["resolution"] == "NEW"
    assert res["email"] == "nobody@example.test"
    assert res["customer"] is None  # unknown fields stay None, never invented


def test_tc_self_service_existing_loads_primary_website(store):
    """T-C: self-service intake on an existing customer auto-loads the
    primary website."""
    email = "owner@blueriverplumbing.test"
    customers.register_customer(store, "Blue River", email)
    client = clients.find_client_by_email(store, email)
    websites.add_website(
        store, client["id"], "https://example.com", authorized=True
    )
    intake = customers.self_service_intake(store, "Blue River", email)
    assert intake["resolution"] == "FOUND"
    assert intake["needs"] == []
    assert intake["primary_website"]["url"] == "https://example.com"


def test_td_full_chain_desert_bloom(store):
    """T-D: FULL CHAIN on fictitious Desert Bloom Barbershop.

    resolve -> source -> website -> cabinet -> footprint.discover ->
    real registry.run_audit (max_pages=2, NO stub) -> social_audit ->
    crosscheck -> build_opportunity_map -> expand_offerings. Each step
    consumes the previous step's output; every opportunity's
    triggering_findings are real finding ids from this chain.
    """
    email = "contact@desertbloom.test"

    # resolve NEW, then register, then resolve FOUND
    assert customers.resolve_customer(store, email)["resolution"] == "NEW"
    customers.register_customer(
        store, "Desert Bloom", email,
        business_name="Desert Bloom Barbershop",
    )
    client = clients.find_client_by_email(store, email)
    cid = client["id"]
    assert customers.resolve_customer(store, email)["resolution"] == "FOUND"

    # source (consumed by the cabinet)
    sources.set_source(store, cid, "referral", detail="walk-in")
    assert sources.get_source(store, cid)["source"] == "referral"

    # website (consumed by footprint + audit); visible in the cabinet
    site, _ = websites.add_website(
        store, cid, "https://example.com",
        label="Desert Bloom site", authorized=True,
    )
    cabinet = customers.get_customer_file(store, email)
    assert any(w["id"] == site["id"] for w in cabinet["websites"])

    # footprint: consumes the website, feeds social_audit via social.json
    profiles = footprint.discover(store, cid, site["id"])
    assert len(profiles) == len(footprint.PLATFORMS) == 8
    social_doc = store.read_json(cid, "social.json")
    assert social_doc["website_id"] == site["id"]
    assert len(social_doc["profiles"]) == len(profiles)

    # REAL audit through the registry (deliberately not stubbed)
    audit = audit_registry.run_audit(store, cid, site["id"], max_pages=2)
    assert audit["status"] == "COMPLETE"
    assert audit["website_id"] == site["id"]
    assert audit["findings"], "example.com should yield real findings"

    # social audit: consumes social.json
    social_findings = social_audit.audit_social(store, cid)
    assert isinstance(social_findings, list)

    # crosscheck: consumes latest COMPLETE audit + social.json + websites
    xcheck = crosscheck.crosscheck(store, cid)
    assert all(f["check"].startswith("xcheck_") for f in xcheck)

    # opportunity map: consumes all three finding streams
    built = opportunities.build_opportunity_map(
        store, cid, current_offering="website-design-build"
    )
    assert built["customer_id"] == cid
    opps = built["opportunities"]
    assert len(opps) >= 1
    evidence_ids = (
        {f["id"] for f in audit["findings"]}
        | {f["id"] for f in social_findings}
        | {f["id"] for f in xcheck}
    )
    for opp in opps:
        assert opp["customer_id"] == cid
        assert opp["status"] == "DISCOVERED"
        assert opp["triggering_findings"], "opportunity without evidence"
        assert set(opp["triggering_findings"]) <= evidence_ids, (
            f"opportunity {opp['id']} cites non-evidence findings"
        )

    # expansions: only evidence-supported services, current excluded
    expansions = opportunities.expand_offerings(store, cid, "website-design-build")
    assert expansions["current_offering"] == "website-design-build"
    for exp in expansions["supported_expansions"]:
        assert exp["service"] != "website-design-build"
        assert exp["triggered_by_findings"]
        assert set(exp["triggered_by_findings"]) <= evidence_ids

    # every stage logged its history event
    events = {
        h["event"]
        for h in store.read_json(cid, "history.json", default=[])
    }
    assert {
        "customer_registered", "source_set", "footprint_discovered",
        "social_audited", "crosschecked", "opportunity_map_built",
    } <= events


def test_te_same_email_twice_one_customer_version_2(store, stub_engine):
    """T-E: registering the same email twice yields one customer; a
    re-audit produces version 2 with version 1 preserved."""
    email = "repeat@desertbloom.test"
    customers.register_customer(store, "Desert Bloom", email)
    customers.register_customer(store, "Desert Bloom", email)
    assert len(clients.list_clients(store)) == 1
    cid = clients.find_client_by_email(store, email)["id"]

    site, _ = websites.add_website(store, cid, "https://example.com", authorized=True)
    v1 = audit_registry.run_audit(store, cid, site["id"], max_pages=2)
    v2 = audit_registry.run_audit(store, cid, site["id"], max_pages=2)
    assert (v1["version"], v2["version"]) == (1, 2)
    path = store.client_dir(cid) / "audits" / f"{v1['id']}.json"
    assert path.is_file()
    assert audit_registry.get_audit(store, cid, v1["id"])["version"] == 1


def test_tf_cabinet_isolation(store):
    """T-F: cabinet isolation A vs B across social, opportunities,
    communications, and history."""
    customers.register_customer(
        store, "Acme Bakery", "a@acmebakery.test", business_name="Acme Bakery"
    )
    customers.register_customer(
        store, "Blue River", "b@blueriverplumbing.test",
        business_name="Blue River Plumbing",
    )
    aid = clients.find_client_by_email(store, "a@acmebakery.test")["id"]
    bid = clients.find_client_by_email(store, "b@blueriverplumbing.test")["id"]
    # distinct social profiles
    for cid, marker in ((aid, "acme_social_marker"), (bid, "blueriver_social_marker")):
        store.write_json(
            cid,
            {
                "website_id": None,
                "discovered_at": "t",
                "homepage_fetch": {},
                "contact_signals": {},
                "profiles": [
                    {
                        "platform": "instagram",
                        "url": f"https://instagram.com/{marker}",
                        "status": "PUBLICLY VERIFIED",
                        "confidence": "CONFIRMED",
                        "evidence": marker,
                    }
                ],
            },
            "social.json",
        )
    # distinct communications
    customers.log_communication(
        store, aid, "email", "outbound", "Acme proposal sent", body_ref="acme-com-1"
    )
    customers.log_communication(
        store, bid, "email", "outbound", "Blue River proposal sent",
        body_ref="blueriver-com-1",
    )
    # distinct evidence -> distinct opportunities
    a_site, _ = websites.add_website(store, aid, "https://a.example.test", authorized=True)
    b_site, _ = websites.add_website(store, bid, "https://b.example.test", authorized=True)
    hand_audit(store, aid, a_site["id"], [{
        "check": "meta_description", "severity": "HIGH",
        "issue": "A issue", "evidence": "acme-evidence-marker",
    }])
    hand_audit(store, bid, b_site["id"], [{
        "check": "hsts", "severity": "CRITICAL",
        "issue": "B issue", "evidence": "blueriver-evidence-marker",
    }])
    opportunities.build_opportunity_map(store, aid)
    opportunities.build_opportunity_map(store, bid)
    # distinct history
    customers.log_history(store, aid, "acme_custom_event", "acme history marker")
    customers.log_history(store, bid, "blueriver_custom_event", "blueriver history marker")

    cab_a = customers.get_customer_file(store, "a@acmebakery.test")
    cab_b = customers.get_customer_file(store, "b@blueriverplumbing.test")

    # social
    assert any("acme_social_marker" in p.get("url", "") for p in cab_a["social_profiles"])
    assert all("blueriver_social_marker" not in p.get("url", "") for p in cab_a["social_profiles"])
    assert all("acme_social_marker" not in p.get("url", "") for p in cab_b["social_profiles"])
    # opportunities
    a_opp_ids = {o["id"] for o in cab_a["opportunities"]}
    b_opp_ids = {o["id"] for o in cab_b["opportunities"]}
    assert a_opp_ids and b_opp_ids and a_opp_ids.isdisjoint(b_opp_ids)
    assert any(o["service"] == "seo" for o in cab_a["opportunities"])
    assert any(o["service"] == "maintenance-retainer" for o in cab_b["opportunities"])
    # communications
    assert {c["body_ref"] for c in cab_a["communications"]} == {"acme-com-1"}
    assert {c["body_ref"] for c in cab_b["communications"]} == {"blueriver-com-1"}
    # history
    a_details = " ".join(h.get("details", "") for h in cab_a["history"])
    assert "acme history marker" in a_details
    assert "blueriver history marker" not in a_details


def test_tg_honesty_no_padding(store):
    """T-G: a single LOW finding that triggers no service yields at most a
    couple of opportunities, and expansions are never padded."""
    email = "honest@acmebakery.test"
    customers.register_customer(store, "Acme Bakery", email)
    cid = clients.find_client_by_email(store, email)["id"]
    site, _ = websites.add_website(store, cid, "https://example.com", authorized=True)
    hand_audit(store, cid, site["id"], [{
        "check": "https",  # real check name, but in NO service's evidence_triggers
        "severity": "LOW",
        "issue": "HTTPS serving fine",
        "evidence": "TLS valid; page served over HTTPS.",
    }])

    built = opportunities.build_opportunity_map(store, cid, current_offering="website-audit")
    assert len(built["opportunities"]) <= 2

    expansions = opportunities.expand_offerings(store, cid, "website-audit")
    assert expansions["supported_expansions"] == []
    assert expansions["note"] == (
        "No catalog service is evidence-supported for expansion right now."
    )
