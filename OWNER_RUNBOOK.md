# AAFC Engine — Owner Runbook

For Amaury. Everything you need to run the business engine day-to-day.

## The 30-second version

The AAFC engine turns a website into a paying client: **audit → findings →
report → opportunities → proposal → project → re-audit → retainer.** One
command runs the whole front half:

```bash
cd ~/workspace/aafc-engine
./aafc audit-full --email tyriene.amey@gmail.com --name "Tyriene Amey" \
    --url https://www.lonhaca.com
```

That resolves (or registers) the customer, runs the audit, discovers the
social footprint, builds the opportunity map, and writes the report. The
report path is printed at the end.

## Daily commands

| What | Command |
|---|---|
| Full pipeline for a lead | `./aafc audit-full --email <email> --name "<name>" --url <site>` |
| Audit only | `./aafc audit run --email <email> --url <site>` |
| Re-audit after fixes | `./aafc audit run --email <email> --url <site>` (versions auto-increment) |
| Generate the client report | `./aafc report --email <email> --audit <audit_id>` |
| See opportunities | `./aafc opportunities --email <email>` |
| Log a call/email | `./aafc log --email <email> --channel phone --direction outbound --notes "..."` |
| Record money | `./aafc money add --email <email> --kind INVOICE --amount 500 --status EXPECTED --evidence "invoice #12"` |

Run `./aafc --help` for everything. Data lives in `./data/clients/<id>/`
(git-ignored, never committed).

## Rules the engine enforces (so you don't have to remember)

- **Findings ≠ opportunities ≠ proposals ≠ invoices ≠ money owed.** Each is a
  separate record. The engine refuses to invent any of them.
- **Opportunities only come from evidence.** No findings, no opportunities —
  never padded.
- **Audit history is append-only.** Re-audits create new versions; old audit
  files are never rewritten. Finding status changes live in a separate
  overlay file.
- **A website must be authorized before it's audited.** Unauthorized sites
  are refused, not audited.
- **One email = one customer.** Lookups by email can never load the wrong
  customer's records.

## Money states

`NEEDS_VERIFICATION → EXPECTED → OWED → PAID`, with `PARTIALLY_PAID` and
`OVERDUE` in the middle, `CANCELLED` as the other exit. `PAID` and `CANCELLED`
are terminal — nothing moves after them. Moving into `OWED`/`EXPECTED`/`OVERDUE`
requires evidence (e.g. an invoice number); moving to `PAID` requires proof of
payment. Plan A remains the financial source of truth; this ledger is an
operational mirror.

## Email

- Gmail delivery defaults to **dry-run** (nothing sends). Real sends need an
  explicit flag and your approval.
- SwiftSend is **blocked** until its contract is verified — the engine raises
  instead of guessing.
- Never send a customer email without the customer's explicit go-ahead.

## The web app

Public site: `https://aafc-engine.vercel.app` — paste a URL, get a Quick Audit
(3 pages, ~40s). Full 6-page rendered audits run from the CLI only.

## If something breaks

1. `./aafc --help` — check the command shape.
2. `tail` the output — errors are plain-language (`WebsiteError`,
   `NotAuthorizedError`, `MoneyError`), not tracebacks.
3. `cd ~/workspace/aafc-engine && .venv/bin/python -m pytest tests/ -q` —
   if the suite is green, the engine is fine and the input was bad.
4. Check `SYSTEM_STATUS.md` in this repo for the last verified state.

## What NOT to do

- Don't edit files under `data/` by hand — use the CLI.
- Don't commit `data/` (it's gitignored for a reason: client records).
- Don't run the pilot script against real customer data — it wipes and
  rebuilds its own store.
- Don't buy anything or connect paid services without saying so first.
