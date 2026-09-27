"""AAFC command-line interface (prog ``aafc``).

Every subcommand maps onto the aafc_engine modules defined in
INTERFACE.md. Global flags:

* ``--data DIR`` — store root (defaults to the Store default:
  ``AAFC_DATA_DIR`` env var, else ``<repo>/data``)
* ``--json`` — machine-readable output (the returned dict as JSON)

On success the human-readable summary prints to stdout (or JSON with
``--json``). On error, ``ERROR: <message>`` prints to stderr and the
process exits with code 1.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from aafc_engine import (
    clients,
    crosscheck,
    customers,
    deadlines,
    fixplans,
    footprint,
    money,
    opportunities,
    pipeline,
    projects,
    reaudit,
    reports,
    social_audit,
    tasks,
    templates,
    websites,
)
from aafc_engine.auditor import registry as audit_registry
from aafc_engine.delivery import GmailDelivery, SwiftSendDelivery, log_delivery
from aafc_engine.store import Store

# Handler signature: (parsed args, store) -> (result dict, human summary).
Handler = Callable[[argparse.Namespace, Store], "tuple[dict, str]"]

DEFAULT_SUBJECTS = {
    "audit_received": "We've received your website audit request",
    "audit_complete": "Your website audit is complete",
    "report_ready": "Your website audit report is ready",
    "fix_plan_ready": "Your website fix plan is ready",
    "project_ready": "Your project proposal is ready",
    "fixes_completed": "Website fixes completed",
    "reaudit_complete": "Website re-audit complete",
}


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def cmd_client_add(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Add a client (dedups by email)."""
    client, created = clients.create_client(
        store,
        args.name,
        args.email,
        business_name=args.business,
        phone=args.phone,
        notes=args.notes or "",
        source=args.source,
    )
    status = "created" if created else "already exists (deduped by email)"
    return {"client": client, "created": created}, (
        f"Client {status}: {client['name']} <{client['email']}> [{client['id']}]"
    )


def cmd_client_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List all clients."""
    items = clients.list_clients(store)
    lines = [f"  - {c['name']} <{c['email']}> [{c['id']}] ({c.get('record_type')})"
             for c in items]
    return {"clients": items}, (
        f"{len(items)} client(s):\n" + "\n".join(lines) if lines else "No clients yet."
    )


def cmd_client_show(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Show one client."""
    client = clients.get_client(store, args.id)
    return {"client": client}, (
        f"{client['name']} <{client['email']}> [{client['id']}] "
        f"({client.get('record_type')})"
    )


def cmd_client_promote(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Promote a LEAD to CLIENT (idempotent)."""
    client = clients.promote_to_client(store, args.id)
    return {"client": client}, (
        f"Promoted to {client.get('record_type')}: {client['name']} [{client['id']}]"
    )


# ---------------------------------------------------------------------------
# websites
# ---------------------------------------------------------------------------

def cmd_website_add(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Add a website to a client (dedups by normalized URL)."""
    site, created = websites.add_website(
        store,
        args.client,
        args.url,
        label=args.label,
        authorized=bool(args.authorize),
    )
    status = "created" if created else "already exists (deduped by URL)"
    return {"website": site, "created": created}, (
        f"Website {status}: {site['url']} [{site['id']}] "
        f"(authorized={site['authorized']})"
    )


def cmd_website_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List a client's websites."""
    items = websites.list_websites(store, args.client)
    lines = [f"  - {w['url']} [{w['id']}] (authorized={w['authorized']})"
             for w in items]
    return {"websites": items}, (
        f"{len(items)} website(s):\n" + "\n".join(lines) if lines else "No websites yet."
    )


def cmd_website_authorize(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Mark a website as authorized for auditing."""
    site = websites.set_authorized(store, args.client, args.website, True)
    return {"website": site}, f"Authorized: {site['url']} [{site['id']}]"


# ---------------------------------------------------------------------------
# audits
# ---------------------------------------------------------------------------

def cmd_audit_run(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Run the audit on an authorized website."""
    audit = audit_registry.run_audit(
        store, args.client, args.website, max_pages=args.pages,
        render=args.render,
    )
    return {"audit": audit}, (
        f"Audit {audit['id']}: status={audit['status']} "
        f"score={audit.get('score')} grade={audit.get('grade')} "
        f"pages={audit.get('pages_crawled')} "
        f"render_mode={audit.get('render', {}).get('mode', 'unknown')}"
    )


def cmd_audit_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List a client's audits (optionally filtered by website)."""
    items = audit_registry.list_audits(store, args.client, website_id=args.website)
    lines = [
        f"  - {a['id']} website={a.get('website_id')} status={a.get('status')} "
        f"score={a.get('score')} grade={a.get('grade')}"
        for a in items
    ]
    return {"audits": items}, (
        f"{len(items)} audit(s):\n" + "\n".join(lines) if lines else "No audits yet."
    )


def cmd_audit_show(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Show one audit."""
    audit = audit_registry.get_audit(store, args.client, args.audit)
    return {"audit": audit}, (
        f"Audit {audit['id']}: status={audit.get('status')} "
        f"score={audit.get('score')} grade={audit.get('grade')} "
        f"findings={len(audit.get('findings', []))}"
    )


def cmd_checks_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List registered audit checks."""
    names = audit_registry.list_checks()
    return {"checks": names}, (
        "Checks:\n" + "\n".join(f"  - {n}" for n in names)
        if names else "No checks registered."
    )


def cmd_checks_capabilities(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Report engine capabilities: renderer availability, check count, runtime deps.

    Documents the standing rule: no AI / Muse API is used at runtime — the
    engine is deterministic checks plus its own headless Chromium.
    """
    from aafc_engine.auditor import render as render_provider
    render_ok = render_provider.render_available()
    names = audit_registry.list_checks()
    caps = {
        "render_available": render_ok,
        "renderer": "playwright+chromium (local headless)" if render_ok else "none — HTTP-only mode",
        "checks_registered": len(names),
        "ai_runtime": False,
        "runtime_note": "Deterministic checks + own headless Chromium only. No AI/Muse API at runtime.",
    }
    return {"capabilities": caps}, (
        "Engine capabilities:\n"
        f"  - Renderer: {caps['renderer']}\n"
        f"  - Checks registered: {caps['checks_registered']}\n"
        f"  - AI at runtime: no ({caps['runtime_note']})"
    )


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def cmd_report_generate(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Generate the audit report markdown + JSON."""
    report, md_path = reports.generate_audit_report(
        store, args.client, args.audit, out_dir=args.out
    )
    return {"report": report, "path": md_path}, (
        f"Report {report['id']} written to {md_path} "
        f"(score={report.get('score')} grade={report.get('grade')})"
    )


def cmd_report_verify(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Generate the before/after verification report."""
    report, md_path = reports.generate_verification_report(
        store, args.client, args.old, args.new
    )
    if args.out:
        shutil.copyfile(md_path, args.out)
        md_path = args.out
    return {"report": report, "path": md_path}, (
        f"Verification report {report['id']} written to {md_path}"
    )


# ---------------------------------------------------------------------------
# fix plans
# ---------------------------------------------------------------------------

def cmd_fixplan_create(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Create a fix plan (one task per failing finding)."""
    plan = fixplans.create_fix_plan(
        store, args.client, args.audit, project_id=args.project
    )
    return {"fix_plan": plan}, (
        f"Fix plan {plan['id']} created with {len(plan.get('items', []))} item(s)"
    )


def cmd_fixplan_advance(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Advance one finding along the finding lifecycle (one step)."""
    finding = fixplans.advance_finding(
        store, args.client, args.finding, args.to, note=args.note or ""
    )
    return {"finding": finding}, (
        f"Finding {finding['id']} -> {finding.get('status')}"
    )


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def cmd_project_create(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Create a project."""
    project = projects.create_project(
        store,
        args.client,
        args.name,
        website_id=args.website,
        audit_id=args.audit,
        stage=args.stage or "PROPOSAL",
    )
    return {"project": project}, (
        f"Project created: {project['name']} [{project['id']}] "
        f"(stage={project.get('stage')})"
    )


def cmd_project_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List a client's projects."""
    items = projects.list_projects(store, args.client)
    lines = [f"  - {p['name']} [{p['id']}] (stage={p.get('stage')})" for p in items]
    return {"projects": items}, (
        f"{len(items)} project(s):\n" + "\n".join(lines) if lines else "No projects yet."
    )


def cmd_project_stage(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Move a project to a new stage."""
    project = projects.set_project_stage(store, args.client, args.project, args.to)
    return {"project": project}, (
        f"Project {project['id']} -> {project.get('stage')}"
    )


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def cmd_task_add(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Add a task."""
    task = tasks.create_task(
        store,
        args.client,
        args.project,
        args.title,
        finding_id=args.finding,
        notes=args.notes or "",
    )
    return {"task": task}, f"Task created: {task['title']} [{task['id']}]"


def cmd_task_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List a client's tasks (optionally filtered by project)."""
    items = tasks.list_tasks(store, args.client, project_id=args.project)
    lines = [f"  - {t['title']} [{t['id']}] (status={t.get('status')})" for t in items]
    return {"tasks": items}, (
        f"{len(items)} task(s):\n" + "\n".join(lines) if lines else "No tasks yet."
    )


def cmd_task_complete(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Mark a task DONE."""
    task = tasks.complete_task(store, args.client, args.task, note=args.note or "")
    return {"task": task}, f"Task {task['id']} -> DONE"


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

def cmd_money_record(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Record a money event (receivables require evidence)."""
    event = money.record_event(
        store,
        args.client,
        kind=args.kind,
        amount_dollars=float(args.amount),
        status=args.status,
        evidence=args.evidence,
        project_id=args.project,
        due_date=args.due,
        notes=args.notes or "",
    )
    dollars = event.get("amount_cents", 0) / 100
    return {"event": event}, (
        f"Money event {event['id']}: {event.get('kind')} "
        f"${dollars:.2f} [{event.get('status')}]"
    )


def cmd_money_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List a client's money events."""
    items = money.list_events(store, args.client)
    lines = [
        f"  - {e['id']} {e.get('kind')} ${e.get('amount_cents', 0) / 100:.2f} "
        f"[{e.get('status')}]"
        for e in items
    ]
    return {"events": items}, (
        f"{len(items)} event(s):\n" + "\n".join(lines) if lines else "No money events yet."
    )


def cmd_money_outstanding(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Show the client's outstanding balance."""
    cents = money.outstanding_cents(store, args.client)
    return (
        {"outstanding_cents": cents, "outstanding_dollars": cents / 100},
        f"Outstanding: ${cents / 100:.2f}",
    )


def cmd_money_set_status(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Move a money event to a new status."""
    event = money.set_event_status(
        store, args.client, args.event, args.to,
        evidence=args.evidence, amount_paid_dollars=args.paid,
    )
    return {"event": event}, f"Money event {event['id']} -> {event.get('status')}"


# ---------------------------------------------------------------------------
# deadlines
# ---------------------------------------------------------------------------

def cmd_deadline_add(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Add a deadline (YYYY-MM-DD)."""
    deadline = deadlines.add_deadline(
        store, args.client, args.title, args.due, notes=args.notes or ""
    )
    return {"deadline": deadline}, (
        f"Deadline added: {deadline['title']} due {deadline.get('due_date')} "
        f"[{deadline['id']}]"
    )


def cmd_deadline_list(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """List deadlines, open ones first unless --all."""
    items = deadlines.list_deadlines(store, args.client, include_done=args.all)
    lines = [
        f"  - {d['title']} due {d.get('due_date')} "
        f"[{'done' if d.get('done') else 'open'}] [{d['id']}]"
        for d in items
    ]
    return {"deadlines": items}, (
        f"{len(items)} deadline(s):\n" + "\n".join(lines)
        if lines else "No deadlines."
    )


def cmd_deadline_done(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Mark a deadline done."""
    deadline = deadlines.mark_done(store, args.client, args.deadline)
    return {"deadline": deadline}, f"Deadline {deadline['id']} marked done"


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def cmd_pipeline_show(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Show the client's pipeline stage."""
    pipe = pipeline.get_pipeline(store, args.client)
    return {"pipeline": pipe}, f"Pipeline stage: {pipe.get('stage')}"


def cmd_pipeline_advance(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Advance (or move back) the pipeline stage."""
    pipe = pipeline.advance(store, args.client, args.to, note=args.note or "")
    return {"pipeline": pipe}, f"Pipeline stage: {pipe.get('stage')}"


# ---------------------------------------------------------------------------
# reaudit
# ---------------------------------------------------------------------------

def cmd_reaudit_compare(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Compare two audits: resolved / still open / new findings."""
    result = reaudit.compare(store, args.client, args.old, args.new)
    counts = result.get("counts", {})
    return {"comparison": result}, (
        f"Resolved: {counts.get('resolved', 0)}, "
        f"still open: {counts.get('still_open', 0)}, "
        f"new: {counts.get('new', 0)}"
    )


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------

def _parse_vars(pairs: list[str] | None) -> dict:
    """Parse repeatable ``--var k=v`` flags into a dict."""
    variables: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--var must be k=v, got {pair!r}")
        key, value = pair.split("=", 1)
        variables[key.strip()] = value
    return variables


def cmd_deliver(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Render a template and deliver it.

    Default is a DRY RUN (nothing is sent); pass ``--send`` for a real
    send (gmail only — swiftsend always raises). Every delivery is logged
    via ``log_delivery``.
    """
    variables = _parse_vars(args.var)
    body = templates.render(args.template, variables)
    subject = args.subject or DEFAULT_SUBJECTS.get(args.template, "Message from AAFC")
    provider = GmailDelivery() if args.provider == "gmail" else SwiftSendDelivery()
    dry_run = not args.send
    record = provider.send(args.to, subject, body, dry_run=dry_run)
    logged = log_delivery(store, args.client, record)
    mode = "DRY RUN (not sent)" if dry_run else "SENT"
    summary = (
        f"{mode}: {provider.name} -> {record['to']} | subject: {subject} | "
        "logged to delivery_log.json"
    )
    return {"delivery": record, "logged": logged, "dry_run": dry_run}, summary


# ---------------------------------------------------------------------------
# extension layer: customers / footprint / full audit / opportunities
# ---------------------------------------------------------------------------

def _client_by_email(store: Store, email: str) -> dict:
    """Resolve a client record by email or raise ClientNotFoundError."""
    client = clients.find_client_by_email(store, email)
    if client is None:
        raise clients.ClientNotFoundError(
            f"no customer found for email {email.strip()!r}"
        )
    return client


def _primary_website(store: Store, client_id: str,
                     website_id: str | None = None) -> dict:
    """Return the given website, or the client's first (primary) website."""
    if website_id:
        return websites.get_website(store, client_id, website_id)
    sites = websites.list_websites(store, client_id)
    if not sites:
        raise websites.WebsiteError(
            "no websites on file for this customer; add one first"
        )
    return sites[0]


def cmd_customer_resolve(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Resolve a customer by email: FOUND or NEW."""
    result = customers.resolve_customer(store, args.email)
    if result["resolution"] == "FOUND":
        cabinet = result["customer"]
        identity = cabinet["identity"]
        summary = (
            f"FOUND: {identity['name']} <{identity['email']}> "
            f"business={cabinet['business'].get('business_name') or 'UNKNOWN'} "
            f"source={cabinet['source'].get('source')} "
            f"websites={len(cabinet['websites'])} "
            f"audits={len(cabinet['audits'])} "
            f"opportunities={len(cabinet['opportunities'])}"
        )
    else:
        summary = (
            f"NEW: no customer with {result['email']} yet. Minimally needed "
            "to register: name and website "
            "(`aafc customer register --email ... --name ...`)."
        )
    return result, summary


def cmd_customer_register(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Register a customer (dedups by email)."""
    cabinet = customers.register_customer(
        store,
        args.name,
        args.email,
        business_name=args.business,
        phone=args.phone,
        source=args.source or "UNKNOWN",
        notes=args.notes or "",
    )
    identity = cabinet["identity"]
    return {"customer": cabinet}, (
        f"Registered: {identity['name']} <{identity['email']}> "
        f"source={cabinet['source'].get('source')}"
    )


def cmd_customer_file(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Print a readable filing-cabinet summary (counts per section)."""
    cabinet = customers.get_customer_file(store, args.email)
    identity = cabinet["identity"]
    counts = {
        "websites": len(cabinet["websites"]),
        "social_profiles": len(cabinet["social_profiles"]),
        "offerings": len(cabinet["offerings"]),
        "communications": len(cabinet["communications"]),
        "audits": len(cabinet["audits"]),
        "findings": len(cabinet["findings"]),
        "opportunities": len(cabinet["opportunities"]),
        "proposals": len(cabinet["proposals"]),
        "projects": len(cabinet["projects"]),
        "deliverables": len(cabinet["deliverables"]),
        "money": len(cabinet["money"]),
        "history": len(cabinet["history"]),
    }
    lines = [
        f"Customer file: {identity['name']} <{identity['email']}>",
        f"  business: {cabinet['business'].get('business_name') or 'UNKNOWN'}",
        f"  source: {cabinet['source'].get('source')}",
    ]
    lines.extend(f"  {section}: {count}" for section, count in counts.items())
    return {"customer": cabinet}, "\n".join(lines)


def cmd_footprint(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Discover the customer's public social footprint from their homepage."""
    client = _client_by_email(store, args.email)
    site = _primary_website(store, client["id"], args.website)
    profiles = footprint.discover(store, client["id"], site["id"])
    lines = [
        f"{p['platform']:<16} {p['status']:<18} {p['confidence']:<10} "
        f"{(p['evidence'] or '')[:90]}"
        for p in profiles
    ]
    verified = sum(1 for p in profiles if p["status"] == "PUBLICLY VERIFIED")
    summary = (
        f"Footprint for {site['url']}: {verified}/{len(profiles)} platforms "
        "publicly verified\n" + "\n".join(lines)
    )
    return {"profiles": profiles, "website_id": site["id"]}, summary


def cmd_audit_full(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Full audit: website audit + social audit + crosscheck."""
    client = _client_by_email(store, args.email)
    site = _primary_website(store, client["id"], args.website)
    # Refuse gracefully when the website is not authorized: NotAuthorizedError
    # becomes "ERROR: ..." + exit 1 via main().
    websites.require_authorized(site)
    audit = audit_registry.run_audit(
        store, client["id"], site["id"], max_pages=args.pages
    )
    social_findings = social_audit.audit_social(store, client["id"])
    xcheck_findings = crosscheck.crosscheck(store, client["id"])
    summary = (
        f"Full audit for {site['url']}:\n"
        f"  website audit {audit['id']}: status={audit['status']} "
        f"score={audit.get('score')} grade={audit.get('grade')} "
        f"findings={len(audit.get('findings', []))}\n"
        f"  social findings: {len(social_findings)}\n"
        f"  crosscheck findings: {len(xcheck_findings)}"
    )
    return {
        "audit": audit,
        "social_findings": social_findings,
        "crosscheck_findings": xcheck_findings,
    }, summary


def cmd_opportunities(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Build the evidence-gated opportunity map and print it."""
    client = _client_by_email(store, args.email)
    built = opportunities.build_opportunity_map(
        store, client["id"], current_offering=args.current_offering
    )
    opps = built["opportunities"]
    lines = []
    for opp in opps:
        evidence_snippet = (opp.get("evidence") or "")[:160]
        lines.append(
            f"  - {opp['service']} [{opp['status']}] "
            f"pricing={opp.get('pricing_model')}\n"
            f"    problem: {opp.get('problem')}\n"
            f"    evidence: {evidence_snippet}"
        )
    summary = (
        f"{len(opps)} opportunitie(s) "
        f"(current_offering={args.current_offering}):\n" + "\n".join(lines)
        if lines else
        f"No evidence-supported opportunities (current_offering={args.current_offering})."
    )
    return {"opportunities": opps, "current_offering": args.current_offering}, summary


def _render_opportunity_map_md(store: Store, client: dict, site: dict | None,
                               built: dict, expansions: dict) -> str:
    """Render the opportunity map as Markdown."""
    from aafc_engine import services as _services

    identity_name = client.get("name")
    identity_email = client.get("email")
    business = client.get("business_name") or "UNKNOWN"
    website_text = site["url"] if site else "none on file"
    date_text = datetime.now(timezone.utc).isoformat()
    current = built.get("current_offering") or "none recorded"

    md: list[str] = [
        f"# Opportunity Map — {identity_name}",
        "",
        f"- Customer: {identity_name} <{identity_email}>",
        f"- Business: {business}",
        f"- Website: {website_text}",
        f"- Built: {date_text}",
        "",
        "## Current offering",
        "",
        current,
        "",
        "## Opportunities (evidence-supported only)",
        "",
    ]
    opps = built.get("opportunities", [])
    if not opps:
        md.append("None. No catalog service is evidence-supported right now.")
        md.append("")
    for opp in opps:
        md.extend([
            f"### {opp['additional_offering']} (`{opp['service']}`)",
            "",
            f"- Problem: {opp.get('problem')}",
            f"- Evidence: {opp.get('evidence')}",
            f"- Additional offering: {opp.get('additional_offering')}",
            f"- Why it fits: {opp.get('why_it_fits')}",
            f"- AAFC capability: {opp.get('aafc_capability')}",
            f"- Implementation path: {opp.get('implementation_path')}",
            f"- Pricing: {opp.get('pricing_model')} "
            f"({'one-time' if opp.get('pricing_model') == 'one-time' else 'recurring'})",
            f"- Status: {opp.get('status')}",
            "",
        ])
    md.extend([
        "## Expansions not evidence-supported",
        "",
        "Services with no triggering evidence are NOT recommended — "
        "no padding.",
        "",
    ])
    supported_slugs = {o.get("service") for o in opps}
    unsupported = [
        svc for svc in _services.list_services()
        if svc.get("slug") not in supported_slugs
    ]
    if not unsupported:
        md.append("none — no padded items")
    else:
        for svc in unsupported:
            md.append(
                f"- {svc.get('name')} (`{svc.get('slug')}`): "
                "no triggering findings — not recommended"
            )
    md.append("")
    return "\n".join(md)


def cmd_opportunity_map(args: argparse.Namespace, store: Store) -> tuple[dict, str]:
    """Build the opportunity map and write it as a Markdown report."""
    client = _client_by_email(store, args.email)
    built = opportunities.build_opportunity_map(
        store, client["id"], current_offering=args.current_offering
    )
    expansions = opportunities.expand_offerings(
        store, client["id"], args.current_offering or ""
    )
    sites = websites.list_websites(store, client["id"])
    site = sites[0] if sites else None
    md = _render_opportunity_map_md(store, client, site, built, expansions)
    filename = datetime.now(timezone.utc).strftime(
        "opportunity-map-%Y%m%d-%H%M%S.md"
    )
    path = store.client_dir(client["id"]) / filename
    path.write_text(md, encoding="utf-8")
    return {"path": str(path), "opportunities": built["opportunities"]}, (
        f"Opportunity map ({len(built['opportunities'])} opportunitie(s)) "
        f"written to {path}"
    )


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the full ``aafc`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="aafc", description="AAFC business engine CLI"
    )
    parser.add_argument("--data", metavar="DIR", default=None,
                        help="store root (default: AAFC_DATA_DIR or <repo>/data)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (result dict as JSON)")
    sub = parser.add_subparsers(dest="command", required=True)

    # client
    client_p = sub.add_parser("client", help="manage clients")
    client_sub = client_p.add_subparsers(dest="client_cmd", required=True)
    p = client_sub.add_parser("add", help="add a client")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--business")
    p.add_argument("--phone")
    p.add_argument("--notes")
    p.add_argument("--source")
    p.set_defaults(func=cmd_client_add)
    p = client_sub.add_parser("list", help="list clients")
    p.set_defaults(func=cmd_client_list)
    p = client_sub.add_parser("show", help="show a client")
    p.add_argument("id")
    p.set_defaults(func=cmd_client_show)
    p = client_sub.add_parser("promote", help="promote a lead to client")
    p.add_argument("id")
    p.set_defaults(func=cmd_client_promote)

    # website
    website_p = sub.add_parser("website", help="manage websites")
    website_sub = website_p.add_subparsers(dest="website_cmd", required=True)
    p = website_sub.add_parser("add", help="add a website")
    p.add_argument("--client", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--label")
    p.add_argument("--authorize", action="store_true")
    p.set_defaults(func=cmd_website_add)
    p = website_sub.add_parser("list", help="list a client's websites")
    p.add_argument("--client", required=True)
    p.set_defaults(func=cmd_website_list)
    p = website_sub.add_parser("authorize", help="authorize a website for auditing")
    p.add_argument("--client", required=True)
    p.add_argument("--website", required=True)
    p.set_defaults(func=cmd_website_authorize)

    # audit
    audit_p = sub.add_parser("audit", help="run and inspect audits")
    audit_sub = audit_p.add_subparsers(dest="audit_cmd", required=True)
    p = audit_sub.add_parser("run", help="run the audit")
    p.add_argument("--client", required=True)
    p.add_argument("--website", required=True)
    p.add_argument("--pages", type=int, default=6)
    p.add_argument("--render", dest="render", action="store_true", default=True,
                   help="verify DOM findings with the local headless Chromium (default when available)")
    p.add_argument("--no-render", dest="render", action="store_false",
                   help="force HTTP-only mode (no headless Chromium)")
    p.set_defaults(func=cmd_audit_run)
    p = audit_sub.add_parser("list", help="list audits")
    p.add_argument("--client", required=True)
    p.add_argument("--website")
    p.set_defaults(func=cmd_audit_list)
    p = audit_sub.add_parser("show", help="show an audit")
    p.add_argument("--client", required=True)
    p.add_argument("--audit", required=True)
    p.set_defaults(func=cmd_audit_show)

    # checks
    checks_p = sub.add_parser("checks", help="audit check registry")
    checks_sub = checks_p.add_subparsers(dest="checks_cmd", required=True)
    p = checks_sub.add_parser("list", help="list registered checks")
    p.set_defaults(func=cmd_checks_list)
    p = checks_sub.add_parser("capabilities", help="report engine capabilities (renderer, runtime)")
    p.set_defaults(func=cmd_checks_capabilities)

    # report
    report_p = sub.add_parser("report", help="audit reports")
    report_sub = report_p.add_subparsers(dest="report_cmd", required=True)
    p = report_sub.add_parser("generate", help="generate the audit report")
    p.add_argument("--client", required=True)
    p.add_argument("--audit", required=True)
    p.add_argument("--out", metavar="PATH")
    p.set_defaults(func=cmd_report_generate)
    p = report_sub.add_parser("verify", help="generate the before/after verification report")
    p.add_argument("--client", required=True)
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--out", metavar="PATH")
    p.set_defaults(func=cmd_report_verify)

    # fixplan
    fixplan_p = sub.add_parser("fixplan", help="fix plans and finding lifecycle")
    fixplan_sub = fixplan_p.add_subparsers(dest="fixplan_cmd", required=True)
    p = fixplan_sub.add_parser("create", help="create a fix plan from an audit")
    p.add_argument("--client", required=True)
    p.add_argument("--audit", required=True)
    p.add_argument("--project")
    p.set_defaults(func=cmd_fixplan_create)
    p = fixplan_sub.add_parser("advance", help="advance a finding one lifecycle step")
    p.add_argument("--client", required=True)
    p.add_argument("--finding", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_fixplan_advance)

    # project
    project_p = sub.add_parser("project", help="manage projects")
    project_sub = project_p.add_subparsers(dest="project_cmd", required=True)
    p = project_sub.add_parser("create", help="create a project")
    p.add_argument("--client", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--website")
    p.add_argument("--audit")
    p.add_argument("--stage")
    p.set_defaults(func=cmd_project_create)
    p = project_sub.add_parser("list", help="list a client's projects")
    p.add_argument("--client", required=True)
    p.set_defaults(func=cmd_project_list)
    p = project_sub.add_parser("stage", help="move a project to a stage")
    p.add_argument("--client", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--to", required=True)
    p.set_defaults(func=cmd_project_stage)

    # task
    task_p = sub.add_parser("task", help="manage tasks")
    task_sub = task_p.add_subparsers(dest="task_cmd", required=True)
    p = task_sub.add_parser("add", help="add a task")
    p.add_argument("--client", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--project")
    p.add_argument("--finding")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_task_add)
    p = task_sub.add_parser("list", help="list tasks")
    p.add_argument("--client", required=True)
    p.add_argument("--project")
    p.set_defaults(func=cmd_task_list)
    p = task_sub.add_parser("complete", help="mark a task done")
    p.add_argument("--client", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_task_complete)

    # money
    money_p = sub.add_parser("money", help="money events")
    money_sub = money_p.add_subparsers(dest="money_cmd", required=True)
    p = money_sub.add_parser("record", help="record a money event")
    p.add_argument("--client", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--amount", required=True, help="amount in dollars")
    p.add_argument("--status", required=True)
    p.add_argument("--evidence", required=True,
                   help="required for OWED/EXPECTED/OVERDUE receivables")
    p.add_argument("--project")
    p.add_argument("--due", metavar="YYYY-MM-DD")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_money_record)
    p = money_sub.add_parser("list", help="list money events")
    p.add_argument("--client", required=True)
    p.set_defaults(func=cmd_money_list)
    p = money_sub.add_parser("outstanding", help="show outstanding balance")
    p.add_argument("--client", required=True)
    p.set_defaults(func=cmd_money_outstanding)
    p = money_sub.add_parser("set-status", help="move a money event to a status")
    p.add_argument("--client", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--evidence")
    p.add_argument("--paid", type=float, default=None,
                   help="amount already paid (required when --to PARTIALLY_PAID)")
    p.set_defaults(func=cmd_money_set_status)

    # deadline
    deadline_p = sub.add_parser("deadline", help="manage deadlines")
    deadline_sub = deadline_p.add_subparsers(dest="deadline_cmd", required=True)
    p = deadline_sub.add_parser("add", help="add a deadline")
    p.add_argument("--client", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--due", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_deadline_add)
    p = deadline_sub.add_parser("list", help="list deadlines")
    p.add_argument("--client", required=True)
    p.add_argument("--all", action="store_true", help="include done deadlines")
    p.set_defaults(func=cmd_deadline_list)
    p = deadline_sub.add_parser("done", help="mark a deadline done")
    p.add_argument("--client", required=True)
    p.add_argument("--deadline", required=True)
    p.set_defaults(func=cmd_deadline_done)

    # pipeline
    pipeline_p = sub.add_parser("pipeline", help="sales pipeline")
    pipeline_sub = pipeline_p.add_subparsers(dest="pipeline_cmd", required=True)
    p = pipeline_sub.add_parser("show", help="show the pipeline stage")
    p.add_argument("--client", required=True)
    p.set_defaults(func=cmd_pipeline_show)
    p = pipeline_sub.add_parser("advance", help="move the pipeline stage")
    p.add_argument("--client", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_pipeline_advance)

    # reaudit
    reaudit_p = sub.add_parser("reaudit", help="audit comparison")
    reaudit_sub = reaudit_p.add_subparsers(dest="reaudit_cmd", required=True)
    p = reaudit_sub.add_parser("compare", help="compare two audits")
    p.add_argument("--client", required=True)
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_reaudit_compare)

    # deliver
    deliver_p = sub.add_parser("deliver", help="render a template and deliver it")
    deliver_p.add_argument("--client", required=True)
    deliver_p.add_argument("--to", required=True, metavar="EMAIL")
    deliver_p.add_argument("--template", required=True, choices=templates.TEMPLATES)
    deliver_p.add_argument("--provider", required=True, choices=["gmail", "swiftsend"])
    deliver_p.add_argument("--subject")
    deliver_p.add_argument("--var", action="append", metavar="k=v",
                           help="template variable; repeatable")
    deliver_p.add_argument("--send", action="store_true",
                           help="perform the real send (default is DRY RUN)")
    deliver_p.set_defaults(func=cmd_deliver)

    # customer (extension layer, email-keyed)
    customer_p = sub.add_parser("customer", help="extension-layer customer cabinet")
    customer_sub = customer_p.add_subparsers(dest="customer_cmd", required=True)
    p = customer_sub.add_parser("resolve", help="resolve a customer by email")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_customer_resolve)
    p = customer_sub.add_parser("register", help="register a customer")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--business")
    p.add_argument("--phone")
    p.add_argument("--source")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_customer_register)
    p = customer_sub.add_parser("file", help="show the customer filing cabinet")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_customer_file)

    # footprint
    footprint_p = sub.add_parser(
        "footprint", help="discover the public social footprint")
    footprint_p.add_argument("--email", required=True)
    footprint_p.add_argument("--website", help="website id (default: primary)")
    footprint_p.set_defaults(func=cmd_footprint)

    # audit-full
    audit_full_p = sub.add_parser(
        "audit-full",
        help="full audit: website audit + social audit + crosscheck")
    audit_full_p.add_argument("--email", required=True)
    audit_full_p.add_argument("--website", help="website id (default: primary)")
    audit_full_p.add_argument("--pages", type=int, default=6)
    audit_full_p.set_defaults(func=cmd_audit_full)

    # opportunities
    opp_p = sub.add_parser(
        "opportunities", help="build the evidence-gated opportunity map")
    opp_p.add_argument("--email", required=True)
    opp_p.add_argument("--current-offering")
    opp_p.set_defaults(func=cmd_opportunities)

    # opportunity-map
    opp_map_p = sub.add_parser(
        "opportunity-map",
        help="write the opportunity map as a Markdown report")
    opp_map_p.add_argument("--email", required=True)
    opp_map_p.add_argument("--current-offering")
    opp_map_p.set_defaults(func=cmd_opportunity_map)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.data)
    try:
        result, summary = args.func(args, store)
    except Exception as exc:  # every engine error becomes ERROR: <msg>, exit 1
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
