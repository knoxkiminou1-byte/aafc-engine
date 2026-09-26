"""Plugin registry for the AAFC website auditor.

Checks are registered by name via :func:`register`; :func:`run_audit`
executes them in registration order against an authorized website, saves
the audit JSON, and returns the audit dict.

Check function signature::

    fn(url: str, ctx: dict) -> list[dict]

where ``ctx = {"max_pages": int, "site": dict | None}``. The built-in
``sitepulse_core`` check runs first and fills ``ctx["site"]`` with the raw
engine result so later checks can reuse it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
import uuid

from aafc_engine import findings
from aafc_engine.auditor import engine
from aafc_engine.websites import get_website, require_authorized

CheckFn = Callable[[str, dict], list[dict]]

CHECKS: dict[str, CheckFn] = {}

CORE_CHECK_NAME = "sitepulse_core"
ENGINE_NAME = "kiminou-website-audit/1.0"


class AuditFailedError(Exception):
    """Raised when the core audit engine itself fails to run."""


def register(name: str) -> Callable[[CheckFn], CheckFn]:
    """Decorator registering a check function under ``name``.

    Checks run in registration order. Raises ``ValueError`` if ``name``
    is already registered.
    """

    def decorator(fn: CheckFn) -> CheckFn:
        if name in CHECKS:
            raise ValueError(f"check already registered: {name!r}")
        CHECKS[name] = fn
        return fn

    return decorator


def list_checks() -> list[str]:
    """Return registered check names in registration order."""
    return list(CHECKS.keys())


def _error_finding(
    name: str,
    url: str,
    exc: BaseException,
    *,
    audit_id: str,
    website_id: str,
    client_id: str,
) -> dict[str, Any]:
    """Build an ERROR finding recording that check ``name`` raised."""
    return {
        "id": "f_" + uuid.uuid4().hex[:12],
        "check": name,
        "page": url,
        "issue": f"Check '{name}' failed to run",
        "evidence": f"{type(exc).__name__}: {exc}",
        "severity": "LOW",
        "confidence": "NEEDS MANUAL REVIEW",
        "why_it_matters": (
            "A check that crashes cannot report its results, so its coverage "
            "is missing from this audit."
        ),
        "recommended_fix": (
            f"Investigate why the '{name}' check raised, fix it, and re-run "
            "the audit."
        ),
        "implementation_path": "",
        "verification_method": (
            f"Re-run the '{name}' check against {url} and confirm "
            "the finding no longer appears."
        ),
        "result": "ERROR",
        "status": "FOUND",
        "audit_id": audit_id,
        "website_id": website_id,
        "client_id": client_id,
    }


@register(CORE_CHECK_NAME)
def _sitepulse_core(url: str, ctx: dict) -> list[dict]:
    """Built-in check: run the vendored engine and convert its findings.

    Stores the raw site dict in ``ctx["site"]`` for other checks to reuse.
    """
    site = engine.audit_site(url, max_pages=ctx["max_pages"])
    ctx["site"] = site
    audit_id = ctx["audit_id"]
    website_id = ctx["website_id"]
    client_id = ctx["client_id"]
    return [
        findings.from_engine_finding(
            raw,
            audit_id=audit_id,
            website_id=website_id,
            client_id=client_id,
        )
        for raw in site.get("findings", [])
    ]


def run_audit(store: Any, client_id: str, website_id: str, max_pages: int = 6) -> dict:
    """Run all registered checks against a website and save the audit.

    Loads the website via ``aafc_engine.websites`` and enforces
    authorization with ``require_authorized`` (raises ``NotAuthorizedError``
    when the website is not authorized -- the audit never runs without it).

    One bad check never kills the audit: a check that raises is recorded as
    an ERROR finding and the rest continue. Exception: if the built-in
    ``sitepulse_core`` check itself fails, the audit cannot produce its core
    data and :class:`AuditFailedError` is raised.

    The audit ``status`` is ``"FAILED"`` when zero pages could be crawled
    (never fake success), else ``"COMPLETE"``. The audit JSON is saved to
    ``audits/<audit_id>.json`` via ``store.write_json`` and also returned.
    """
    website = get_website(store, client_id, website_id)
    require_authorized(website)  # raises NotAuthorizedError when not authorized

    url = website["url"]
    version = len(list_audits(store, client_id, website_id)) + 1
    audit_id = store.new_id("aud_")
    started_at = datetime.now(timezone.utc).isoformat()

    ctx: dict = {
        "max_pages": max_pages,
        "site": None,
        "audit_id": audit_id,
        "website_id": website_id,
        "client_id": client_id,
    }

    all_findings: list[dict] = []
    notes: list[str] = []
    for name in list_checks():
        try:
            results = CHECKS[name](url, ctx)
        except Exception as exc:  # one bad check must not kill the audit
            if name == CORE_CHECK_NAME:
                raise AuditFailedError(
                    f"core audit engine failed for {url}: {exc}"
                ) from exc
            all_findings.append(
                _error_finding(
                    name,
                    url,
                    exc,
                    audit_id=audit_id,
                    website_id=website_id,
                    client_id=client_id,
                )
            )
            notes.append(
                f"Check '{name}' raised {type(exc).__name__} and was recorded "
                "as an ERROR finding; its coverage is missing from this audit."
            )
            continue
        if results:
            all_findings.extend(results)

    site = ctx["site"] or {}
    pages_crawled = int(site.get("pages_crawled", 0))
    status = "FAILED" if pages_crawled == 0 else "COMPLETE"
    counts = site.get("counts", {"critical": 0, "warning": 0, "info": 0})
    notes = list(site.get("notes", [])) + notes

    audit = {
        "id": audit_id,
        "client_id": client_id,
        "website_id": website_id,
        "url": url,
        "version": version,
        "started_at": started_at,
        "status": status,
        "engine": ENGINE_NAME,
        "score": site.get("score", 0),
        "grade": site.get("grade", "F"),
        "pages_crawled": pages_crawled,
        "counts": counts,
        "findings": all_findings,
        "elapsed_total": site.get("elapsed_total", 0),
        "notes": notes,
    }
    store.write_json(client_id, audit, "audits", f"{audit_id}.json")
    return audit


def get_audit(store: Any, client_id: str, audit_id: str) -> dict:
    """Return a saved audit dict.

    Raises:
        KeyError: if no audit with that id exists for the client.
    """
    audit = store.read_json(client_id, "audits", f"{audit_id}.json", default=None)
    if audit is None:
        raise KeyError(f"audit not found: {audit_id!r}")
    return audit


def list_audits(
    store: Any, client_id: str, website_id: str | None = None
) -> list[dict]:
    """List a client's saved audits, optionally filtered by website.

    Returned in ascending ``version`` order.
    """
    audits_dir = store.client_dir(client_id) / "audits"
    results: list[dict] = []
    if audits_dir.is_dir():
        for path in sorted(audits_dir.glob("*.json")):
            audit = store.read_json(client_id, "audits", path.name, default=None)
            if not isinstance(audit, dict):
                continue
            if website_id is not None and audit.get("website_id") != website_id:
                continue
            results.append(audit)
    results.sort(key=lambda a: (a.get("version", 0), a.get("started_at", "")))
    return results
