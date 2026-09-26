"""Findings schema and lifecycle helpers for the AAFC engine.

A *finding* is the repo-wide unit of audit output. Raw findings from the
vendored engine (``aafc_engine.auditor.engine``) are converted into this
schema by :func:`from_engine_finding`; every later stage (reports, fix
plans, re-audits) works only with this schema.
"""

from __future__ import annotations

import uuid
from typing import Any

SEVERITIES: list[str] = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "PASS"]
CONFIDENCES: list[str] = ["CONFIRMED", "LIKELY", "NEEDS MANUAL REVIEW"]
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
#   ttfb, robots_txt, sitemap, broken_links, parse
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
    return {
        "id": _new_finding_id(),
        "check": check,
        "page": page,
        "issue": raw.get("title", ""),
        "evidence": raw.get("evidence", ""),
        "severity": SEVERITY_MAP.get(raw.get("severity", ""), "LOW"),
        "confidence": CONFIDENCE_BY_CHECK.get(check, DEFAULT_CONFIDENCE),
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


def get_finding(store: Any, client_id: str, finding_id: str) -> dict[str, Any]:
    """Find a finding by id across all of a client's saved audits.

    Raises:
        KeyError: if no finding with that id exists for the client.
    """
    for _audit_id, audit in _iter_audit_files(store, client_id):
        for finding in audit.get("findings", []):
            if isinstance(finding, dict) and finding.get("id") == finding_id:
                return finding
    raise KeyError(f"finding not found: {finding_id!r}")


def set_finding_status(
    store: Any, client_id: str, finding_id: str, status: str
) -> dict[str, Any]:
    """Set a finding's lifecycle status and rewrite the owning audit file.

    Raises:
        ValueError: if ``status`` is not in :data:`FINDING_STATES`.
        KeyError: if no finding with that id exists for the client.
    """
    if status not in FINDING_STATES:
        raise ValueError(
            f"invalid finding status {status!r}; must be one of {FINDING_STATES}"
        )
    for audit_id, audit in _iter_audit_files(store, client_id):
        for finding in audit.get("findings", []):
            if isinstance(finding, dict) and finding.get("id") == finding_id:
                finding["status"] = status
                store.write_json(client_id, audit, "audits", f"{audit_id}.json")
                return finding
    raise KeyError(f"finding not found: {finding_id!r}")


def finding_key(finding: dict[str, Any]) -> tuple[str, str]:
    """Return the re-audit matching key for a finding: ``(check, page)``."""
    return (finding.get("check", ""), finding.get("page", ""))
