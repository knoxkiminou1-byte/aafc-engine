"""Audit and verification reports for the AAFC business engine.

generate_audit_report renders a Markdown report from a COMPLETE audit and
persists both the Markdown and a JSON record. A FAILED audit can never
produce a success report: ReportError is raised instead.

generate_verification_report compares two COMPLETE audits via reaudit.compare
and renders a before/after verification report using only recorded evidence.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import reaudit
from .store import Store

EM_DASH = "\u2014"


class ReportError(Exception):
    """Raised when a report cannot be generated (e.g. a FAILED audit)."""


REPORT_DIRNAME = "reports"
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "PASS"]


def _now() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _domain_for(
    store: Store, client_id: str, website_id: str | None, url: str
) -> str:
    """Best-effort domain for a report title, from the website record or URL."""
    if website_id:
        for site in store.read_json(client_id, "websites.json", default=[]):
            if site.get("id") == website_id and site.get("domain"):
                return site["domain"]
    host = urlsplit(url or "").hostname or ""
    return host or url or "unknown site"


def _group_by_severity(
    findings_list: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group findings into CRITICAL -> HIGH -> MEDIUM -> LOW -> PASS buckets."""
    grouped: dict[str, list[dict[str, Any]]] = {sev: [] for sev in SEVERITY_ORDER}
    for finding in findings_list:
        severity = finding.get("severity")
        if severity in grouped:
            grouped[severity].append(finding)
        else:
            grouped["LOW"].append(finding)
    return grouped


def _finding_block(finding: dict[str, Any], number: int) -> list[str]:
    """Render one finding with every recorded field."""
    return [
        f"#### {number}. {finding.get('issue')}",
        "",
        f"- Check: `{finding.get('check')}`",
        f"- Page: {finding.get('page')}",
        f"- Issue: {finding.get('issue')}",
        f"- Evidence: {finding.get('evidence')}",
        f"- Severity: {finding.get('severity')}",
        f"- Confidence: {finding.get('confidence')}",
        f"- Why it matters: {finding.get('why_it_matters')}",
        f"- Recommended fix: {finding.get('recommended_fix')}",
        f"- Verification method: {finding.get('verification_method')}",
        "",
    ]


def _render_audit_markdown(
    domain: str,
    audit: dict[str, Any],
    grouped: dict[str, list[dict[str, Any]]],
    counts: dict[str, int],
    created_at: str,
) -> str:
    """Render the full audit report Markdown with the exact contract headings."""
    lines: list[str] = []
    findings_list = audit.get("findings", [])
    total = sum(counts.values())
    count_line = ", ".join(f"{counts[s]} {s.lower()}" for s in SEVERITY_ORDER)

    lines.append(f"# Website Audit Report {EM_DASH} {domain}")
    lines.append("")
    lines.append(f"Audit ID: `{audit.get('id')}` | Generated: {created_at}")
    lines.append("")

    lines.append("## Score")
    lines.append("")
    lines.append(f"Overall score: **{audit.get('score')}** (grade **{audit.get('grade')}**)")
    lines.append("")
    lines.append(f"Pages crawled: {audit.get('pages_crawled')}")
    lines.append(f"Findings: {total} total {EM_DASH} {count_line}.")
    lines.append("")

    lines.append("## What we checked")
    lines.append("")
    lines.append(
        f"We crawled {audit.get('pages_crawled')} page(s) starting at {audit.get('url')} "
        "and recorded findings from these checks:"
    )
    lines.append("")
    for check in sorted({f.get("check", "?") for f in findings_list}):
        lines.append(f"- `{check}`")
    if not findings_list:
        lines.append("- (no findings recorded)")
    lines.append("")

    lines.append("## What we found")
    lines.append("")
    number = 0
    for severity in SEVERITY_ORDER:
        group = grouped[severity]
        lines.append(f"### {severity} ({len(group)})")
        lines.append("")
        if not group:
            lines.append("No findings at this severity.")
            lines.append("")
            continue
        for finding in group:
            number += 1
            lines.extend(_finding_block(finding, number))

    lines.append("## Where we found it")
    lines.append("")
    by_page: dict[str, list[dict[str, Any]]] = {}
    for finding in findings_list:
        by_page.setdefault(str(finding.get("page")), []).append(finding)
    if not by_page:
        lines.append("No findings recorded, so no affected pages to list.")
        lines.append("")
    for page in sorted(by_page):
        lines.append(f"### {page}")
        lines.append("")
        for finding in by_page[page]:
            lines.append(
                f"- `{finding.get('check')}` ({finding.get('severity')}): {finding.get('issue')}"
            )
        lines.append("")

    lines.append("## Why it matters")
    lines.append("")
    for finding in findings_list:
        why = (finding.get("why_it_matters") or "").strip()
        if why:
            lines.append(
                f"- **{finding.get('check')}** on {finding.get('page')}: {why}"
            )
    if not any((f.get("why_it_matters") or "").strip() for f in findings_list):
        lines.append("No recorded impacts.")
    lines.append("")

    lines.append("## What should change")
    lines.append("")
    for finding in findings_list:
        fix = (finding.get("recommended_fix") or "").strip()
        if fix:
            lines.append(
                f"- **{finding.get('check')}** on {finding.get('page')}: {fix}"
            )
    if not any((f.get("recommended_fix") or "").strip() for f in findings_list):
        lines.append("No recorded recommendations.")
    lines.append("")

    lines.append("## What AAFC can do")
    lines.append("")
    actionable = counts["CRITICAL"] + counts["HIGH"] + counts["MEDIUM"]
    lines.append(
        f"- Implement the recommended fixes above ({actionable} actionable "
        "finding(s) at CRITICAL/HIGH/MEDIUM severity)."
    )
    lines.append(
        "- Confirm every finding marked NEEDS MANUAL REVIEW with you before any "
        "change is made."
    )
    lines.append(
        "- After fixes are deployed, re-run the audit and issue a verification "
        "report comparing before-and-after evidence."
    )
    lines.append("")

    lines.append("## How we will verify the fix")
    lines.append("")
    for finding in findings_list:
        method = (finding.get("verification_method") or "").strip()
        if method:
            lines.append(
                f"- **{finding.get('check')}** on {finding.get('page')}: {method}"
            )
    if not any((f.get("verification_method") or "").strip() for f in findings_list):
        lines.append("No recorded verification methods.")
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- This is a point-in-time automated audit of publicly reachable pages; "
        "it does not test logged-in areas, transactions, or server internals."
    )
    for note in audit.get("notes", []):
        lines.append(f"- {note}")
    if any(f.get("confidence") == "NEEDS MANUAL REVIEW" for f in findings_list):
        lines.append(
            "- Findings with confidence NEEDS MANUAL REVIEW require human "
            "confirmation before implementation."
        )
    lines.append("")
    return "\n".join(lines)


def _write_markdown(
    store: Store, client_id: str, report_id: str, markdown: str, out_dir: str | None
) -> Path:
    """Persist report Markdown atomically; returns the final path."""
    target_dir = Path(out_dir) if out_dir else store.client_dir(client_id) / REPORT_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{report_id}.md"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(markdown, encoding="utf-8")
    os.replace(tmp, path)
    return path


def generate_audit_report(
    store: Store, client_id: str, audit_id: str, out_dir: str | None = None
) -> tuple[dict[str, Any], str]:
    """Generate a Markdown + JSON audit report for a COMPLETE audit.

    Raises ReportError when the audit does not exist or its status is not
    COMPLETE: a FAILED audit must never produce a success report. Findings
    are grouped CRITICAL -> HIGH -> MEDIUM -> LOW -> PASS.

    Saves reports/<report_id>.md (or <out_dir>/<report_id>.md when out_dir is
    given) and reports/<report_id>.json with
    {id, client_id, website_id, audit_id, kind, created_at, score, grade,
    counts, path}. Returns (report_dict, md_path).
    """
    audit = store.read_json(client_id, "audits", f"{audit_id}.json")
    if audit is None:
        raise ReportError(f"audit not found: {audit_id}")
    if audit.get("status") != "COMPLETE":
        raise ReportError(
            f"refusing to generate an audit report for audit {audit_id}: "
            f"status is {audit.get('status')!r}, not COMPLETE. "
            "Failed audits never produce a success report."
        )
    domain = _domain_for(store, client_id, audit.get("website_id"), audit.get("url", ""))
    grouped = _group_by_severity(audit.get("findings", []))
    counts = {severity: len(grouped[severity]) for severity in SEVERITY_ORDER}
    created_at = _now()
    report_id = store.new_id("rep_")

    markdown = _render_audit_markdown(domain, audit, grouped, counts, created_at)
    md_path = _write_markdown(store, client_id, report_id, markdown, out_dir)

    report = {
        "id": report_id,
        "client_id": client_id,
        "website_id": audit.get("website_id"),
        "audit_id": audit_id,
        "kind": "audit",
        "created_at": created_at,
        "score": audit.get("score"),
        "grade": audit.get("grade"),
        "counts": counts,
        "path": str(md_path),
    }
    store.write_json(client_id, report, REPORT_DIRNAME, f"{report_id}.json")
    return report, str(md_path)


def _render_verification_markdown(
    domain: str,
    old_audit: dict[str, Any],
    new_audit: dict[str, Any],
    comparison: dict[str, Any],
    created_at: str,
) -> str:
    """Render the verification report: BEFORE -> AFTER with recorded evidence only."""
    lines: list[str] = []
    lines.append(f"# Verification Report {EM_DASH} {domain}")
    lines.append("")
    lines.append(f"Generated: {created_at}")
    lines.append("")

    lines.append("## Before")
    lines.append("")
    lines.append(f"Audit ID: `{old_audit.get('id')}`")
    lines.append(
        f"Score: **{old_audit.get('score')}** (grade **{old_audit.get('grade')}**) "
        f"across {old_audit.get('pages_crawled')} page(s), "
        f"{len(old_audit.get('findings', []))} finding(s) recorded."
    )
    lines.append("")

    lines.append("## After")
    lines.append("")
    lines.append(f"Audit ID: `{new_audit.get('id')}`")
    lines.append(
        f"Score: **{new_audit.get('score')}** (grade **{new_audit.get('grade')}**) "
        f"across {new_audit.get('pages_crawled')} page(s), "
        f"{len(new_audit.get('findings', []))} finding(s) recorded."
    )
    lines.append("")
    lines.append(f"Comparison: {comparison['summary']}.")
    lines.append("")

    lines.append("## Resolved")
    lines.append("")
    if not comparison["resolved"]:
        lines.append("No previously recorded findings disappeared in the new audit.")
        lines.append("")
    for finding in comparison["resolved"]:
        lines.append(
            f"- `{finding.get('check')}` ({finding.get('severity')}) on {finding.get('page')}: "
            f"{finding.get('issue')} {EM_DASH} evidence was: {finding.get('evidence')}"
        )
    lines.append("")

    lines.append("## Still open")
    lines.append("")
    if not comparison["still_open"]:
        lines.append("No previously recorded findings are still present in the new audit.")
        lines.append("")
    for finding in comparison["still_open"]:
        lines.append(
            f"- `{finding.get('check')}` ({finding.get('severity')}) on {finding.get('page')}: "
            f"{finding.get('issue')} {EM_DASH} current evidence: {finding.get('evidence')}"
        )
    lines.append("")

    lines.append("## New issues")
    lines.append("")
    if not comparison["new"]:
        lines.append("No new findings appeared in the new audit.")
        lines.append("")
    for finding in comparison["new"]:
        lines.append(
            f"- `{finding.get('check')}` ({finding.get('severity')}) on {finding.get('page')}: "
            f"{finding.get('issue')} {EM_DASH} evidence: {finding.get('evidence')}"
        )
    lines.append("")

    lines.append("## How we verified")
    lines.append("")
    lines.append(
        "Each finding was matched between the two audits by its (check, page) key. "
        "A finding counts as resolved only when its key is absent from the new "
        "audit's recorded findings; presence in the new audit means it is still "
        "open. No improvement numbers were estimated or invented."
    )
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- This comparison only reflects what the two automated audits recorded; "
        "fixes applied outside the audited pages are not visible here."
    )
    for note in new_audit.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def generate_verification_report(
    store: Store, client_id: str, old_audit_id: str, new_audit_id: str
) -> tuple[dict[str, Any], str]:
    """Generate a verification report comparing two COMPLETE audits.

    Uses reaudit.compare (which raises ValueError unless both audits are
    COMPLETE). The report shows BEFORE -> AFTER with recorded evidence only
    and never invents improvement numbers. It is saved as another report
    record with kind "verification". Returns (report_dict, md_path).
    """
    comparison = reaudit.compare(store, client_id, old_audit_id, new_audit_id)
    old_audit = store.read_json(client_id, "audits", f"{old_audit_id}.json")
    new_audit = store.read_json(client_id, "audits", f"{new_audit_id}.json")
    domain = _domain_for(
        store, client_id, new_audit.get("website_id"), new_audit.get("url", "")
    )
    created_at = _now()
    report_id = store.new_id("rep_")

    markdown = _render_verification_markdown(
        domain, old_audit, new_audit, comparison, created_at
    )
    md_path = _write_markdown(store, client_id, report_id, markdown, None)

    report = {
        "id": report_id,
        "client_id": client_id,
        "website_id": new_audit.get("website_id"),
        "audit_id": new_audit_id,
        "old_audit_id": old_audit_id,
        "new_audit_id": new_audit_id,
        "kind": "verification",
        "created_at": created_at,
        "score": new_audit.get("score"),
        "grade": new_audit.get("grade"),
        "counts": comparison["counts"],
        "path": str(md_path),
    }
    store.write_json(client_id, report, REPORT_DIRNAME, f"{report_id}.json")
    return report, str(md_path)


def get_report(store: Store, client_id: str, report_id: str) -> dict[str, Any]:
    """Return one report record by id. Raises KeyError when it does not exist."""
    report = store.read_json(client_id, REPORT_DIRNAME, f"{report_id}.json")
    if report is None:
        raise KeyError(f"report not found: {report_id}")
    return report


def list_reports(store: Store, client_id: str) -> list[dict[str, Any]]:
    """Return all report records for a client, oldest first."""
    reports_dir = store.client_dir(client_id) / REPORT_DIRNAME
    if not reports_dir.is_dir():
        return []
    reports = []
    for path in sorted(reports_dir.glob("*.json")):
        record = store.read_json(client_id, REPORT_DIRNAME, path.name)
        if record:
            reports.append(record)
    reports.sort(key=lambda r: r.get("created_at", ""))
    return reports
