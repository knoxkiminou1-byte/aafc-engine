"""Base spec M: the full engine workflow, end to end (A1-A15).

Fictitious data only ("Acme Bakery", @example.test). Audits run through
the real registry (registry.run_audit) with the vendored engine stubbed
via the ``stub_engine`` fixture -- no network. Live end-to-end network
audits live in test_audit_live.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aafc_engine import (
    clients,
    fixplans,
    money,
    projects,
    reaudit,
    reports,
    tasks,
    websites,
)
from aafc_engine.auditor import registry as audit_registry
from aafc_engine.reports import EM_DASH, ReportError

from conftest import hand_audit, hand_audit_v2


def test_01_add_website(store, acme_client):
    """A1: a website can be added to a client."""
    site, created = websites.add_website(
        store, acme_client["id"], "https://example.com", label="main"
    )
    assert created is True
    assert site["url"] == "https://example.com"
    assert site["client_id"] == acme_client["id"]
    assert site["id"].startswith("web_")


def test_02_run_audit(store, acme_client, acme_site, stub_engine):
    """A2: the audit runs through the registry on an authorized website."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    assert audit["status"] == "COMPLETE"
    assert audit["version"] == 1
    assert audit["website_id"] == acme_site["id"]
    saved = audit_registry.get_audit(store, acme_client["id"], audit["id"])
    assert saved["id"] == audit["id"]


def test_03_findings_produced(store, acme_client, acme_site, stub_engine):
    """A3: the audit produces findings."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    assert len(audit["findings"]) == 4
    checks = {f["check"] for f in audit["findings"]}
    assert {"title", "meta_description", "hsts", "https"} <= checks


def test_04_every_finding_has_evidence(store, acme_client, acme_site, stub_engine):
    """A4: every finding carries non-empty evidence."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    assert audit["findings"], "expected findings to check"
    for finding in audit["findings"]:
        assert (finding.get("evidence") or "").strip(), (
            f"finding {finding['id']} ({finding['check']}) has empty evidence"
        )


def test_05_report_generated_with_exact_headings(
    store, acme_client, acme_site, stub_engine
):
    """A5: the report is generated with the exact contract headings."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    report, md_path = reports.generate_audit_report(
        store, acme_client["id"], audit["id"]
    )
    expected_headings = [
        f"# Website Audit Report {EM_DASH} example.com",
        "## Score",
        "## What we checked",
        "## What we found",
        "## Where we found it",
        "## Why it matters",
        "## What should change",
        "## What AAFC can do",
        "## How we will verify the fix",
        "## Limitations",
    ]
    md = Path(md_path).read_text(encoding="utf-8")
    for heading in expected_headings:
        assert heading in md, f"missing heading: {heading!r}"
    assert report["client_id"] == acme_client["id"]
    assert report["audit_id"] == audit["id"]


def test_06_fix_plan_created(store, acme_client, acme_site, stub_engine):
    """A6: a fix plan is created with one task per failing finding."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    plan = fixplans.create_fix_plan(store, acme_client["id"], audit["id"])
    # CRITICAL (hsts) + HIGH (title, meta_description); LOW (https) excluded
    assert len(plan["items"]) == 3
    assert plan["audit_id"] == audit["id"]
    for item in plan["items"]:
        assert item["task_id"].startswith("tsk_")
        assert item["state"] == "RECOMMENDED"
    assert len(tasks.list_tasks(store, acme_client["id"])) == 3


def test_07_project_created(store, acme_client, acme_site, stub_engine):
    """A7: a project can be created off the audit."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    project = projects.create_project(
        store,
        acme_client["id"],
        "Acme Bakery site rebuild",
        website_id=acme_site["id"],
        audit_id=audit["id"],
        stage="PROPOSAL",
    )
    assert project["id"].startswith("prj_")
    assert project["stage"] == "PROPOSAL"
    assert project["audit_id"] == audit["id"]


def test_08_task_tracked(store, acme_client, stub_engine):
    """A8: tasks are tracked against a project/finding."""
    project = projects.create_project(store, acme_client["id"], "Acme fixes")
    task = tasks.create_task(
        store, acme_client["id"], project["id"], "Add HSTS header",
        notes="strict-transport-security",
    )
    assert task["status"] == "TODO"
    assert task["completed_at"] is None
    listed = tasks.list_tasks(store, acme_client["id"], project_id=project["id"])
    assert [t["id"] for t in listed] == [task["id"]]
    done = tasks.complete_task(store, acme_client["id"], task["id"], note="done")
    assert done["status"] == "DONE"
    assert done["completed_at"]


def test_09_money_event_recorded(store, acme_client):
    """A9: a money event is recorded; the hard rule rejects receivables
    without evidence."""
    with pytest.raises(money.MoneyError):
        money.record_event(
            store, acme_client["id"], kind="INVOICE", amount_dollars=500.0,
            status="OWED", evidence="",
        )
    event = money.record_event(
        store,
        acme_client["id"],
        kind="INVOICE",
        amount_dollars=500.0,
        status="EXPECTED",
        evidence="invoice INV-001 emailed 2026-09-26",
    )
    assert event["amount_cents"] == 50000
    assert event["currency"] == "USD"
    assert money.outstanding_cents(store, acme_client["id"]) == 0  # EXPECTED ignored


def test_10_fix_completed(store, acme_client, acme_site, stub_engine):
    """A10: a finding advances FOUND -> ... -> IMPLEMENTED with a note."""
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    finding = audit["findings"][0]
    mid = fixplans.advance_finding(
        store, acme_client["id"], finding["id"], "RECOMMENDED"
    )
    assert mid["status"] == "RECOMMENDED"
    mid = fixplans.advance_finding(
        store, acme_client["id"], finding["id"], "READY_TO_IMPLEMENT"
    )
    assert mid["status"] == "READY_TO_IMPLEMENT"
    done = fixplans.advance_finding(
        store,
        acme_client["id"],
        finding["id"],
        "IMPLEMENTED",
        note="added HSTS header; redeployed; header now present",
    )
    assert done["status"] == "IMPLEMENTED"
    # VERIFIED is reserved for reaudit.verify_finding
    with pytest.raises(ValueError):
        fixplans.advance_finding(
            store, acme_client["id"], finding["id"], "VERIFIED"
        )


def test_11_rerun_audit_second_version(
    store, acme_client, acme_site, stub_engine
):
    """A11: re-running the audit creates version 2."""
    first = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    second = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]


def test_12_issue_changed_state(store, acme_client, acme_site, stub_engine):
    """A12: a finding whose key disappears from audit v2 verifies."""
    v1 = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    gone = v1["findings"][0]
    v2 = hand_audit_v2(store, acme_client["id"], v1, (gone["id"],))
    assert v2["version"] == 2
    assert reaudit.verify_finding(store, acme_client["id"], gone["id"], v2["id"]) is True
    from aafc_engine import findings as findings_mod

    assert findings_mod.get_finding(
        store, acme_client["id"], gone["id"]
    )["status"] == "VERIFIED"


def test_13_post_fix_verification_report(
    store, acme_client, acme_site, stub_engine
):
    """A13: a before/after verification report is generated from real
    evidence only."""
    v1 = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    gone = v1["findings"][0]
    v2 = hand_audit_v2(store, acme_client["id"], v1, (gone["id"],))
    report, md_path = reports.generate_verification_report(
        store, acme_client["id"], v1["id"], v2["id"]
    )
    md = Path(md_path).read_text(encoding="utf-8")
    assert "resolved" in md.lower()
    assert report["client_id"] == acme_client["id"]
    assert report["old_audit_id"] == v1["id"]
    assert report["new_audit_id"] == v2["id"]


def test_14_duplicate_audits_no_duplicate_clients(
    store, acme_client, acme_site, stub_engine
):
    """A14: running the audit twice does not duplicate clients/websites;
    versions 1 and 2 are produced."""
    audit_registry.run_audit(store, acme_client["id"], acme_site["id"], max_pages=2)
    audit_registry.run_audit(store, acme_client["id"], acme_site["id"], max_pages=2)
    assert len(clients.list_clients(store)) == 1
    assert len(websites.list_websites(store, acme_client["id"])) == 1
    versions = [
        a["version"]
        for a in audit_registry.list_audits(
            store, acme_client["id"], acme_site["id"]
        )
    ]
    assert versions == [1, 2]


def test_15_failed_audit_blocked(store, acme_client, acme_site, monkeypatch):
    """A15: a 0-page crawl yields status FAILED and report generation
    raises ReportError.

    Simulated by monkeypatching the vendored engine's ``audit_site`` to
    return pages_crawled=0 (the registry must never fake success).
    """
    def fake_audit_site(url: str, max_pages: int = 6) -> dict:
        return {
            "url": url,
            "pages_crawled": 0,
            "score": 0,
            "grade": "F",
            "counts": {"critical": 0, "warning": 0, "info": 0},
            "findings": [],
            "elapsed_total": 0.0,
            "notes": ["simulated: pages_crawled=0"],
        }

    monkeypatch.setattr(
        "aafc_engine.auditor.registry.engine.audit_site", fake_audit_site
    )
    audit = audit_registry.run_audit(
        store, acme_client["id"], acme_site["id"], max_pages=2
    )
    assert audit["status"] == "FAILED"
    with pytest.raises(ReportError):
        reports.generate_audit_report(store, acme_client["id"], audit["id"])
