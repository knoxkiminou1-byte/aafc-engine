"""Re-audit comparison and finding verification for the AAFC business engine.

After fixes are implemented, a fresh audit is compared against the original:
findings whose (check, page) key disappeared are resolved, keys present in
both are still open, and keys only in the new audit are new issues. A finding
may only be marked VERIFIED here, never through fixplans.advance_finding.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import findings
from .store import Store


def _now() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _load_audit(store: Store, client_id: str, audit_id: str) -> dict[str, Any]:
    """Load an audit by id. Raises ValueError when it does not exist."""
    audit = store.read_json(client_id, "audits", f"{audit_id}.json")
    if audit is None:
        raise ValueError(f"audit not found: {audit_id}")
    return audit


def _require_complete(audit: dict[str, Any], role: str) -> None:
    """Raise ValueError unless the audit completed successfully."""
    if audit.get("status") != "COMPLETE":
        raise ValueError(
            f"{role} audit {audit.get('id')!r} has status {audit.get('status')!r}: "
            "both audits must be COMPLETE to compare or verify"
        )


def compare(
    store: Store, client_id: str, old_audit_id: str, new_audit_id: str
) -> dict[str, Any]:
    """Compare two audits finding-by-finding.

    Both audits must have status COMPLETE, else ValueError (a failed audit
    carries no trustworthy evidence). Findings are matched by
    findings.finding_key (check, page):

    - resolved: findings in the old audit whose key is absent from the new audit
    - still_open: findings whose key is in both (the new audit's copy, i.e.
      the current evidence, is returned)
    - new: findings in the new audit whose key was not in the old audit

    Only recorded findings are reported; no improvement numbers are invented.
    """
    old_audit = _load_audit(store, client_id, old_audit_id)
    new_audit = _load_audit(store, client_id, new_audit_id)
    _require_complete(old_audit, "old")
    _require_complete(new_audit, "new")

    old_findings = old_audit.get("findings", [])
    new_findings = new_audit.get("findings", [])

    old_keys = {findings.finding_key(f) for f in old_findings}
    new_keys = {findings.finding_key(f) for f in new_findings}

    resolved = [f for f in old_findings if findings.finding_key(f) not in new_keys]
    still_open = [f for f in new_findings if findings.finding_key(f) in old_keys]
    new_issues = [f for f in new_findings if findings.finding_key(f) not in old_keys]

    counts = {
        "resolved": len(resolved),
        "still_open": len(still_open),
        "new": len(new_issues),
    }
    summary = (
        f"{counts['resolved']} resolved, {counts['still_open']} still open, "
        f"{counts['new']} new (evidence-based, no invented numbers)"
    )
    return {
        "old_audit_id": old_audit_id,
        "new_audit_id": new_audit_id,
        "at": _now(),
        "resolved": resolved,
        "new": new_issues,
        "still_open": still_open,
        "counts": counts,
        "summary": summary,
    }


def verify_finding(
    store: Store, client_id: str, finding_id: str, new_audit_id: str
) -> bool:
    """Verify a finding against a newer audit.

    Returns True and sets the finding's status to VERIFIED (via
    findings.set_finding_status) when the finding's (check, page) key is
    absent from the new audit. Returns False and leaves the finding
    untouched when the key is still present. The new audit must be COMPLETE,
    else ValueError -- verifying against a failed audit would fake success.
    Raises KeyError when the finding does not exist.
    """
    finding = findings.get_finding(store, client_id, finding_id)
    new_audit = _load_audit(store, client_id, new_audit_id)
    _require_complete(new_audit, "new")

    key = findings.finding_key(finding)
    new_keys = {findings.finding_key(f) for f in new_audit.get("findings", [])}
    if key in new_keys:
        return False
    findings.set_finding_status(store, client_id, finding_id, "VERIFIED")
    return True
