"""Opportunity map (evidence-gated; recommendation != revenue).

Builds a customer's opportunity map from real evidence only: the latest
COMPLETE website audit findings, social audit findings, and crosscheck
findings, matched against the AAFC service catalog. A service becomes an
opportunity ONLY when at least one finding's check is in its
``evidence_triggers`` -- never padded, never invented.

This module NEVER touches money records: it does not import or call
``money.py``, and opportunity records carry no amounts. A recommendation
is not revenue; a proposal is not revenue.
"""

from __future__ import annotations

from typing import Any

from . import crosscheck, customers, services
from .auditor import registry as audit_registry
from .store import Store, utc_now_iso

OPPORTUNITY_STATES = [
    "DISCOVERED",
    "RECOMMENDED",
    "DISCUSSION",
    "PROPOSAL",
    "APPROVED",
    "IN PROGRESS",
    "COMPLETED",
    "VERIFIED",
    "DECLINED",
    "NOT CURRENTLY RELEVANT",
]

_OPPORTUNITIES_FILE = "opportunities.json"


def _collect_evidence_findings(store: Store, client_id: str) -> list[dict]:
    """Gather all evidence findings for a customer.

    Combines: latest COMPLETE website audit findings, every social audit
    finding stored under profiles' ``last_audit``, and the latest
    crosscheck run's findings.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        Combined findings list (may be empty).
    """
    evidence: list[dict] = []
    complete = [
        a for a in audit_registry.list_audits(store, client_id)
        if a.get("status") == "COMPLETE"
    ]
    if complete:
        evidence.extend(complete[-1].get("findings", []))
    social_doc = store.read_json(client_id, "social.json", default=None) or {}
    for profile in social_doc.get("profiles", []):
        last_audit = profile.get("last_audit") or {}
        evidence.extend(last_audit.get("findings", []))
    evidence.extend(crosscheck.latest_crosscheck_findings(store, client_id))
    return evidence


def _read_opportunities(store: Store, client_id: str) -> list[dict]:
    """Return stored opportunity records (empty list when none)."""
    doc = store.read_json(client_id, _OPPORTUNITIES_FILE, default=None) or {}
    opportunities = doc.get("opportunities", [])
    return opportunities if isinstance(opportunities, list) else []


def _write_opportunities(store: Store, client_id: str, opportunities: list[dict]) -> None:
    """Persist the opportunity list atomically."""
    store.write_json(
        client_id,
        {"updated_at": utc_now_iso(), "opportunities": opportunities},
        _OPPORTUNITIES_FILE,
    )


def build_opportunity_map(
    store: Store, client_id: str, current_offering: str | None = None
) -> dict:
    """Build (or refresh) a customer's evidence-gated opportunity map.

    Gathers website + social + crosscheck findings, runs
    :func:`services.match_services`, and creates one opportunity record
    per matched service. Dedupe: an existing opportunity with the same
    service and the same set of triggering findings is not duplicated;
    existing records keep their status and are otherwise refreshed.

    Args:
        store: The Store.
        client_id: The client identifier.
        current_offering: The customer's current offering/service slug
            (recorded for context on each opportunity).

    Returns:
        ``{"customer_id", "built_at", "current_offering",
        "opportunities": [...]}``. Each opportunity has: id ``"opp_<hex>"``,
        customer_id, problem, evidence, current_offering,
        additional_offering (service name), why_it_fits, aafc_capability,
        implementation_path, pricing_model, status ``"DISCOVERED"`` (for new
        records), triggering_findings [finding ids].
    """
    findings = _collect_evidence_findings(store, client_id)
    findings_by_id = {f.get("id"): f for f in findings}
    matches = services.match_services(findings)
    existing = _read_opportunities(store, client_id)
    built_at = utc_now_iso()

    new_count = 0
    for match in matches:
        service = services.get_service(match["service"])
        triggering = sorted(match["triggered_by"])
        already = any(
            o.get("service") == service["slug"]
            and sorted(o.get("triggering_findings", [])) == triggering
            for o in existing
        )
        if already:
            continue
        problems = []
        for finding_id in triggering:
            finding = findings_by_id.get(finding_id, {})
            issue = finding.get("issue") or finding.get("check") or finding_id
            if issue not in problems:
                problems.append(issue)
        evidence_bits = []
        for finding_id in triggering:
            finding = findings_by_id.get(finding_id, {})
            snippet = (finding.get("evidence") or "")[:160]
            evidence_bits.append(
                f"{finding_id} ({finding.get('check')}: {snippet})"
            )
        opportunity = {
            "id": store.new_id("opp_"),
            "customer_id": client_id,
            "service": service["slug"],
            "problem": "; ".join(problems[:5]),
            "evidence": "findings: " + "; ".join(evidence_bits),
            "current_offering": current_offering,
            "additional_offering": service["name"],
            "why_it_fits": match["why"],
            "aafc_capability": "; ".join(service.get("covers", [])),
            "implementation_path": service.get("implementation_path", ""),
            "pricing_model": service.get("pricing_model"),
            "status": "DISCOVERED",
            "triggering_findings": triggering,
            "created_at": built_at,
        }
        existing.append(opportunity)
        new_count += 1

    _write_opportunities(store, client_id, existing)
    customers.log_history(
        store,
        client_id,
        "opportunity_map_built",
        f"{len(existing)} opportunitie(s) on the map ({new_count} new); "
        f"current_offering={current_offering!r}; no money records touched",
    )
    return {
        "customer_id": client_id,
        "built_at": built_at,
        "current_offering": current_offering,
        "opportunities": existing,
    }


def expand_offerings(store: Store, client_id: str, current_offering: str) -> dict:
    """List evidence-supported expansions beyond the current offering.

    The ONE->MANY: every catalog service with at least one triggering
    finding is listed, each citing the specific finding ids. The current
    offering itself is excluded (it cannot be an expansion of itself).
    When nothing is evidence-supported, ``supported_expansions`` is []
    with an honest note -- the list is never padded.

    Args:
        store: The Store.
        client_id: The client identifier.
        current_offering: The customer's current offering/service slug.

    Returns:
        ``{"current_offering", "supported_expansions": [{"service",
        "triggered_by_findings", "why_it_fits"}], "note"}``.
    """
    findings = _collect_evidence_findings(store, client_id)
    matches = services.match_services(findings)
    expansions = []
    for match in matches:
        if match["service"] == current_offering:
            continue
        service = services.get_service(match["service"])
        expansions.append(
            {
                "service": service["name"],
                "triggered_by_findings": match["triggered_by"],
                "why_it_fits": match["why"],
            }
        )
    note = ""
    if not expansions:
        note = "No catalog service is evidence-supported for expansion right now."
    return {
        "current_offering": current_offering,
        "supported_expansions": expansions,
        "note": note,
    }


def set_opportunity_status(
    store: Store, client_id: str, opp_id: str, status: str
) -> dict:
    """Set an opportunity's lifecycle status.

    Args:
        store: The Store.
        client_id: The client identifier.
        opp_id: The opportunity id.
        status: Must be in :data:`OPPORTUNITY_STATES`.

    Returns:
        The updated opportunity dict.

    Raises:
        ValueError: If ``status`` is not a valid opportunity state.
        KeyError: If no opportunity has ``opp_id``.
    """
    if status not in OPPORTUNITY_STATES:
        raise ValueError(
            f"invalid opportunity status {status!r}; "
            f"must be one of {OPPORTUNITY_STATES}"
        )
    opportunities = _read_opportunities(store, client_id)
    for opportunity in opportunities:
        if opportunity.get("id") == opp_id:
            old = opportunity.get("status")
            opportunity["status"] = status
            _write_opportunities(store, client_id, opportunities)
            customers.log_history(
                store,
                client_id,
                "opportunity_status_changed",
                f"{opp_id}: {old} -> {status}",
            )
            return opportunity
    raise KeyError(f"opportunity not found: {opp_id!r}")


def get_opportunities(
    store: Store, client_id: str, status: str | None = None
) -> list[dict]:
    """Return a customer's opportunities, optionally filtered by status.

    Args:
        store: The Store.
        client_id: The client identifier.
        status: Optional status filter (validated against
            :data:`OPPORTUNITY_STATES`).

    Returns:
        List of opportunity dicts.

    Raises:
        ValueError: If ``status`` is not a valid opportunity state.
    """
    if status is not None and status not in OPPORTUNITY_STATES:
        raise ValueError(
            f"invalid opportunity status {status!r}; "
            f"must be one of {OPPORTUNITY_STATES}"
        )
    opportunities = _read_opportunities(store, client_id)
    if status is None:
        return opportunities
    return [o for o in opportunities if o.get("status") == status]
