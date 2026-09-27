"""Shared fixtures and helpers for the AAFC engine test suite.

All tests use fictitious clients only ("Acme Bakery", "Blue River
Plumbing", "Desert Bloom Barbershop") and @example.test emails. No real
email is ever sent: delivery tests use dry_run or the SwiftSend stub
only. Network access happens only in the real-audit paths the task
explicitly requires (T-D in test_extension.py and the @pytest.mark.live
tests in test_audit_live.py); every other audit test stubs the engine or
hand-crafts audit JSON.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest

from aafc_engine import clients, websites
from aafc_engine.auditor import registry as audit_registry
from aafc_engine.store import Store

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path) -> Store:
    """A fresh Store rooted in a pytest tmp dir (per-test isolation)."""
    return Store(tmp_path / "data")


@pytest.fixture()
def acme_client(store: Store) -> dict:
    """Fictitious client: Acme Bakery."""
    client, _ = clients.create_client(
        store,
        "Acme Bakery",
        "hello@acmebakery.test",
        business_name="Acme Bakery",
    )
    return client


@pytest.fixture()
def acme_site(store: Store, acme_client: dict) -> dict:
    """Fictitious authorized website for Acme Bakery."""
    site, _ = websites.add_website(
        store,
        acme_client["id"],
        "https://example.com",
        label="Acme Bakery site",
        authorized=True,
    )
    return site


def _stub_raw_findings(url: str) -> list[dict]:
    """Raw engine-shaped findings (keys audit_site would emit)."""
    return [
        {
            "page": url,
            "check": "title",
            "severity": "warning",
            "title": "Title length is 14 characters (recommended 30-60)",
            "evidence": 'Title: "Example Domain"',
            "why_it_matters": "why",
            "recommended_fix": "fix",
            "technical": "",
        },
        {
            "page": url,
            "check": "meta_description",
            "severity": "warning",
            "title": "Missing meta description",
            "evidence": 'No <meta name="description"> found.',
            "why_it_matters": "why",
            "recommended_fix": "fix",
            "technical": "",
        },
        {
            "page": url,
            "check": "hsts",
            "severity": "critical",
            "title": "HSTS header not set",
            "evidence": "Response headers lack strict-transport-security.",
            "why_it_matters": "why",
            "recommended_fix": "fix",
            "technical": "",
        },
        {
            "page": url,
            "check": "https",
            "severity": "info",
            "title": "HTTPS OK",
            "evidence": "TLS certificate valid; page served over HTTPS.",
            "why_it_matters": "why",
            "recommended_fix": "fix",
            "technical": "",
        },
    ]


@pytest.fixture()
def stub_engine(monkeypatch) -> None:
    """Stub the vendored engine's audit_site: deterministic, no network.

    Severity mapping exercised: warning->HIGH (x2), critical->CRITICAL,
    info->LOW. Every finding carries non-empty evidence.
    """
    def fake_audit_site(url: str, max_pages: int = 6, **kwargs) -> dict:
        return {
            "url": url,
            "pages_crawled": 2,
            "score": 70,
            "grade": "C",
            "counts": {"critical": 1, "warning": 2, "info": 1},
            "findings": _stub_raw_findings(url),
            "elapsed_total": 0.05,
            "notes": ["fixture: engine stubbed, no network"],
        }

    monkeypatch.setattr(
        "aafc_engine.auditor.registry.engine.audit_site", fake_audit_site
    )


# ---------------------------------------------------------------------------
# hand-made audit helpers (no network at all)
# ---------------------------------------------------------------------------


def make_finding_dict(
    client_id: str,
    website_id: str,
    audit_id: str,
    *,
    check: str = "meta_description",
    severity: str = "LOW",
    page: str = "https://example.com",
    issue: str = "Test issue",
    evidence: str = "concrete evidence for the test finding",
    result: str = "FAIL",
    status: str = "FOUND",
    confidence: str = "CONFIRMED",
) -> dict:
    """Build one finding dict in the repo-wide finding schema."""
    return {
        "id": "f_" + uuid.uuid4().hex[:12],
        "check": check,
        "page": page,
        "issue": issue,
        "evidence": evidence,
        "severity": severity,
        "confidence": confidence,
        "why_it_matters": "test why-it-matters",
        "recommended_fix": "test recommended fix",
        "implementation_path": "",
        "verification_method": f"re-run {check}",
        "result": result,
        "status": status,
        "audit_id": audit_id,
        "website_id": website_id,
        "client_id": client_id,
    }


def hand_audit(
    store: Store,
    client_id: str,
    website_id: str,
    finding_specs: list[dict],
) -> dict:
    """Write a hand-crafted COMPLETE audit JSON (no network).

    finding_specs: kwargs dicts for :func:`make_finding_dict`
    (``evidence`` is required in each spec). Versions are 1-based per
    website, mirroring registry.run_audit.
    """
    audit_id = store.new_id("aud_")
    version = len(audit_registry.list_audits(store, client_id, website_id)) + 1
    findings = [
        make_finding_dict(client_id, website_id, audit_id, **spec)
        for spec in finding_specs
    ]
    audit = {
        "id": audit_id,
        "client_id": client_id,
        "website_id": website_id,
        "url": "https://example.com",
        "version": version,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETE",
        "engine": "kiminou-website-audit/1.0",
        "score": 80,
        "grade": "B",
        "pages_crawled": 2,
        "counts": {"critical": 0, "warning": 0, "info": len(findings)},
        "findings": findings,
        "elapsed_total": 0.01,
        "notes": ["hand-made fixture: no network"],
    }
    store.write_json(client_id, audit, "audits", f"{audit_id}.json")
    return audit


def hand_audit_v2(
    store: Store,
    client_id: str,
    old_audit: dict,
    drop_finding_ids: tuple[str, ...] = (),
) -> dict:
    """Hand-craft a version-2 audit from an existing audit dict.

    Copies the old audit, bumps the version, and drops the given finding
    ids (simulating issues that disappeared after fixes). The old audit
    file is never touched.
    """
    audit = copy.deepcopy(old_audit)
    audit["id"] = store.new_id("aud_")
    audit["version"] = old_audit["version"] + 1
    audit["started_at"] = datetime.now(timezone.utc).isoformat()
    audit["notes"] = ["hand-made fixture v2: no network"]
    dropped = set(drop_finding_ids)
    audit["findings"] = [
        f for f in audit["findings"] if f["id"] not in dropped
    ]
    for finding in audit["findings"]:
        finding["audit_id"] = audit["id"]
    store.write_json(client_id, audit, "audits", f"{audit['id']}.json")
    return audit
