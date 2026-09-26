"""Per-property social audit (public pages only, read-only).

For every profile that :mod:`footprint` marked PUBLICLY VERIFIED, runs
simple public checks: is the profile URL reachable, does the fetched page
link back to the customer's website, and are there basic branding/bio
signals in the fetched HTML.

Blocked pages are reported honestly as ERROR findings (severity LOW,
confidence NEEDS MANUAL REVIEW) -- never faked. Content claims are made
only from HTML that was actually fetched. No verified properties -> []
(an honest empty result, not a failure).
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlparse

import requests

from . import customers
from .store import Store, utc_now_iso

UA = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AAFC-Social-Audit/1.0; read-only public audit)"
    )
}
TIMEOUT = 12


def _finding(
    *,
    platform: str,
    aspect: str,
    page: str,
    issue: str,
    evidence: str,
    severity: str,
    confidence: str,
    why_it_matters: str,
    recommended_fix: str,
    result: str,
    website_id: str | None,
    client_id: str,
) -> dict:
    """Build one standard-schema finding for a social property."""
    return {
        "id": "f_" + uuid.uuid4().hex[:12],
        "check": f"social_{platform}_{aspect}",
        "page": page,
        "issue": issue,
        "evidence": evidence,
        "severity": severity,
        "confidence": confidence,
        "why_it_matters": why_it_matters,
        "recommended_fix": recommended_fix,
        "implementation_path": "",
        "verification_method": f"re-run social_{platform}_{aspect} against {page}",
        "result": result,
        "status": "FOUND",
        "audit_id": None,
        "website_id": website_id,
        "client_id": client_id,
    }


def _fetch_profile(url: str) -> dict:
    """GET a public profile URL (own tiny fetch; read-only).

    Args:
        url: Profile URL.

    Returns:
        Dict with ``ok``, ``status``, ``text``, ``error``.
    """
    try:
        response = requests.get(url, headers=UA, timeout=TIMEOUT,
                                allow_redirects=True)
        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "text": response.text if response.status_code == 200 else "",
            "error": None
            if response.status_code == 200
            else f"HTTP {response.status_code}",
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _page_title(text: str) -> str:
    """Best-effort <title> extraction from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200]


def audit_social(store: Store, client_id: str) -> list[dict]:
    """Audit each PUBLICLY VERIFIED social profile for a customer.

    For each verified profile: (1) reachability check, (2) website-linkback
    check (only when the fetch succeeded), (3) branding/bio signals (only
    from fetched HTML). Results are stored under each profile's
    ``"last_audit"`` key in ``social.json``. Logs ``"social_audited"``.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        List of standard-schema findings (empty when there are no
        verified properties -- honest, not a failure).
    """
    social_doc = store.read_json(client_id, "social.json", default=None)
    if not social_doc:
        customers.log_history(
            store, client_id, "social_audited", "no footprint discovery on record"
        )
        return []
    profiles = social_doc.get("profiles", [])
    verified = [
        p for p in profiles
        if p.get("status") == "PUBLICLY VERIFIED" and p.get("url")
    ]
    website_id = social_doc.get("website_id")
    website_url = social_doc.get("website_url", "")
    website_host = urlparse(website_url).netloc.lower()

    client = store.read_json(client_id, "client.json", default={}) or {}
    business_name = (client.get("business_name") or "").strip()

    all_findings: list[dict] = []
    audited_at = utc_now_iso()
    for profile in verified:
        platform = profile["platform"]
        url = profile["url"]
        result = _fetch_profile(url)

        findings: list[dict] = []
        if result["ok"]:
            findings.append(
                _finding(
                    platform=platform, aspect="reachable", page=url,
                    issue=f"{platform} profile is publicly reachable",
                    evidence=f"GET {url} -> HTTP 200",
                    severity="PASS", confidence="CONFIRMED",
                    why_it_matters=(
                        "A reachable public profile is a live touchpoint "
                        "customers can find and trust."
                    ),
                    recommended_fix="",
                    result="PASS", website_id=website_id, client_id=client_id,
                )
            )
            text = result["text"]
            if website_host and website_host in text.lower():
                findings.append(
                    _finding(
                        platform=platform, aspect="website_linkback", page=url,
                        issue=f"{platform} profile links back to the website",
                        evidence=(
                            f"website host {website_host} appears in the "
                            f"fetched profile HTML for {url}"
                        ),
                        severity="PASS", confidence="CONFIRMED",
                        why_it_matters=(
                            "A link back turns social visitors into website "
                            "visitors."
                        ),
                        recommended_fix="",
                        result="PASS", website_id=website_id,
                        client_id=client_id,
                    )
                )
            else:
                findings.append(
                    _finding(
                        platform=platform, aspect="website_linkback", page=url,
                        issue=f"{platform} profile does not link back to the website",
                        evidence=(
                            f"website host {website_host or website_url} not found "
                            f"in the fetched profile HTML for {url}"
                        ),
                        severity="LOW", confidence="CONFIRMED",
                        why_it_matters=(
                            "Social visitors have no path to the website, so "
                            "the profile cannot drive traffic or leads."
                        ),
                        recommended_fix=(
                            "Add the website URL to the profile's website/link field."
                        ),
                        result="FAIL", website_id=website_id,
                        client_id=client_id,
                    )
                )
            title = _page_title(text)
            if business_name and business_name.lower() in text.lower():
                findings.append(
                    _finding(
                        platform=platform, aspect="branding", page=url,
                        issue=f"{platform} profile mentions the business name",
                        evidence=(
                            f"business name {business_name!r} appears in the "
                            f"fetched profile HTML (page title: {title!r})"
                        ),
                        severity="PASS", confidence="CONFIRMED",
                        why_it_matters=(
                            "Consistent naming across channels builds trust."
                        ),
                        recommended_fix="",
                        result="PASS", website_id=website_id,
                        client_id=client_id,
                    )
                )
            else:
                findings.append(
                    _finding(
                        platform=platform, aspect="branding", page=url,
                        issue=f"{platform} profile branding could not be confirmed",
                        evidence=(
                            f"business name {business_name!r} not found in the "
                            f"fetched profile HTML (page title: {title!r}); "
                            "the page may render branding via JavaScript, "
                            "which a static fetch cannot see"
                        ),
                        severity="LOW", confidence="NEEDS MANUAL REVIEW",
                        why_it_matters=(
                            "If visitors cannot tell the profile belongs to "
                            "the business, it does not build trust."
                        ),
                        recommended_fix=(
                            "Confirm the profile displays the business name "
                            "and logo; a human review is needed."
                        ),
                        result="FAIL", website_id=website_id,
                        client_id=client_id,
                    )
                )
        else:
            # Blocked or unreachable: honest ERROR, never faked content.
            status_note = (
                f"HTTP {result['status']}" if result["status"] else result["error"]
            )
            findings.append(
                _finding(
                    platform=platform, aspect="reachable", page=url,
                    issue=f"{platform} profile could not be checked publicly",
                    evidence=(
                        f"GET {url} -> {status_note}; the platform blocked "
                        "the fetch or the page is unavailable, so no content "
                        "claims are made"
                    ),
                    severity="LOW", confidence="NEEDS MANUAL REVIEW",
                    why_it_matters=(
                        "A profile that cannot be verified publicly may be "
                        "private, restricted, or mis-linked."
                    ),
                    recommended_fix=(
                        "Open the profile in a browser and confirm it is "
                        "public and correct."
                    ),
                    result="ERROR", website_id=website_id, client_id=client_id,
                )
            )

        profile["last_audit"] = {"audited_at": audited_at, "findings": findings}
        all_findings.extend(findings)

    store.write_json(client_id, social_doc, "social.json")
    customers.log_history(
        store,
        client_id,
        "social_audited",
        f"{len(verified)} verified propertie(s) audited; "
        f"{len(all_findings)} finding(s)",
    )
    return all_findings
