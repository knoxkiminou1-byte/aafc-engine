"""Findings schema and lifecycle helpers for the AAFC engine.

A *finding* is the repo-wide unit of audit output. Raw findings from the
vendored engine (``aafc_engine.auditor.engine``) are converted into this
schema by :func:`from_engine_finding`; every later stage (reports, fix
plans, re-audits) works only with this schema.
"""

from __future__ import annotations

import uuid
from typing import Any

from .store import utc_now_iso

SEVERITIES: list[str] = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "PASS"]
# FIX 7: NEEDS_RENDERED_REVIEW — the raw-HTML evidence contradicts what a
# browser renders (e.g. img-alt counts inflated by hidden/duplicate markup);
# a real rendered-DOM check is required before the finding can be asserted.
CONFIDENCES: list[str] = ["CONFIRMED", "LIKELY", "NEEDS MANUAL REVIEW", "NEEDS_RENDERED_REVIEW"]
FINDING_STATES: list[str] = [
    "FOUND",
    "RECOMMENDED",
    "READY_TO_IMPLEMENT",
    "IMPLEMENTED",
    "VERIFIED",
]

# Engine severity -> our severity. Unknown engine severities fall back to LOW.
SEVERITY_MAP: dict[str, str] = {
    "critical": "CRITICAL",
    "warning": "HIGH",
    "info": "LOW",
}

# ---------------------------------------------------------------------------
# Confidence-by-check mapping table.
#
# Built from the REAL check names emitted by the vendored engine's
# ``check_page`` / ``check_robots_and_sitemap`` / ``check_broken_links`` /
# ``audit_site`` (see aafc_engine/auditor/engine.py):
#
#   fetch, http_status, https, hsts, mixed_content, indexability,
#   title, meta_description, viewport, charset, h1, heading_order, img_alt,
#   canonical, open_graph, structured_data, lang, forms, cta, page_weight,
#   ttfb, robots_txt, sitemap, broken_links, parse, bot_protection
#
# Rules:
#   CONFIRMED            -- directly measured technical facts: HTTP status /
#                           transport / headers / live link fetches / robots.txt
#                           and sitemap.xml probes. No heuristics involved.
#   LIKELY               -- heuristic analysis of parsed HTML (title, headings,
#                           images, CTAs, ...). Correct on well-formed pages but
#                           depends on parser heuristics, so a human should
#                           eyeball it before client delivery.
#   NEEDS MANUAL REVIEW  -- default for anything else: plugin checks registered
#                           later and any engine check name not listed here.
# ---------------------------------------------------------------------------
_CONFIRMED_CHECKS = frozenset(
    {
        "fetch",          # page could not be loaded at all (connection-level)
        "http_status",    # measured HTTP response status >= 400
        "https",          # final URL scheme observed directly
        "hsts",           # response headers observed directly
        "mixed_content",  # insecure resource URLs observed in the HTML
        "indexability",   # noindex directive read verbatim from meta/headers
        "robots_txt",     # live probe of /robots.txt
        "sitemap",        # live probe of /sitemap.xml
        "broken_links",   # live HEAD/GET checks of same-host links
    }
)

_LIKELY_CHECKS = frozenset(
    {
        "title",
        "meta_description",
        "viewport",
        "charset",
        "h1",
        "heading_order",
        "img_alt",
        "canonical",
        "open_graph",
        "structured_data",
        "lang",
        "forms",
        "cta",
        "page_weight",
        "ttfb",
        "parse",          # our own parser hiccup on a page, not a site fact
    }
)

CONFIDENCE_BY_CHECK: dict[str, str] = {
    **{name: "CONFIRMED" for name in _CONFIRMED_CHECKS},
    **{name: "LIKELY" for name in _LIKELY_CHECKS},
    # FIX 9: bot_protection is a *refused measurement*, not a site defect.
    # The refusal itself is directly observed (HTTP 403/429 + bot-wall
    # markers), so the finding is CONFIRMED -- it asserts "we could not
    # measure", never a fake site fact.
    "bot_protection": "CONFIRMED",
}

DEFAULT_CONFIDENCE = "NEEDS MANUAL REVIEW"


def _new_finding_id() -> str:
    """Return a fresh finding id of the form ``f_<12 hex chars>``."""
    return "f_" + uuid.uuid4().hex[:12]


def from_engine_finding(
    raw: dict[str, Any],
    *,
    audit_id: str,
    website_id: str,
    client_id: str,
) -> dict[str, Any]:
    """Convert one raw vendored-engine finding into the repo-wide finding schema.

    ``raw`` keys: page, check, severity, title, evidence, why_it_matters,
    recommended_fix, technical. Engine severities are lowercase
    (``critical``/``warning``/``info``); anything unknown maps to ``LOW``.
    Confidence comes from :data:`CONFIDENCE_BY_CHECK`, defaulting to
    ``NEEDS MANUAL REVIEW``. The implementation path is left empty here --
    the fixplans layer fills it in.
    """
    check = raw.get("check", "")
    page = raw.get("page", "")
    # FIX 6/7: engine review hints override the check-table confidence.
    # review_hint=True  -> page looked JS-rendered, static HTML said nothing
    # review_hint="rendered" -> raw HTML contradicts rendered reality
    #   (e.g. img-alt counts); a browser check is required before asserting.
    review_hint = raw.get("review_hint")
    confidence = CONFIDENCE_BY_CHECK.get(check, DEFAULT_CONFIDENCE)
    if review_hint == "rendered":
        confidence = "NEEDS_RENDERED_REVIEW"
    elif review_hint:
        confidence = "NEEDS MANUAL REVIEW"
    return {
        "id": _new_finding_id(),
        "check": check,
        "page": page,
        # FIX 5: every finding says which page (and what kind) it came from.
        "page_kind": raw.get("page_kind", "content"),
        # FIX 6: how this finding was verified — never claim browser evidence
        # from an HTTP fetch.
        "verification": raw.get("verification", "HTTP_FETCH"),
        "issue": raw.get("title", ""),
        "evidence": raw.get("evidence", ""),
        "severity": SEVERITY_MAP.get(raw.get("severity", ""), "LOW"),
        "confidence": confidence,
        "why_it_matters": raw.get("why_it_matters", ""),
        "recommended_fix": raw.get("recommended_fix", ""),
        "implementation_path": "",
        "verification_method": (
            f"Re-run the '{check}' check against {page} and confirm "
            "the finding no longer appears."
        ),
        "result": "FAIL",
        "status": "FOUND",
        "audit_id": audit_id,
        "website_id": website_id,
        "client_id": client_id,
    }


def _iter_audit_files(store: Any, client_id: str):
    """Yield (audit_id, audit_dict) for every saved audit of a client."""
    audits_dir = store.client_dir(client_id) / "audits"
    if not audits_dir.is_dir():
        return
    for path in sorted(audits_dir.glob("*.json")):
        audit = store.read_json(client_id, "audits", path.name, default=None)
        if isinstance(audit, dict):
            yield path.stem, audit


#: Per-client lifecycle overlay: ``finding_status.json`` maps
#: ``finding_id -> {"status": ..., "history": [{"from", "to", "at"}]}``.
#: The lifecycle status (RECOMMENDED / VERIFIED / ...) is operational state,
#: NOT audit-run history -- so it lives in this overlay and the saved audit
#: JSON files stay append-only and always reflect what the audit actually
#: found (status "FOUND").
_STATUS_FILE = "finding_status.json"


def _load_status_overlay(store: Any, client_id: str) -> dict[str, Any]:
    overlay = store.read_json(client_id, _STATUS_FILE, default={})
    return overlay if isinstance(overlay, dict) else {}


def _save_status_overlay(store: Any, client_id: str, overlay: dict[str, Any]) -> None:
    store.write_json(client_id, overlay, _STATUS_FILE)


def get_finding_status(store: Any, client_id: str, finding_id: str) -> str:
    """Return the finding's current lifecycle status.

    The overlay wins; a finding with no overlay entry is still at its
    audit-time status (``FOUND``).

    Args:
        store: The Store.
        client_id: The client identifier.
        finding_id: The finding identifier.

    Returns:
        The current lifecycle status string.
    """
    entry = _load_status_overlay(store, client_id).get(finding_id)
    if isinstance(entry, dict) and entry.get("status") in FINDING_STATES:
        return entry["status"]
    return "FOUND"


def get_finding(store: Any, client_id: str, finding_id: str) -> dict[str, Any]:
    """Find a finding by id across all of a client's saved audits.

    The returned dict carries the finding's *current lifecycle* status
    (merged from the status overlay); the saved audit files themselves are
    never rewritten, so the audit-time record stays pristine.

    Raises:
        KeyError: if no finding with that id exists for the client.
    """
    for _audit_id, audit in _iter_audit_files(store, client_id):
        for finding in audit.get("findings", []):
            if isinstance(finding, dict) and finding.get("id") == finding_id:
                merged = dict(finding)
                merged["status"] = get_finding_status(store, client_id, finding_id)
                return merged
    raise KeyError(f"finding not found: {finding_id!r}")


def set_finding_status(
    store: Any, client_id: str, finding_id: str, status: str
) -> dict[str, Any]:
    """Set a finding's lifecycle status in the status overlay.

    The saved audit files are append-only: this writes to
    ``finding_status.json`` (with a full from/to/at history) and never
    rewrites the owning audit file, so audit #1's file always reflects what
    audit #1 actually found.

    Raises:
        ValueError: if ``status`` is not in :data:`FINDING_STATES`.
        KeyError: if no finding with that id exists for the client.
    """
    if status not in FINDING_STATES:
        raise ValueError(
            f"invalid finding status {status!r}; must be one of {FINDING_STATES}"
        )
    # Raises KeyError if the finding does not exist for this client.
    finding = get_finding(store, client_id, finding_id)
    old_status = get_finding_status(store, client_id, finding_id)
    overlay = _load_status_overlay(store, client_id)
    entry = overlay.get(finding_id)
    if not isinstance(entry, dict):
        entry = {"status": old_status, "history": []}
    history = entry.setdefault("history", [])
    if isinstance(history, list):
        history.append({"from": old_status, "to": status, "at": utc_now_iso()})
    entry["status"] = status
    overlay[finding_id] = entry
    _save_status_overlay(store, client_id, overlay)
    merged = dict(finding)
    merged["status"] = status
    return merged


def finding_key(finding: dict[str, Any]) -> tuple[str, str]:
    """Return the re-audit matching key for a finding: ``(check, page)``."""
    return (finding.get("check", ""), finding.get("page", ""))
