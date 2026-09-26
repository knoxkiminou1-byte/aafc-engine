"""Live audit tests (D) -- real network audits against public fixture sites.

Marked @pytest.mark.live. Page counts are kept tiny (max_pages=2) to be
polite. These are the only tests besides T-D that touch the network; they
prove the real engine + registry path works end to end.
"""
from __future__ import annotations

import pytest

from aafc_engine import clients, websites
from aafc_engine.auditor import registry as audit_registry


def _live_audit(store, email: str, url: str) -> dict:
    client, _ = clients.create_client(
        store, "Live Fixture", email, business_name="Live Fixture Co"
    )
    site, _ = websites.add_website(
        store, client["id"], url, authorized=True
    )
    return audit_registry.run_audit(store, client["id"], site["id"], max_pages=2)


@pytest.mark.live
def test_live_audit_example_com(store):
    """Real audit of https://example.com: COMPLETE, score/grade present,
    every finding carries evidence."""
    audit = _live_audit(store, "live-acme@example.test", "https://example.com")
    assert audit["status"] == "COMPLETE"
    assert audit["pages_crawled"] > 0
    assert audit.get("score") is not None
    assert audit.get("grade")
    assert audit["findings"], "expected at least one finding"
    for finding in audit["findings"]:
        assert (finding.get("evidence") or "").strip(), (
            f"finding {finding['id']} ({finding['check']}) lacks evidence"
        )


@pytest.mark.live
def test_live_audit_iana(store):
    """Real audit of https://www.iana.org: COMPLETE, score/grade present,
    every finding carries evidence."""
    audit = _live_audit(store, "live-blue@example.test", "https://www.iana.org")
    assert audit["status"] == "COMPLETE"
    assert audit["pages_crawled"] > 0
    assert audit.get("score") is not None
    assert audit.get("grade")
    assert audit["findings"], "expected at least one finding"
    for finding in audit["findings"]:
        assert (finding.get("evidence") or "").strip(), (
            f"finding {finding['id']} ({finding['check']}) lacks evidence"
        )
