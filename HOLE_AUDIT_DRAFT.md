# AAFC Engine — Code Hole Audit (DRAFT)

**Auditor:** subagent code-audit pass · **Date:** 2026-09-27
**Scope:** `~/workspace/aafc-engine` — `aafc_engine/` (all modules), `api/index.py`, `web/`
**Method:** read the actual source files; no test suite run; nothing fixed.
**Out of scope (already VERIFIED per prior work):** engine scoring, FIX 9 bot-protection behavior, 50-site results, API happy path, UI happy path.

**Totals: 31 findings — 3 HIGH · 11 MEDIUM · 17 LOW**

---

## HIGH

### H1 — `clients.update_client` allows email change with no validation or dedup check
**File:** `aafc_engine/clients.py:164`

```python
def update_client(store: Store, client_id: str, **fields: Any) -> dict:
    client = get_client(store, client_id)
    for protected in ("id", "created_at"):
        fields.pop(protected, None)
    client.update(fields)   # <-- email, record_type, anything: no validation
    client["updated_at"] = utc_now_iso()
    store.write_json(client_id, client, _CLIENT_FILE)
    return client
```

**Why it matters:** `create_client` validates email shape and dedups by email, but `update_client` writes arbitrary fields. `update_client(store, cid_a, email="victim@example.com")` creates an email collision with client B; `find_client_by_email` returns the first match, so `resolve_customer` / `self-service intake` can then load the **wrong customer's** filing cabinet (audits, money, communications). This is the exact "misattribute a customer record" failure the integrity pass must prevent. It also allows setting `record_type` to any garbage string, bypassing the LEAD→CLIENT promotion control.

### H2 — `footprint._fetch_homepage` performs SSRF-unguarded server-side fetches
**File:** `aafc_engine/footprint.py:64`

```python
def _fetch_homepage(url: str) -> dict:
    try:
        response = requests.get(
            url, headers=UA, timeout=TIMEOUT, allow_redirects=True
        )
```

**Why it matters:** This is a second, independent HTTP fetcher that **bypasses the engine's `FETCH_GUARD`** (the SSRF guard enforced in `engine.fetch` and `api/index.py`). No private/loopback/link-local IP check, no redirect-target check, and `allow_redirects=True` follows redirects anywhere. The URL comes from `websites.validate_url`, which explicitly permits `localhost` ("allowed for tests", `websites.py:90`), and `discover()` never calls `require_authorized`. Exposure today is CLI-only (`cli.py:595`), but a compromised customer site or a careless operator entry turns this into a server-side probe of internal resources. The module's own docstring claims "public web only" — the code does not enforce it.

### H3 — `social_audit._fetch_profile` performs SSRF-unguarded server-side fetches
**File:** `aafc_engine/social_audit.py:70`

```python
def _fetch_profile(url: str) -> dict:
    try:
        response = requests.get(url, headers=UA, timeout=TIMEOUT,
                                allow_redirects=True)
```

**Why it matters:** Same defect class as H2, in the module that fetches **third-party-influenced URLs** (profile URLs extracted from the target site's own HTML, stored in `social.json`). No private-IP guard, no redirect-target guard, no response-size limit. Called from `cli.py:619` only, so exposure is the operator machine — but it is the same machine that holds every customer record, and the URL content is attacker-influenceable.

---

## MEDIUM

### M1 — `money.record_event` accepts negative amounts
**File:** `aafc_engine/money.py:65` (amounts converted at `money.py:35`)

```python
    amount_cents = _to_cents(amount_dollars)   # int(round(float(dollars) * 100))
    amount_paid_cents = _to_cents(amount_paid_dollars)
    # no check that amounts are >= 0
```

`record_event(store, cid, "INVOICE", -500.0, "OWED", evidence="x")` happily records a **negative-$500 receivable**, which corrupts `outstanding_cents()` (it subtracts from what the customer owes). The evidence hard rule is enforced; the amount-sign rule is not. A negative `PAYMENT` with `PAID` status is equally possible.

### M2 — `money.set_event_status` has no transition matrix and keeps no audit trail
**File:** `aafc_engine/money.py:139`

Any status → any status is allowed: `PAID → OWED`, `CANCELLED → PAID`, `PAID → EXPECTED` all pass validation as long as evidence is present. There is also no transition history: the event dict is mutated in place (`event["status"] = new_status`), overwriting `evidence` on the PAID path, with no `updated_at` and no record of what the status was before. For a money ledger, "who changed what, from what, to what, when" is the entire point — it is absent.

### M3 — `findings.set_finding_status` rewrites historical audit files in place
**File:** `aafc_engine/findings.py:185`

```python
    for audit_id, audit in _iter_audit_files(store, client_id):
        for finding in audit.get("findings", []):
            if isinstance(finding, dict) and finding.get("id") == finding_id:
                finding["status"] = status
                store.write_json(client_id, audit, "audits", f"{audit_id}.json")
```

**Why it matters:** Called by `fixplans.create_fix_plan` (sets RECOMMENDED), `fixplans.advance_finding`, and `reaudit.verify_finding` (sets VERIFIED). Every one of these **mutates a saved historical audit JSON**. The system's own contract is "audit history is append-only" / "never overwrite the historical audit". After a re-audit verification, audit #1's file no longer reflects what audit #1 actually found — its findings say VERIFIED, a state that did not exist when the audit ran.

### M4 — `build_opportunity_map` dedupes on random finding IDs → duplicate opportunities
**File:** `aafc_engine/opportunities.py:83` (dedupe check at lines 118–124)

```python
    matches = services.match_services(findings)   # triggered_by = [finding ids]
    ...
        already = any(
            o.get("service") == service["slug"]
            and sorted(o.get("triggering_findings", [])) == triggering
            for o in existing
        )
```

**Why it matters:** Finding IDs are `f_<uuid4 hex>` generated fresh on **every** audit run (`findings.from_engine_finding`). After a re-audit, the same service matches the same (check, page) problems but with all-new finding IDs, so `already` is False and a **duplicate opportunity** (same service, same problem) is appended. The "one-offering → multiple-opportunities" map accumulates duplicates on every rebuild. The dedupe key should be (service, check/page keys), not random IDs.

### M5 — `footprint.discover` / `social_audit.audit_social` skip authorization
**Files:** `aafc_engine/footprint.py:110`, `aafc_engine/social_audit.py:107`

Neither function calls `websites.require_authorized`. `registry.run_audit` refuses to audit an unauthorized website (`NotAuthorizedError`); the footprint and social modules will happily fetch and record data for a website the customer never authorized. Authorization enforcement is inconsistent across the pipeline.

### M6 — `fixplans.create_fix_plan` is not idempotent → duplicate tasks
**File:** `aafc_engine/fixplans.py:32`

Re-running `create_fix_plan` for the same audit creates a **second task per finding** (via `tasks_mod.create_task`) with no check for an existing plan covering that audit. There is no dedupe on (audit_id, finding_id). Operators re-running a command get doubled task lists.

### M7 — `_IMPLEMENTATION_PATHS` keys don't match real engine check names (~10 of 21 dead)
**File:** `aafc_engine/fixplans.py:175`

```python
_IMPLEMENTATION_PATHS: dict[str, str] = {
    ...
    "broken_link": (...),   # engine emits "broken_links"
    "robots": (...),        # engine emits "robots_txt"
    "headings": (...),      # engine emits "h1", "heading_order"
    "alt_text": (...),      # engine emits "img_alt"
    "performance": (...),   # engine emits "page_weight", "ttfb"
    "mobile": (...),        # engine emits "viewport"
    "trust": (...), "content": (...),   # engine emits nothing like these
    "indexing": (...),      # engine emits "indexability"
```

`implementation_path_for()` looks up the finding's real check name, misses on every stale key, and returns the generic fallback. The "concrete implementation steps per check" are effectively dead code for real findings.

### M8 — `--pages` uncapped; `run_audit(max_pages=…)` uncapped; default `time_budget=None`
**Files:** `aafc_engine/cli.py:812`, `aafc_engine/cli.py:1016`, `aafc_engine/auditor/registry.py:126`

```python
p.add_argument("--pages", type=int, default=6)   # no upper bound, no lower bound
```

`aafc audit run --pages 100000` (or `--pages -5`) flows straight into `engine.audit_site`, whose crawl loop is `while queue and attempts < max_pages` with no time budget by default. A typo'd `--pages 1000000` is a runaway crawl with no server-side stop. The hosted API clamps to 3; the CLI has no clamp at all.

### M9 — `POST /api/audit` has no rate limiting
**File:** `api/index.py` (`api_audit_post`)

Each POST triggers up to 52 seconds of server-side crawling, synchronously, with no per-IP rate limit, no auth, no CAPTCHA, no job queue. The in-memory `_JOBS` cache is bounded (128), but concurrent POSTs are unbounded — a trivial loop can saturate the function with overlapping 52s audits. (Vercel edge protections may blunt this; the application itself has none.)

### M10 — `Store` allows path traversal via `client_id`
**File:** `aafc_engine/store.py:47` (`client_dir`), `store.py:87` (`write_json`)

```python
    def client_dir(self, client_id: str) -> Path:
        directory = self.root / "clients" / client_id
        directory.mkdir(parents=True, exist_ok=True)
```

No validation that `client_id` is a bare `cl_<hex>` identifier. A `--client ../../x` argument escapes the data root: `write_json` will `mkdir` and write attacker-influenced JSON outside `data/`, and `read_json` will read arbitrary JSON files. Exposure is CLI-operator-only today (the hosted API has no customer endpoints), but the store is the trust boundary for client isolation and it doesn't validate its own key.

### M11 — `projects.create_project` doesn't validate `website_id` / `audit_id` references
**File:** `aafc_engine/projects.py:32`

`website_id` and `audit_id` are stored verbatim with no check that they exist — or that they belong to *this* client. Dangling and cross-client foreign keys are possible, breaking the "everything traceable back to the customer" invariant.

---

## LOW

### L1 — Audit `version` collides if an audit file is deleted
**File:** `aafc_engine/auditor/registry.py:158` — `version = len(list_audits(store, client_id, website_id)) + 1`. Delete audit v1 of 2, run again → new audit also gets version 2. `list_audits` sorts by version, so ordering/history display becomes ambiguous.

### L2 — `validate_url` allows `localhost` ("for tests") in production
**File:** `aafc_engine/websites.py:90` — `if "." not in host and host != "localhost": raise`. A test-only allowance reachable in production; combined with H2/H3 it widens the SSRF surface.

### L3 — Social findings can never trigger opportunities
**File:** `aafc_engine/services.py` — `KNOWN_CHECKS = ENGINE_CHECKS | XCHECK_NAMES`; social check names (`social_<platform>_<aspect>`) are in neither set, and adding one as a trigger would raise `ValueError` at catalog load. `opportunities._collect_evidence_findings` gathers social findings as evidence, but no service can ever match them — they're inert in the opportunity map.

### L4 — `tasks.set_task_status` erases completion history
**File:** `aafc_engine/tasks.py:87` — moving a task off DONE clears `completed_at`; moving DONE→TODO→DONE fabricates a new completion time. No `updated_at`, no status history.

### L5 — `social_audit` keeps only the latest social audit
**File:** `aafc_engine/social_audit.py:107` — `profile["last_audit"] = {...}` overwrites the previous run; unlike website audits (append-only files), social audits have no history.

### L6 — `_timeout_partial` labels an unverified finding `HTTP_FETCH`
**File:** `api/index.py` (`_timeout_partial`) — the honest PARTIAL envelope's finding carries `"verification": "HTTP_FETCH"` while its own evidence text says "no page verified". Minor honesty nit in the one path that exists to be maximally honest.

### L7 — Non-string `url` in POST body → 500 instead of 400
**File:** `api/index.py` (`_normalize_target`) — `(raw or "").strip()` raises `AttributeError` for a JSON number/list/bool, which `api_audit_post` catches as a 500 "Audit failed" instead of a 400 validation error.

### L8 — No response-size cap on any fetcher
`engine.fetch` (`aafc_engine/auditor/engine.py:284`), `footprint._fetch_homepage`, `social_audit._fetch_profile` all do `requests.get(...)` + full `r.text`/`r.content` with no `stream=True` / max-bytes guard. A target serving a multi-GB response exhausts memory. No `Content-Length` check either.

### L9 — Hardcoded personal reference in `swiftsend.py`
**File:** `aafc_engine/delivery/swiftsend.py:18` — the `NotImplementedError` message names "Kiminou's mom" as the source of the missing API contract. Organizational/personal detail baked into source; belongs in docs/config, not an exception string.

### L10 — `customers.log_communication` validates nothing
**File:** `aafc_engine/customers.py:341` — `channel` and `direction` are free text; `"inboud"` or any garbage persists. Should validate `direction ∈ {inbound, outbound}` at minimum.

### L11 — Frontend nits
**File:** `web/app.js`
- The hint "Nothing is stored — the report is generated fresh each run" is slightly off: results persist in the server's in-memory job cache (up to 128).
- `esc(r.elapsed_total)` renders raw floats (e.g. `3.456789s`); cosmetic.
- The findings sort comparator yields `NaN` for unknown severities (no crash, undefined order).
- The "No score this run — the site couldn't be loaded" message also covers PARTIAL-with-null-score cases where pages *were* crawled.

### L12 — `append_json_list` is lock-free read-modify-write
**File:** `aafc_engine/store.py:118` — two concurrent writers (threads/processes) can lose appends. Fine for single-operator CLI; fragile if the customer layer ever moves server-side.

### L13 — Possible `TypeError` on null `profiles` in social.json
`social_audit.py` (`social_doc.get("profiles", [])`) and `opportunities._collect_evidence_findings` assume a list; a JSON `null` there raises `TypeError`. Same pattern in `customers.get_customer_file` (`social_profiles`).

### L14 — `reports._write_markdown` leaves `.tmp` on crash; `out_dir` is an arbitrary path
**File:** `aafc_engine/reports.py` (`_write_markdown`) — no cleanup of the temp file on failure; `out_dir` lets the caller write the markdown anywhere on disk (CLI-operator-only exposure).

### L15 — `websites.normalize_url` dedup gaps
**File:** `aafc_engine/websites.py:36` — default ports (`:443`) and `user:pass@` userinfo are not normalized, so `https://example.com:443` and `https://example.com` dedup as different websites.

### L16 — Unbounded `_dns_cache` growth
**File:** `api/index.py` (`_resolve_ips`) — every distinct hostname queried is cached; a flood of random hostnames grows memory without bound on the serverless instance.

### L17 — `bot_protection` has no confidence-table entry
`aafc_engine/findings.py` `CONFIDENCE_BY_CHECK` lacks the engine's own `bot_protection` check name, so those findings get the default `"NEEDS MANUAL REVIEW"` confidence. Arguably intentional (it's a measurement refusal, not a site fact), but the table's header comment claims to be built from the real check list — `bot_protection` is missing from it.

---

## Explicitly checked, no hole found

- **No hardcoded credentials/secrets/tokens** anywhere in `aafc_engine/`, `api/`, `web/` (grep for api_key/secret/password/token clean; `render.py:69` proxy password comes from `HTTPS_PROXY` env only).
- **No committed customer data**: `data/` is gitignored and empty in the repo; the working tree is clean.
- **No TODO/FIXME/placeholder code** in the package (only `swiftsend.py`'s intentional `NotImplementedError` and `delivery/base.py`'s abstract `send`).
- **Engine crawl budget**: `audit_site` counts every fetch attempt toward `max_pages` and enforces budget-aware per-request timeouts (`engine.py:1009-1011`, `980-1000`); redirects capped at 10 (`MAX_REDIRECTS`); redirect targets re-checked via `FETCH_GUARD` hook (`engine.py:274-282`).
- **Render degradation is honest**: `render_degraded` mode recorded when Chromium fails (`engine.py:1217-1220`).
- **Money evidence hard rule** enforced on record and on transitions into receivable states; `PARTIALLY_PAID` bounds checked.
- **Report honesty**: `generate_audit_report` refuses non-COMPLETE audits; verification reports use recorded evidence only.
- **Source permanence**: `sources.set_source` refuses silent overwrites (force-flagged + logged).
- **Pipeline**: forward-one-stage / backward-any-stage transitions enforced; never touches `record_type`.
- **Frontend**: all dynamic content passes through `esc()` — no XSS sink found.
- **Dependencies**: deploy set (`requirements.txt`) is exactly flask/requests/pyyaml + transitive pins — all used, none unused.

---

## VALIDATION & REPAIR LOG — 2026-09-27

Every finding was independently source-validated before repair. Repairs are
listed with the exact mechanism; regressions are still pending (full suite
runs after the whole batch). Legend: **CONFIRMED+REPAIRED** / **FALSE
POSITIVE** / **BY DESIGN** / **NOT CONFIRMED**.

### HIGH

- **H1 — CONFIRMED+REPAIRED.** `clients.update_client` now routes email
  changes through `_validate_email`, rejects collisions with another client,
  and protects `record_type` (promotion only via `promote_to_client`).
- **H2 — CONFIRMED+REPAIRED.** `footprint._fetch_homepage` now uses the new
  shared `aafc_engine/safefetch.py`: DNS is resolved up front and every
  address must be globally routable, redirects are followed manually (never
  `allow_redirects=True`) with every hop re-resolved and re-validated, and
  bodies are streamed with a 5 MB cap. Reuses the engine's posture; the
  engine's own vendored fetch guard is untouched.
- **H3 — CONFIRMED+REPAIRED.** `social_audit._fetch_profile` uses the same
  shared `safefetch.safe_get`. Known residual: DNS is checked before connect
  (no TOCTOU/rebinding defense) — documented in `safefetch.py`; proportionate
  for an operator-side fetcher that only targets chosen customer websites.

### MEDIUM

- **M1 — CONFIRMED+REPAIRED.** `money.record_event` rejects negative amounts
  and negative paid amounts.
- **M2 — CONFIRMED+REPAIRED.** `money.set_event_status` now enforces a
  transition matrix (NEEDS_VERIFICATION → EXPECTED|OWED|CANCELLED;
  EXPECTED → OWED|OVERDUE|PARTIALLY_PAID|PAID|CANCELLED; OWED → same;
  OVERDUE → PARTIALLY_PAID|PAID|CANCELLED; PARTIALLY_PAID → PAID|CANCELLED;
  PAID/CANCELLED terminal), is idempotent on same-status, stamps
  `updated_at`, and keeps append-only `status_history`.
- **M3 — CONFIRMED+REPAIRED.** Finding lifecycle state moved to
  `finding_status.json`; `get_finding` merges the overlay; saved audit JSON
  files stay byte-for-byte pristine (append-only history).
- **M4 — CONFIRMED+REPAIRED.** Opportunity dedupe now keys on a stored
  `dedupe_key` = `service_slug::` sorted stable `(check, page)` pairs instead
  of random per-audit finding IDs. Rebuilding after a re-audit refreshes
  evidence instead of appending duplicates. Back-compat kept for records
  written before `dedupe_key` existed.
- **M5 — CONFIRMED+REPAIRED.** `footprint.discover` and
  `social_audit.audit_social` now call `require_authorized` for the website.
- **M6 — CONFIRMED+REPAIRED.** `create_fix_plan` skips findings already
  covered for the same audit; when nothing new remains it returns the
  existing plan without creating tasks.
- **M7 — CONFIRMED+REPAIRED.** `_IMPLEMENTATION_PATHS` mappings fixed for
  real checks (broken_links, robots_txt, h1, heading_order, img_alt,
  page_weight, ttfb, viewport, indexability, hsts, charset, lang, parse,
  bot_protection).
- **M8 — CONFIRMED+REPAIRED.** CLI `--pages` clamped to 1–12 with a printed
  warning (both `audit run` and `audit-full`); `registry.run_audit` raises
  `ValueError` on `max_pages < 1`. The registry default `time_budget=None`
  is correct for the local CLI (no serverless limit); the hosted path keeps
  its own ~40s/52s budget.
- **M9 — CONFIRMED+REPAIRED.** `POST /api/audit` now has a per-IP sliding
  window (10 starts / rolling 60s) returning HTTP 429 with `Retry-After`.
  Honest scope: per-instance (Vercel may run several; cold starts reset),
  stops casual abuse, not a distributed flood.
- **M10 — CONFIRMED+REPAIRED.** `store.py` validates client IDs
  (`cl_` + 12 lowercase hex) and rejects path separators, `.`/`..`, empty
  components, and `~`-prefixed path parts on read/write.
- **M11 — CONFIRMED+REPAIRED.** `projects.create_project` validates
  `website_id` (exists via `get_website`) and `audit_id` (exists in the
  client's `audits/` dir); dangling or cross-client references raise
  `ValueError`.

### LOW

- **L1 — CONFIRMED+REPAIRED.** Audit `version` is now `max(existing)+1`, so a
  deleted audit file can never collide with a surviving version.
- **L2 — BY DESIGN.** `validate_url` permits `localhost` for tests (used by
  the test suite). Fetch-time enforcement is the real boundary:
  `safefetch.safe_get` refuses all non-global addresses including localhost,
  and the hosted engine fetch guard does the same. Registration is not the
  security boundary; this is documented in `validate_url`'s docstring.
- **L3 — CONFIRMED+REPAIRED.** `services.KNOWN_CHECKS` now includes
  `SOCIAL_CHECKS` (`social_<platform>_<aspect>` for all platforms/aspects
  `social_audit` emits), and the `social-media-system` catalog entry gained
  `social_<platform>_reachable` / `social_<platform>_website_linkback`
  evidence triggers. `match_services` still only matches FAIL/ERROR
  findings, so evidence-gating is preserved.
- **L4 — CONFIRMED+REPAIRED.** `tasks.set_task_status` no longer clears
  `completed_at` when a task leaves DONE (first completion time is kept
  honestly); every transition appends to `status_history` and stamps
  `updated_at`. New tasks carry `updated_at` and `status_history: []`.
- **L5 — CONFIRMED+REPAIRED.** `social_audit` now appends every run to the
  profile's `audit_history`; `last_audit` remains the latest run.
- **L6 — CONFIRMED+REPAIRED.** The `_timeout_partial` finding's
  `"verification"` is now `"NONE"` — the fetch never completed, so claiming
  `HTTP_FETCH` would have been a lie.
- **L7 — CONFIRMED+REPAIRED.** `_normalize_target` rejects non-string URLs
  with `ValueError` → HTTP 400 (previously AttributeError → HTTP 500).
- **L8 — CONFIRMED+REPAIRED (business layer).** `safefetch.safe_get` streams
  with a 5 MB body cap and flags truncation. The vendored engine's own
  fetcher is out of scope (frozen verified engine; untouched).
- **L9 — CONFIRMED+REPAIRED.** SwiftSend `NotImplementedError` no longer
  names a personal reference; it says "from the account owner."
- **L10 — CONFIRMED+REPAIRED.** `customers.log_communication` validates
  `direction` (`inbound`/`outbound` only) and requires a non-blank `channel`.
- **L11 — PARTIALLY CONFIRMED.** (a) NaN comparator in `web/app.js` findings
  sort — CONFIRMED+REPAIRED (unknown severities now sort last via `?? 3`).
  (b) "Nothing is stored" hint — CONFIRMED+REPAIRED (reworded: short-lived
  server memory only, never a database). (c) `esc()` leaving floats
  unescaped — FALSE POSITIVE (`esc` stringifies everything first).
  (d) PARTIAL message showing a raw check name — NOT CONFIRMED in current
  code (no raw check name rendered).
- **L12 — FALSE POSITIVE.** `store.write_json` is atomic file replacement;
  no lock-free list append exists in the current code.
- **L13 — CONFIRMED+REPAIRED.** Null-safe `social_doc.get("profiles")`
  guards added in `social_audit.py`, `crosscheck.py`, `opportunities.py`,
  and `customers.py` (a JSON `null` no longer throws `TypeError`).
- **L14 — FALSE POSITIVE.** No `tempfile`/`NamedTemporaryFile`/`mkstemp`
  usage exists anywhere in `aafc_engine/` or `api/`; there is nothing to
  clean up.
- **L15 — CONFIRMED+REPAIRED.** `normalize_url` now strips default ports
  (`:80`/`:443`) and drops userinfo, so `https://example.com:443` and
  `https://example.com` dedupe to one website and credentials can never
  become part of the stored identity.
- **L16 — CONFIRMED+REPAIRED.** `api/index.py` DNS cache is bounded
  (`_DNS_CACHE_MAX = 1000`) with expired-first, then oldest, eviction via
  `_cache_ips`.
- **L17 — CONFIRMED+REPAIRED.** `findings.CONFIDENCE_BY_CHECK` now maps
  `bot_protection → CONFIRMED`: the refusal is directly observed (403/429 +
  bot-wall markers); the finding asserts "we could not measure", never a
  fake site fact.

### Still to do after this log

Focused regressions for every repair above, the remaining try-to-break
matrix, the full 103+ test suite, the final Ty/LONHA pilot from a clean
store, then commit/push/deploy verification, docs, runbook, system-status
file, and the owner handoff email.
