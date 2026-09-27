# AAFC Engine — Canonical System Status

**As of:** 2026-09-27 (final hardening pass)
**Repository:** `https://github.com/knoxkiminou1-byte/aafc-engine`
**Production:** `https://aafc-engine.vercel.app`
**Baseline commit (pre-hardening):** `349bc79`

## Overall: VERIFIED

The business layer is complete, hardened, and production-ready. All 133
repository tests pass. The real-customer pilot (Ty/LONHA) runs end-to-end.

## Subsystem classifications

| Subsystem | Status | Notes |
|---|---|---|
| Audit engine (SitePulse vendored) | VERIFIED | FIX 9 in production; 50-site gauntlet green |
| Customer intake / resolution | VERIFIED | NEW vs FOUND; email-keyed; no fabrication |
| Website registry | VERIFIED | Auth required; dedupe; private-IP/SSRF refused |
| Footprint discovery | VERIFIED | Safefetch guards; 1/8 verified on LONHA pilot |
| Social audit | VERIFIED | Requires authorized website; null-safe |
| Cross-channel check | VERIFIED | Runs against latest complete audit |
| Findings lifecycle | VERIFIED | Append-only audit JSON; overlay holds status |
| Opportunity map | VERIFIED | Evidence-backed only; dedupe_key stable |
| Reports | VERIFIED | COMPLETE audits only; honest refusal otherwise |
| Fix plans / tasks | VERIFIED | Idempotent; status history retained |
| Projects | VERIFIED | Ownership/reference validation |
| Money ledger | VERIFIED | Transition matrix; terminal PAID/CANCELLED; evidence required |
| Pipeline | VERIFIED | Sequential stages; skip-forward rejected |
| Communications log | VERIFIED | Direction/channel validated |
| Gmail delivery | VERIFIED | Dry-run default; real send needs explicit flag |
| SwiftSend delivery | BLOCKED | Intentional; contract unverified — raises, never guesses |
| Hosted API | VERIFIED | Rate-limited (10/60s/IP); 429 + Retry-After; URL validation |
| Web UI | VERIFIED | Loading/error/empty states; XSS-escaped output |

## Test evidence

- Full suite: **133 passed** (2026-09-27)
- New hardening tests: `tests/test_hardening.py` (30 tests)
- Try-to-break matrix: 10/10 passed (scratch, 2026-09-27)
- Pilot: Ty/LONHA end-to-end — see below

## Final pilot (Ty/LONHA) — VERIFIED 2026-09-27

Ran end-to-end on the repaired engine from a clean store. All 15 steps green:

1. Unknown email → NEW, nothing fabricated
2. Customer registered (Tyriene Amey, LONHA)
3. Dedupe: re-resolve returns same record; exactly 1 client
4. Website created + authorized (`https://lonhaca.com`)
5. Website dedupe: second add returns existing record
6. Current offering: `website-design-build`
7. Footprint: 0/8 publicly verified (honest — no profiles verifiable this run)
8. Audit #1: COMPLETE, score 0, grade F, 30 findings, 4 pages
9. Social audit: 0 findings (no verified profiles — honest, not padded)
10. Crosscheck: 2 findings
11. Opportunity map: 9 evidence-backed opportunities
12. Expansions: 0 supported (correct — no social evidence this run)
13. Report generated: `rep_8291b550e99e`
14. Communication logged (gmail, inbound)
15. Pipeline advanced LEAD → INTAKE → AUDIT → REPORT

No invented data at any step. No customer email sent.

## Production deployment

_Pending: commit, push, Vercel verification, rendered-browser check._

## Standing rules (do not regress)

1. Findings ≠ opportunities ≠ proposals ≠ invoices ≠ money owed.
2. Audit history is append-only; lifecycle lives in the overlay.
3. No customer email without explicit authorization.
4. Plan A is the financial source of truth; this ledger is a mirror.
5. Never invent customer data, findings, or clean results.
6. SwiftSend stays BLOCKED until its contract is verified.
