"""Fix plans for the AAFC business engine.

A fix plan turns an audit's actionable findings (CRITICAL/HIGH/MEDIUM with
result FAIL) into tracked tasks, and moves each finding through the
FINDING_STATES lifecycle one step at a time. Only reaudit.verify_finding may
set the VERIFIED state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import findings as findings_mod
from . import tasks as tasks_mod
from .store import Store

PLAN_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")
MANUAL_REVIEW_NOTE = "Manual review required before implementation."


def _now() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _load_plans(store: Store, client_id: str) -> list[dict[str, Any]]:
    """Read the client's fix plans (empty list when none exist yet)."""
    return store.read_json(client_id, "fixplans.json", default=[])


def create_fix_plan(
    store: Store,
    client_id: str,
    audit_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a fix plan from an audit's actionable findings.

    Creates one task per finding with severity in CRITICAL/HIGH/MEDIUM and
    result FAIL. Findings with confidence NEEDS MANUAL REVIEW are included
    and their task notes read "Manual review required before implementation."
    Each included finding's status is set to RECOMMENDED via
    findings.set_finding_status. Returns the plan dict:

        {id, client_id, audit_id, project_id, created_at, updated_at,
         items: [{finding_id, task_id, state}]}

    Idempotent per audit: re-running for the same audit skips findings an
    earlier plan already covers; when nothing new remains, the existing plan
    is returned unchanged (no duplicate tasks).

    Raises ValueError when the audit or the (given) project does not exist.
    """
    audit = store.read_json(client_id, "audits", f"{audit_id}.json")
    if audit is None:
        raise ValueError(f"audit not found: {audit_id}")
    if project_id is not None:
        from aafc_engine.projects import get_project

        try:
            get_project(store, client_id, project_id)
        except KeyError as exc:
            raise ValueError(f"project not found: {project_id}") from exc

    # Idempotency: findings already covered by an earlier plan for this
    # audit are skipped, so re-running the command never doubles the task
    # list. If every actionable finding is already covered, the existing
    # plan is returned unchanged.
    covered: set[str] = set()
    prior_plan: dict[str, Any] | None = None
    for existing in _load_plans(store, client_id):
        if existing.get("audit_id") == audit_id:
            prior_plan = existing
            for item in existing.get("items", []):
                finding_id = item.get("finding_id")
                if finding_id:
                    covered.add(finding_id)

    items: list[dict[str, Any]] = []
    for finding in audit.get("findings", []):
        if finding.get("severity") not in PLAN_SEVERITIES:
            continue
        if finding.get("result") != "FAIL":
            continue
        finding_id = finding.get("id")
        if finding_id in covered:
            continue
        notes = (
            MANUAL_REVIEW_NOTE
            if finding.get("confidence") == "NEEDS MANUAL REVIEW"
            else ""
        )
        title = f"[{finding.get('severity')}] {finding.get('check')}: {finding.get('issue')}"
        task = tasks_mod.create_task(
            store,
            client_id,
            project_id,
            title,
            finding_id=finding.get("id"),
            notes=notes,
        )
        findings_mod.set_finding_status(
            store, client_id, finding.get("id"), "RECOMMENDED"
        )
        items.append(
            {"finding_id": finding.get("id"), "task_id": task["id"], "state": "RECOMMENDED"}
        )

    now = _now()
    if not items and prior_plan is not None:
        # Nothing new to plan: return the existing plan unchanged.
        return prior_plan
    plan = {
        "id": store.new_id("fix_"),
        "client_id": client_id,
        "audit_id": audit_id,
        "project_id": project_id,
        "created_at": now,
        "updated_at": now,
        "items": items,
    }
    store.append_json_list(client_id, plan, "fixplans.json")
    return plan


def get_fix_plan(store: Store, client_id: str, plan_id: str) -> dict[str, Any]:
    """Return one fix plan by id. Raises KeyError when it does not exist."""
    for plan in _load_plans(store, client_id):
        if plan.get("id") == plan_id:
            return plan
    raise KeyError(f"fix plan not found: {plan_id}")


def advance_finding(
    store: Store, client_id: str, finding_id: str, to_state: str, note: str = ""
) -> dict[str, Any]:
    """Move a finding exactly one step forward or backward along FINDING_STATES.

    Rules enforced:
    - to_state == "VERIFIED" is rejected with ValueError; only
      reaudit.verify_finding may set VERIFIED.
    - to_state must be a valid FINDING_STATES entry and exactly one step away
      from the finding's current state (no skipping, no staying put).
    - Moving to IMPLEMENTED requires a non-empty note describing what was done.

    When the finding belongs to one or more fix plans, the matching plan
    item(s) are updated to the new state (and carry the note, when given).
    Returns the updated finding dict. Raises KeyError when the finding does
    not exist.
    """
    states = findings_mod.FINDING_STATES
    if to_state == "VERIFIED":
        raise ValueError(
            "cannot advance a finding to VERIFIED here: "
            "only reaudit.verify_finding may set VERIFIED"
        )
    if to_state not in states:
        raise ValueError(f"invalid finding state: {to_state!r}; must be one of {states}")
    finding = findings_mod.get_finding(store, client_id, finding_id)
    current = finding.get("status")
    if current not in states:
        raise ValueError(f"finding {finding_id} has unknown status {current!r}")
    if abs(states.index(to_state) - states.index(current)) != 1:
        raise ValueError(
            f"cannot move finding from {current} to {to_state}: "
            f"must advance exactly one step along {states}"
        )
    if to_state == "IMPLEMENTED" and not (note or "").strip():
        raise ValueError(
            "moving a finding to IMPLEMENTED requires a non-empty note "
            "describing what was done"
        )
    updated = findings_mod.set_finding_status(store, client_id, finding_id, to_state)
    _sync_plan_items(store, client_id, finding_id, to_state, note)
    return updated


def _sync_plan_items(
    store: Store, client_id: str, finding_id: str, to_state: str, note: str
) -> None:
    """Update plan items referencing a finding to the finding's new state."""
    plans = _load_plans(store, client_id)
    changed = False
    for plan in plans:
        for item in plan.get("items", []):
            if item.get("finding_id") == finding_id:
                item["state"] = to_state
                if (note or "").strip():
                    item["note"] = note.strip()
                plan["updated_at"] = _now()
                changed = True
    if changed:
        store.write_json(client_id, plans, "fixplans.json")


# Concrete implementation steps per check name. Steps are ordered and
# actionable: identify -> fix -> deploy -> re-run the check -> confirm.
_IMPLEMENTATION_PATHS: dict[str, str] = {
    "fetch": (
        "1. Confirm the page URL loads in a browser and with a plain HTTP request. "
        "2. Check DNS resolves and the server/hosting is up. "
        "3. If the site is down, restore hosting or the server process; if the URL moved, "
        "update links and sitemaps to the new address. "
        "4. Re-run the fetch check and confirm the page is reachable."
    ),
    "http_status": (
        "1. Note the requested URL and the status code actually returned. "
        "2. Fix server routing/configuration so the page returns 200 (or the intended redirect). "
        "3. Update or remove internal links that point at the erroring URL. "
        "4. Re-run the http_status check and confirm the expected status."
    ),
    "https": (
        "1. Install or renew a valid TLS certificate for the domain. "
        "2. Force HTTPS with a 301 redirect from every HTTP URL. "
        "3. Update internal links, canonicals, and sitemaps to https:// URLs. "
        "4. Re-run the https check and confirm the certificate is valid and redirects work."
    ),
    "mixed_content": (
        "1. Open the page source and list every http:// subresource (images, scripts, stylesheets). "
        "2. Serve each subresource over HTTPS or rewrite its URL to https://. "
        "3. Redeploy and reload the page; confirm the browser shows no mixed-content warnings. "
        "4. Re-run the mixed_content check."
    ),
    "broken_links": (
        "1. Identify the source page and the dead destination URL from the evidence. "
        "2. Fix the destination URL, point the link at a live replacement, or remove the link "
        "if the target no longer exists. "
        "3. Deploy the change. "
        "4. Re-run the link check and confirm the destination returns 200."
    ),
    "robots_txt": (
        "1. Open /robots.txt and read the Disallow rules. "
        "2. Remove or narrow any rule that blocks pages meant to be indexed; keep admin/login "
        "paths disallowed. "
        "3. Redeploy and fetch /robots.txt again to confirm the corrected rules are live."
    ),
    "sitemap": (
        "1. Generate or regenerate sitemap.xml containing the current canonical URLs (no 404s, "
        "no redirects). "
        "2. Reference the sitemap in robots.txt and submit it in Search Console. "
        "3. Re-fetch sitemap.xml and confirm it parses and lists live pages."
    ),
    "title": (
        "1. Rewrite the <title> tag to a unique, descriptive title of about 50-60 characters "
        "that names the page and the business. "
        "2. Deploy and re-crawl the page; confirm the new title is picked up on every affected page."
    ),
    "meta_description": (
        "1. Write a unique meta description of about 120-160 characters summarizing what the "
        "page offers. "
        "2. Deploy and re-crawl; confirm each page has its own description."
    ),
    "h1": (
        "1. Give the page exactly one H1 that names the page topic and the business. "
        "2. Deploy and re-run the h1 check to confirm a single H1 is present."
    ),
    "heading_order": (
        "1. Restructure headings so H2/H3 sections nest logically under the H1 with no "
        "skipped levels. "
        "2. Deploy and re-run the heading_order check to confirm the hierarchy is clean."
    ),
    "img_alt": (
        "1. Add concise, descriptive alt text to every informative image (empty alt=\"\" for "
        "purely decorative images). "
        "2. Deploy and re-run the img_alt check to confirm coverage."
    ),
    "canonical": (
        "1. Add <link rel=\"canonical\"> with the preferred absolute URL to each affected page. "
        "2. Make duplicate variants redirect to (or canonicalize to) the same preferred URL. "
        "3. Re-run the canonical check and confirm consistency."
    ),
    "open_graph": (
        "1. Add og:title, og:description, og:image (absolute URL), and og:url meta tags. "
        "2. Test the page with a social sharing debugger and fix the preview. "
        "3. Re-run the open_graph check."
    ),
    "structured_data": (
        "1. Add JSON-LD structured data (e.g. Organization or LocalBusiness) that matches the "
        "visible page content. "
        "2. Validate it with a schema validator and fix any errors. "
        "3. Re-run the structured_data check."
    ),
    "cta": (
        "1. Add a clear, visible call-to-action (button or link) near the top of the page that "
        "states the next step (call, book, buy). "
        "2. Link it to the correct contact/booking destination. "
        "3. Confirm it renders and is tappable on desktop and mobile."
    ),
    "forms": (
        "1. Submit the form end-to-end with test data. "
        "2. Confirm validation messages, the success confirmation, and that the submission "
        "reaches the intended inbox or CRM. "
        "3. Fix the form handler/endpoint for anything that failed, redeploy, and re-test "
        "until a submission completes cleanly."
    ),
    "page_weight": (
        "1. Measure the page with a speed testing tool and note the slowest resources. "
        "2. Compress/convert images, minify CSS/JS, enable caching, and lazy-load below-the-fold media. "
        "3. Re-measure and confirm the scores improved; re-run the page_weight check."
    ),
    "ttfb": (
        "1. Measure time-to-first-byte from several locations; rule out DNS and TLS handshake "
        "overhead first. "
        "2. Fix slow server-side work (uncached queries, cold functions), enable caching or a CDN. "
        "3. Re-run the ttfb check and confirm the response starts faster."
    ),
    "viewport": (
        "1. Confirm the viewport meta tag is present and the layout is responsive: no horizontal "
        "scroll, readable text, tap targets at least ~44px. "
        "2. Fix the CSS/layout issues found. "
        "3. Test on a real phone and re-run the viewport check."
    ),
    "trust": (
        "1. Add visible trust signals: business name, phone/email, physical address, customer "
        "testimonials or reviews, and privacy/terms links. "
        "2. Confirm every detail is accurate and up to date. "
        "3. Re-crawl and confirm the signals are present."
    ),
    "content": (
        "1. Rewrite thin or duplicated sections into original, useful copy that answers what the "
        "page is for. "
        "2. Remove filler and duplicated boilerplate. "
        "3. Re-crawl and confirm the content is substantive and unique."
    ),
    "indexability": (
        "1. Confirm the page has no noindex directive and is allowed by robots.txt. "
        "2. Request indexing in Search Console after fixes are deployed. "
        "3. Confirm the page appears in the search index."
    ),
    "hsts": (
        "1. Confirm the site serves over HTTPS, then add (or fix) the Strict-Transport-Security "
        "header with a long max-age; consider includeSubDomains and preload once stable. "
        "2. Deploy and re-run the hsts check to confirm the header is present and valid."
    ),
    "charset": (
        "1. Declare the page encoding once, early in <head> (e.g. <meta charset=\"utf-8\">), "
        "and make sure the server sends a matching Content-Type charset. "
        "2. Deploy and re-run the charset check."
    ),
    "lang": (
        "1. Add the lang attribute to the <html> tag matching the page's actual language "
        "(e.g. lang=\"en\"). "
        "2. Deploy and re-run the lang check."
    ),
    "parse": (
        "1. Open the page source and find the malformed markup flagged in the evidence. "
        "2. Fix the broken tags/structure so the document parses cleanly. "
        "3. Re-run the parse check."
    ),
    "bot_protection": (
        "1. This is a refused measurement, not a site defect: the server blocked automated "
        "fetching (bot protection / rate limit). "
        "2. To get a real measurement, audit from an allowlisted IP, add a crawl allowance "
        "for the auditor's user agent, or run the audit from the operator's machine. "
        "3. Re-run the audit; confirm the pages now measure instead of refusing."
    ),
}

_DEFAULT_IMPLEMENTATION_PATH = (
    "1. Review the finding's evidence on the affected page and determine the root cause. "
    "2. Implement the recommended fix. "
    "3. Deploy the change. "
    "4. Re-run the check and confirm the finding is resolved."
)


def implementation_path_for(finding: dict[str, Any]) -> str:
    """Return concrete implementation steps for a finding's check.

    Keys are the REAL check names emitted by the vendored engine
    (``aafc_engine/auditor/engine.py``): fetch, http_status, https, hsts,
    mixed_content, indexability, title, meta_description, viewport, charset,
    h1, heading_order, img_alt, canonical, open_graph, structured_data, lang,
    forms, cta, page_weight, ttfb, robots_txt, sitemap, broken_links, parse,
    bot_protection. Unknown checks get a generic but actionable path.
    """
    check = (finding or {}).get("check", "")
    return _IMPLEMENTATION_PATHS.get(check, _DEFAULT_IMPLEMENTATION_PATH)
