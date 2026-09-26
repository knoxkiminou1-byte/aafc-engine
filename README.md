# AAFC Engine

**WHAT THIS IS.** A reusable, client-agnostic business engine for AAFC (a web studio):
customer intake → website audit → evidence-backed findings → client report →
opportunity map → proposal/project → implementation → re-audit/verification →
maintenance. One Python package, one CLI, local JSON storage with strict
per-client isolation. No framework, no database server, no auth system — the
smallest architecture that genuinely runs the workflow.

**WHO IT IS FOR.** AAFC operators running website audits as a lead magnet and
turning findings into paid fix/retainer work, and (later) any client instance
that needs the same audit → fix → verify loop.

**HOW IT WORKS.** `aafc_engine/` Python package + `aafc` CLI. Data lives in
`data/clients/<client_id>/` as JSON (git-ignored). The website auditor is the
vendored, already-proven SitePulse engine (`aafc_engine/auditor/engine.py` —
the single audit implementation; do not create a second one). A plugin
registry wraps it so new checks are one function + one registration line.
Findings carry evidence, severity, and confidence; only evidence-backed
services from `data/services.yaml` can become opportunities — the engine
never pads or manufactures problems.

## How to run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./aafc --help                       # or: .venv/bin/python -m aafc_engine.cli --help
```

`--data DIR` overrides the store root (default `AAFC_DATA_DIR` env or `./data`).
`--json` prints machine-readable output. Errors print `ERROR: <msg>` to
stderr and exit 1.

## How to add a client (customer intake)

Email is the customer identifier; duplicates are impossible by construction.

```bash
./aafc customer resolve --email jane@acmebakery.test      # FOUND or NEW
./aafc customer register --email jane@acmebakery.test --name "Jane Doe" \
    --business "Acme Bakery" --source referral
./aafc customer file --email jane@acmebakery.test         # full filing cabinet
./aafc website add --client <id> --url https://acmebakery.test --authorize
```

`--authorize` records that the customer authorized the audit. `audit-full`
refuses to run on unauthorized websites (exit 1) — finding an issue and
having permission to inspect are separate things.

## How to run an audit

```bash
./aafc checks list
./aafc audit run --client <id> --website <id> [--pages 6]
./aafc audit-full --email jane@acmebakery.test   # website + social + cross-channel
./aafc footprint --email jane@acmebakery.test    # public digital-footprint discovery
```

The audit crawls up to N pages (default 6), runs every registered check, and
saves a versioned, immutable audit (`version` 1, 2, 3… per website — old
audits are never overwritten). If zero pages load, the audit status is
`FAILED` and no success report can be generated — failures are reported as
failures, never fake success.

## How findings work

Every finding has: check, page/URL, issue, **evidence**, severity
(`CRITICAL`/`HIGH`/`MEDIUM`/`LOW`/`PASS`), confidence
(`CONFIRMED`/`LIKELY`/`NEEDS MANUAL REVIEW`), why it matters, recommended
fix, implementation path, verification method, and lifecycle status
(`FOUND` → `RECOMMENDED` → `READY_TO_IMPLEMENT` → `IMPLEMENTED` →
`VERIFIED`). `VERIFIED` can only be set by a re-audit that confirms the issue
is gone — never by hand.

Cross-channel findings (`xcheck_*`) relate website vs social vs listings
(e.g. offer on website but invisible on social, inconsistent contact info).

## How reports work

```bash
./aafc report generate --client <id> --audit <audit-id>   # client-facing audit report
./aafc report verify --client <id> --old <a1> --new <a2>   # before/after verification report
./aafc opportunity-map --email jane@acmebakery.test --current-offering website-design-build
```

Reports show what was checked, what was found, where, why it matters, what
should change, what AAFC can do, and how each fix will be verified — with
real evidence only. The opportunity map lists only services whose
`evidence_triggers` matched real findings, each citing the finding ids.

## How fixes are verified

```bash
./aafc fixplan create --client <id> --audit <audit-id> [--project <id>]
./aafc fixplan advance --client <id> --finding <id> --to READY_TO_IMPLEMENT
./aafc fixplan advance --client <id> --finding <id> --to IMPLEMENTED --note "what was done"
# ... do the work, deploy ...
./aafc audit run --client <id> --website <id>     # re-audit -> new version
./aafc reaudit compare --client <id> --old <a1> --new <a2>
```

`reaudit.compare` returns resolved / still-open / new findings from real
evidence. Nothing is called fixed until the re-audit confirms it.

## How money is tracked

Per-client ledger (`money.json`). States: `PAID`, `OWED`, `EXPECTED`,
`PARTIALLY_PAID`, `OVERDUE`, `NEEDS_VERIFICATION`, `CANCELLED`. Kinds:
`PROPOSAL`, `INVOICE`, `DEPOSIT`, `PAYMENT`, `BALANCE`.

**Hard rule:** `OWED`, `EXPECTED`, and `OVERDUE` require non-empty evidence
(a proposal/invoice reference) — the engine raises instead of manufacturing
a receivable from a bare dollar amount. `outstanding` counts only
`OWED` + `OVERDUE` + partial remainders; `EXPECTED` is forecast, not debt.
Recommendations and proposals never touch money.

```bash
./aafc money record --client <id> --kind PROPOSAL --amount 1200 --status EXPECTED \
    --evidence "Proposal PB-2026-09 sent 2026-09-26" --project <id>
./aafc money outstanding --client <id>
```

## How deployment works

There is no hosted deployment. The engine runs locally via the CLI. A
`Dockerfile`/Vercel deployment is **not** built (no backend, DB, or auth
exists to deploy; a multi-tenant SaaS cannot be honestly verified here).
Migration path when needed: replace `Store`'s JSON backend with Postgres
(one module), keeping per-client isolation as row-level scoping. **Do not
deploy anything to production without Amaury's explicit authorization.**

## How testing works

```bash
.venv/bin/python -m pytest tests/ -q            # full suite incl. live audits
.venv/bin/python -m pytest tests/ -q -m "not live"  # offline only
```

45 tests: engine workflow (spec M 1–15), customer journey (1–14), extension
chain (T-A–T-G), CLI smoke tests, and `@pytest.mark.live` real audits of
example.com + iana.org. Live tests use tiny page counts. No test ever sends
a real email (dry-run / stub only); all fixtures are fictitious
(`@example.test`).

## How to add new audit checks

```python
from aafc_engine.auditor.registry import register

@register("my_check")
def my_check(url, ctx):
    # ctx: {"max_pages": int, "site": dict|None} (site set by sitepulse_core)
    return [{
        "check": "my_check", "page": url, "issue": "...",
        "evidence": "...", "severity": "MEDIUM", "confidence": "LIKELY",
        "why_it_matters": "...", "recommended_fix": "...",
        "verification_method": "...", "result": "FAIL",
    }]
```

That's it — one function + the decorator. Use the standard finding schema
(see `INTERFACE.md`); `run_audit` fills in ids and lifecycle state. Add the
check name to `CONFIDENCE_BY_CHECK` in `findings.py` and, if it should drive
sales, to a service's `evidence_triggers` in `data/services.yaml`.

## Email delivery

`delivery/` is a provider interface. `GmailDelivery` is the working default
(dry-run unless `--send`; real sends go through `hatch_gws_cli gmail +send`).
`SwiftSendDelivery` is an explicit **BLOCKED** stub — it raises
`NotImplementedError` naming exactly what's missing (API base URL, auth
scheme, endpoint contract). Seven `{{variable}}` templates live in
`templates/` (`audit_received`, `audit_complete`, `report_ready`,
`fix_plan_ready`, `project_ready`, `fixes_completed`, `reaudit_complete`).

```bash
./aafc deliver --client <id> --to jane@acmebakery.test --template report_ready \
    --provider gmail --var customer_name="Jane Doe" --var website=https://acmebakery.test ...
# add --send to actually send; default is DRY RUN and is always logged
```

## Status: what is real and what isn't

**IMPLEMENTED (tested):** customer intake + email dedup, source attribution,
website intake + auth gate, website auditor (vendored SitePulse) + plugin
registry, findings with evidence/severity/confidence, client reports,
verification reports, fix plans + finding lifecycle, projects, tasks, money
ledger with the no-manufactured-receivable rule, deadlines, pipeline state
machine (never auto-promotes leads), footprint discovery, social audit,
cross-channel checks, service catalog + evidence-gated opportunity map +
one→many expansion, re-audit loop with history preservation, Gmail dry-run
delivery + delivery log, 7 email templates, full CLI, 45-test suite.

**EXPERIMENTAL:** footprint/social discovery (public homepage links only; no
search API, so Google Business/directories report NOT FOUND honestly; major
social platforms usually bot-block fetches → honest ERROR findings).

**PLANNED (not built):** Postgres migration path, hosted deployment,
per-client report URLs, real re-audit scheduling.

**BLOCKED:** SwiftSend provider (no API exists; needs base URL + auth +
endpoint contract). Live Vercel/production changes (Amaury's read-only
boundary — authorization required).
