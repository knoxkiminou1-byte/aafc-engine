"""Cross-channel relationship findings (first-class findings).

Combines the latest COMPLETE website audit findings, the social footprint
discovery record, and the websites list to emit only evidence-backed
relationships between channels. Finding check names are prefixed
``"xcheck_"``. Because these are inferential (two honest facts joined),
confidence is always LIKELY. Every finding cites evidence from BOTH sides.

Runs are appended to ``crosscheck.json`` as ``{"at", "findings"}`` and the
run is logged to history as ``"crosschecked"``.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import customers, websites
from .auditor import registry as audit_registry
from .store import Store, utc_now_iso

#: Check names this module can emit (also used by services evidence triggers).
XCHECK_NAMES = [
    "xcheck_social_links_missing",
    "xcheck_social_no_website_linkback",
    "xcheck_cta_no_social_path",
    "xcheck_contact_signals_missing",
]

_CROSSCHECK_FILE = "crosscheck.json"


def _finding(
    *,
    check: str,
    page: str,
    issue: str,
    evidence: str,
    severity: str,
    why_it_matters: str,
    recommended_fix: str,
    website_id: str | None,
    client_id: str,
) -> dict:
    """Build one standard-schema cross-channel finding."""
    return {
        "id": "f_" + uuid.uuid4().hex[:12],
        "check": check,
        "page": page,
        "issue": issue,
        "evidence": evidence,
        "severity": severity,
        "confidence": "LIKELY",
        "why_it_matters": why_it_matters,
        "recommended_fix": recommended_fix,
        "implementation_path": "",
        "verification_method": f"re-run {check} and confirm the finding no longer applies",
        "result": "FAIL",
        "status": "FOUND",
        "audit_id": None,
        "website_id": website_id,
        "client_id": client_id,
    }


def _latest_complete_audit(store: Store, client_id: str) -> dict | None:
    """Return the newest COMPLETE website audit for a client, or None."""
    complete = [
        a for a in audit_registry.list_audits(store, client_id)
        if a.get("status") == "COMPLETE"
    ]
    return complete[-1] if complete else None


def _latest_run(store: Store, client_id: str) -> dict | None:
    """Return the most recent crosscheck run, or None."""
    runs = store.read_json(client_id, _CROSSCHECK_FILE, default=[]) or []
    return runs[-1] if runs else None


def crosscheck(store: Store, client_id: str) -> list[dict]:
    """Emit evidence-backed cross-channel relationship findings.

    Inputs: latest COMPLETE website audit findings, ``social.json`` (the
    footprint discovery record, including any ``last_audit`` results), and
    the websites list. Only relationships supported by evidence on both
    sides are emitted.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        List of standard-schema findings with ``"xcheck_"`` check names.
    """
    audit = _latest_complete_audit(store, client_id)
    audit_findings: list[dict] = audit.get("findings", []) if audit else []
    website_id = audit.get("website_id") if audit else None
    website_list = websites.list_websites(store, client_id)

    social_doc = store.read_json(client_id, "social.json", default=None) or {}
    profiles = social_doc.get("profiles", [])
    verified = [p for p in profiles if p.get("status") == "PUBLICLY VERIFIED"]
    website_url = social_doc.get("website_url", "")
    contact_signals = social_doc.get("contact_signals", {})

    findings: list[dict] = []
    page = website_url or (website_list[0]["url"] if website_list else "UNKNOWN")

    cta_fails = [
        f for f in audit_findings
        if f.get("check") == "cta" and f.get("result") == "FAIL"
    ]
    platform_names = [p["platform"] for p in profiles] or ["(none discovered)"]

    # 1. Website has zero discoverable social links -> opportunity.
    if social_doc and not verified and website_list:
        severity = "MEDIUM" if cta_fails else "LOW"
        findings.append(
            _finding(
                check="xcheck_social_links_missing",
                page=page,
                issue="Website has no discoverable social profiles",
                evidence=(
                    f"footprint discovery on {website_url or page} found 0 "
                    f"links to any tracked platform ({', '.join(platform_names)}); "
                    f"latest COMPLETE audit ({audit['id'] if audit else 'none'}) "
                    "crawled the site and its homepage link extraction is the "
                    "basis for this claim"
                ),
                severity=severity,
                why_it_matters=(
                    "Visitors who want to follow the business on social have "
                    "no path from the website, so social proof and repeat "
                    "contact are lost."
                ),
                recommended_fix=(
                    "Decide which social platforms the business actually uses, "
                    "then add real profile links to the website footer/header."
                ),
                website_id=website_id,
                client_id=client_id,
            )
        )

    # 2. Social profile exists but does not link back to the website.
    for profile in verified:
        last_audit = profile.get("last_audit") or {}
        linkback_fails = [
            f for f in last_audit.get("findings", [])
            if f.get("check") == f"social_{profile['platform']}_website_linkback"
            and f.get("result") == "FAIL"
        ]
        for failed in linkback_fails:
            findings.append(
                _finding(
                    check="xcheck_social_no_website_linkback",
                    page=profile["url"],
                    issue=(
                        f"{profile['platform']} profile exists but does not "
                        "link back to the website"
                    ),
                    severity="LOW",
                    evidence=(
                        f"social audit finding {failed['id']} "
                        f"({failed['check']}) observed no website link in the "
                        f"fetched profile HTML for {profile['url']}, while the "
                        f"website {website_url or page} links out to this "
                        "profile -- traffic flows one way only"
                    ),
                    why_it_matters=(
                        "The profile sends visitors nowhere; the business "
                        "gets visibility but no website traffic from it."
                    ),
                    recommended_fix=(
                        "Add the website URL to the profile's website/link field."
                    ),
                    website_id=website_id,
                    client_id=client_id,
                )
            )

    # 3. Website has no CTA and no social path either -> compounded.
    if cta_fails and social_doc and not verified:
        findings.append(
            _finding(
                check="xcheck_cta_no_social_path",
                page=page,
                issue="No call-to-action on the website and no social path either",
                evidence=(
                    f"website audit finding {cta_fails[0]['id']} (check 'cta') "
                    "found no clear call-to-action on the site, AND footprint "
                    f"discovery found 0 verified social profiles ({', '.join(platform_names)}) "
                    "-- there is no conversion path and no alternate channel"
                ),
                severity="MEDIUM",
                why_it_matters=(
                    "A visitor who is ready to act has nowhere to go: no "
                    "button, no form prompt, no social follow."
                ),
                recommended_fix=(
                    "Add at least one clear call-to-action; separately, decide "
                    "whether social profiles should exist and be linked."
                ),
                website_id=website_id,
                client_id=client_id,
            )
        )

    # 4. No email/phone contact signals on the homepage.
    if social_doc and contact_signals:
        mailto = int(contact_signals.get("mailto_count", 0) or 0)
        tel = int(contact_signals.get("tel_count", 0) or 0)
        if mailto == 0 and tel == 0:
            findings.append(
                _finding(
                    check="xcheck_contact_signals_missing",
                    page=page,
                    issue="No email or phone contact signals on the homepage",
                    evidence=(
                        f"footprint discovery on {website_url or page} counted "
                        "0 mailto: links and 0 tel: links in the homepage HTML"
                    ),
                    severity="LOW",
                    why_it_matters=(
                        "Visitors who prefer to call or email must hunt for "
                        "contact details, and every extra step loses people."
                    ),
                    recommended_fix=(
                        "Add a visible email and/or phone link to the homepage."
                    ),
                    website_id=website_id,
                    client_id=client_id,
                )
            )

    run = {"at": utc_now_iso(), "findings": findings}
    store.append_json_list(client_id, run, _CROSSCHECK_FILE)
    customers.log_history(
        store,
        client_id,
        "crosschecked",
        f"{len(findings)} cross-channel finding(s)",
    )
    return findings


def latest_crosscheck_findings(store: Store, client_id: str) -> list[dict]:
    """Return the findings from the most recent crosscheck run.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        Findings list (empty when no run exists).
    """
    run = _latest_run(store, client_id)
    return list(run.get("findings", [])) if run else []
