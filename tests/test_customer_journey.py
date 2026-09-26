"""The customer journey, intake to verification (B1-B14).

Fictitious data only ("Blue River Plumbing", @example.test). Audits are
hand-made COMPLETE JSON or run through the stubbed engine -- no network.
The real end-to-end engine runs in test_audit_live.py.
"""
from __future__ import annotations

import subprocess

import pytest

from aafc_engine import (
    clients,
    customers,
    fixplans,
    money,
    reaudit,
    reports,
    tasks,
    websites,
)
from aafc_engine.auditor import registry as audit_registry
from aafc_engine.delivery import (
    DeliveryError,
    GmailDelivery,
    SwiftSendDelivery,
    get_delivery_log,
    log_delivery,
)

from conftest import hand_audit, hand_audit_v2

EMAIL = "blue@blueriverplumbing.test"


def _intake_customer(store):
    """Intake the fictitious Blue River Plumbing customer + website."""
    res = customers.self_service_intake(store, "Blue River", EMAIL)
    assert res["resolution"] == "NEW"
    client = clients.find_client_by_email(store, EMAIL)
    site, _ = websites.add_website(
        store, client["id"], "https://blueriverplumbing.test", authorized=True
    )
    return client, site


def _hand_audit(store, client, site, n_findings=2):
    specs = [
        {
            "check": "meta_description",
            "severity": "HIGH",
            "issue": "Missing meta description",
            "evidence": "blueriver-evidence: no <meta name='description'> on the homepage",
        },
        {
            "check": "hsts",
            "severity": "CRITICAL",
            "issue": "HSTS header not set",
            "evidence": "blueriver-evidence: strict-transport-security header absent",
        },
    ]
    return hand_audit(store, client["id"], site["id"], specs[:n_findings])


def test_01_intake_new_customer(store):
    """B1: self-service intake registers a new customer and reports needs."""
    res = customers.self_service_intake(store, "Blue River", EMAIL)
    assert res["resolution"] == "NEW"
    assert res["needs"] == ["website", "authorization"]
    assert res["primary_website"] is None


def test_02_customer_record_created(store):
    """B2: the intake creates a customer record."""
    res = customers.self_service_intake(store, "Blue River", EMAIL)
    identity = res["customer"]["identity"]
    assert identity["email"] == EMAIL
    assert identity["name"] == "Blue River"
    assert clients.find_client_by_email(store, EMAIL) is not None


def test_03_website_associated_to_right_customer(store):
    """B3: the website is associated to the right customer."""
    client, site = _intake_customer(store)
    cabinet = customers.get_customer_file(store, EMAIL)
    assert len(cabinet["websites"]) == 1
    assert cabinet["websites"][0]["id"] == site["id"]
    assert cabinet["websites"][0]["client_id"] == client["id"]
    assert cabinet["websites"][0]["url"] == "https://blueriverplumbing.test"


def test_04_auditor_runs(store):
    """B4: the auditor runs against the authorized site.

    Uses a hand-made COMPLETE audit here (no network); the REAL engine
    runs end-to-end in test_audit_live.py (marked live).
    """
    client, site = _intake_customer(store)
    audit = _hand_audit(store, client, site)
    assert audit["status"] == "COMPLETE"
    assert audit["website_id"] == site["id"]
    assert audit["findings"], "expected findings"


def test_05_findings(store):
    """B5: findings are produced with concrete evidence."""
    client, site = _intake_customer(store)
    audit = _hand_audit(store, client, site)
    assert len(audit["findings"]) == 2
    for finding in audit["findings"]:
        assert finding["evidence"].strip()


def test_06_report(store):
    """B6: a report is generated from the audit."""
    client, site = _intake_customer(store)
    audit = _hand_audit(store, client, site)
    report, md_path = reports.generate_audit_report(
        store, client["id"], audit["id"]
    )
    assert report["id"].startswith("rep_")
    assert report["audit_id"] == audit["id"]


def test_07_report_client_id_matches_customer(store):
    """B7: the report is associated to the right customer."""
    client, site = _intake_customer(store)
    audit = _hand_audit(store, client, site)
    report, _ = reports.generate_audit_report(store, client["id"], audit["id"])
    assert report["client_id"] == client["id"]


def test_08_delivery_never_sends_in_tests(store, monkeypatch):
    """B8: SwiftSend.send raises NotImplementedError; Gmail dry_run returns
    DRY_RUN and performs no send (subprocess is guarded)."""
    with pytest.raises(NotImplementedError):
        SwiftSendDelivery().send("x@example.test", "s", "b", dry_run=True)

    def _no_send(*args, **kwargs):  # pragma: no cover
        raise AssertionError("a real send was attempted in a test")

    monkeypatch.setattr(subprocess, "run", _no_send)
    record = GmailDelivery().send(
        "x@example.test", "subject", "body", dry_run=True
    )
    assert record["status"] == "DRY_RUN"
    assert record["to"] == "x@example.test"
    client, _ = _intake_customer(store)
    logged = log_delivery(store, client["id"], record)
    assert logged["status"] == "DRY_RUN"
    assert get_delivery_log(store, client["id"])[0]["status"] == "DRY_RUN"
    with pytest.raises(DeliveryError):
        GmailDelivery().validate_address("not-an-email")


def test_09_duplicate_submission_same_customer(store):
    """B9: submitting the same email twice yields the same customer id."""
    first = customers.self_service_intake(store, "Blue River", EMAIL)
    second = customers.self_service_intake(store, "Blue River", "BLUE@blueriverplumbing.test")
    first_id = clients.find_client_by_email(store, EMAIL)["id"]
    assert clients.find_client_by_email(store, "blue@blueriverplumbing.test")["id"] == first_id
    assert second["resolution"] == "FOUND"
    assert len(clients.list_clients(store)) == 1


def test_10_same_website_audit_version_2(store, stub_engine):
    """B10: auditing the same website again creates version 2."""
    client, site = _intake_customer(store)
    audit_registry.run_audit(store, client["id"], site["id"], max_pages=2)
    audit_registry.run_audit(store, client["id"], site["id"], max_pages=2)
    versions = [
        a["version"]
        for a in audit_registry.list_audits(store, client["id"], site["id"])
    ]
    assert versions == [1, 2]


def test_11_findings_to_tasks_via_fix_plan(store):
    """B11: findings become tracked tasks through the fix plan."""
    client, site = _intake_customer(store)
    audit = _hand_audit(store, client, site)
    plan = fixplans.create_fix_plan(store, client["id"], audit["id"])
    assert len(plan["items"]) == 2
    task_ids = [item["task_id"] for item in plan["items"]]
    listed = [t["id"] for t in tasks.list_tasks(store, client["id"])]
    assert sorted(task_ids) == sorted(listed)


def test_12_completed_fix_reaudit_verifies(store):
    """B12: after a fix is completed and the issue disappears from the
    re-audit, verify_finding returns True."""
    client, site = _intake_customer(store)
    v1 = _hand_audit(store, client, site)
    fixed = v1["findings"][0]
    for state in ("RECOMMENDED", "READY_TO_IMPLEMENT"):
        fixplans.advance_finding(store, client["id"], fixed["id"], state)
    fixplans.advance_finding(
        store, client["id"], fixed["id"], "IMPLEMENTED",
        note="added meta description; deployed",
    )
    v2 = hand_audit_v2(store, client["id"], v1, (fixed["id"],))
    assert reaudit.verify_finding(
        store, client["id"], fixed["id"], v2["id"]
    ) is True


def test_13_audit1_file_byte_identical_after_reaudit(store):
    """B13: writing audit v2 does not alter the audit v1 file."""
    client, site = _intake_customer(store)
    v1 = _hand_audit(store, client, site)
    path = store.client_dir(client["id"]) / "audits" / f"{v1['id']}.json"
    before = path.read_bytes()
    hand_audit_v2(store, client["id"], v1, (v1["findings"][0]["id"],))
    assert path.read_bytes() == before


def test_14_ab_isolation(store):
    """B14: two clients are fully isolated -- B's files never reference A's
    ids, and A's cabinet never includes B's records."""
    a_email, b_email = "a@acmebakery.test", "b@blueriverplumbing.test"
    customers.self_service_intake(store, "Acme Bakery", a_email)
    customers.self_service_intake(store, "Blue River", b_email)
    a = clients.find_client_by_email(store, a_email)
    b = clients.find_client_by_email(store, b_email)
    a_site, _ = websites.add_website(store, a["id"], "https://a.example.test", authorized=True)
    b_site, _ = websites.add_website(store, b["id"], "https://b.example.test", authorized=True)

    a_audit = hand_audit(
        store, a["id"], a_site["id"],
        [{"check": "hsts", "severity": "CRITICAL", "issue": "A issue",
          "evidence": "acme-marker-evidence-001"}],
    )
    b_audit = hand_audit(
        store, b["id"], b_site["id"],
        [{"check": "hsts", "severity": "CRITICAL", "issue": "B issue",
          "evidence": "blueriver-marker-evidence-002"}],
    )
    a_event = money.record_event(
        store, a["id"], kind="INVOICE", amount_dollars=100.0, status="PAID",
        evidence="acme-payment-receipt-001",
    )
    b_event = money.record_event(
        store, b["id"], kind="INVOICE", amount_dollars=200.0, status="PAID",
        evidence="blueriver-payment-receipt-002",
    )
    a_task = tasks.create_task(store, a["id"], None, "Acme task")
    b_task = tasks.create_task(store, b["id"], None, "Blue River task")
    a_report, _ = reports.generate_audit_report(store, a["id"], a_audit["id"])
    b_report, _ = reports.generate_audit_report(store, b["id"], b_audit["id"])

    a_ids = {
        a["id"], a_site["id"], a_audit["id"], a_event["id"], a_task["id"],
        a_report["id"], "acme-marker-evidence-001", "acme-payment-receipt-001",
    } | {f["id"] for f in a_audit["findings"]}
    b_ids = {
        b["id"], b_site["id"], b_audit["id"], b_event["id"], b_task["id"],
        b_report["id"], "blueriver-marker-evidence-002", "blueriver-payment-receipt-002",
    } | {f["id"] for f in b_audit["findings"]}

    # B's directory contains zero references to A's ids (and vice versa).
    for client, other_ids in ((b, a_ids), (a, b_ids)):
        for path in sorted((store.client_dir(client["id"])).rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for other_id in other_ids:
                assert other_id not in text, (
                    f"leak: {other_id} found in {path}"
                )

    # A's cabinet never includes B's money/audits/reports/tasks/evidence.
    cab_a = customers.get_customer_file(store, a_email)
    assert {e["id"] for e in cab_a["money"]} == {a_event["id"]}
    assert {x["id"] for x in cab_a["audits"]} == {a_audit["id"]}
    assert {r["id"] for r in cab_a["deliverables"]} == {a_report["id"]}
    assert {t["id"] for t in tasks.list_tasks(store, a["id"])} == {a_task["id"]}
    a_evidence = " ".join(f["evidence"] for f in cab_a["findings"])
    assert "blueriver-marker-evidence-002" not in a_evidence
    assert "acme-marker-evidence-001" in a_evidence
