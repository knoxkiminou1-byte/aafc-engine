# AAFC Engine — Build Contract (READ FIRST)

Every module listed here is being built in parallel by different agents.
Follow this contract EXACTLY so the pieces fit together. Do not invent
different function names or schemas.

## Repo layout (all under ~/workspace/aafc-engine/)

```
aafc_engine/
  __init__.py          # __version__ = "0.1.0"
  store.py             # storage primitives (see below)
  clients.py
  websites.py
  auditor/
    __init__.py
    engine.py          # VENDORED ~/workspace/sitepulse/audit.py, see auditor contract
    registry.py
  findings.py
  reports.py
  fixplans.py
  projects.py
  tasks.py
  money.py
  deadlines.py
  pipeline.py
  reaudit.py
  delivery/
    __init__.py
    base.py
    gmail.py
    swiftsend.py
  templates/
    __init__.py
    audit_received.txt, audit_complete.txt, report_ready.txt,
    fix_plan_ready.txt, project_ready.txt, fixes_completed.txt,
    reaudit_complete.txt
  cli.py
tests/
  (written later by the test agent)
aafc                   # executable shim: python3 -m aafc_engine.cli "$@"
README.md requirements.txt LICENSE .gitignore
```

## Data layout

Store root defaults to env `AAFC_DATA_DIR`, else `<repo>/data`.
Per-client isolation: `data/clients/<client_id>/` containing:

```
client.json  websites.json  projects.json  tasks.json  money.json
deadlines.json  pipeline.json  delivery_log.json
audits/<audit_id>.json
reports/<report_id>.json  reports/<report_id>.md
```

NEVER read or write outside the given client_id's directory (except
listing client dirs for dedup by email). This is the isolation mechanism.

## store.py

```python
class Store:
    def __init__(self, root: str | Path | None = None)
    def client_dir(self, client_id: str) -> Path        # creates dirs
    def read_json(self, client_id, *parts, default=None)  # parts like "audits", f"{aid}.json"
    def write_json(self, client_id, obj, *parts)       # ATOMIC: tmp file + os.replace
    def append_json_list(self, client_id, item, *parts)  # read list (default []), append, write
    def new_id(self, prefix: str) -> str                # e.g. "cl_" + uuid4().hex[:12]
    def client_exists(self, client_id: str) -> bool
    def list_client_ids(self) -> list[str]
class StoreError(Exception): ...
```

Timestamps: `datetime.now(timezone.utc).isoformat()`.

## clients.py

```python
class DuplicateClientError(Exception): ...
class ClientNotFoundError(Exception): ...

def create_client(store, name, email, business_name=None, phone=None, notes="", source=None) -> tuple[dict, bool]
    # validates email with regex; strips/lowercases email
    # DEDUP: if a client with same email exists (any client dir), return (existing, False)
    # else create client.json: {id,name,email,business_name,phone,notes,source,
    #   record_type:"LEAD", created_at, updated_at}
def get_client(store, client_id) -> dict          # raises ClientNotFoundError
def list_clients(store) -> list[dict]
def find_client_by_email(store, email) -> dict | None
def promote_to_client(store, client_id) -> dict  # record_type LEAD -> CLIENT; idempotent
def update_client(store, client_id, **fields) -> dict
```

## websites.py

```python
class WebsiteError(ValueError): ...
class NotAuthorizedError(PermissionError): ...

def normalize_url(url: str) -> str
    # add https:// if no scheme; lowercase host; drop fragment; drop trailing
    # slash except root; raise WebsiteError on empty/unparseable
def validate_url(url: str) -> str
    # returns normalized; scheme must be http/https; host must contain a dot
    # (allows 'localhost' for tests); raise WebsiteError otherwise
def add_website(store, client_id, url, label=None, authorized=False) -> tuple[dict, bool]
    # DEDUP per client by normalized_url -> (existing, False)
    # website: {id, client_id, url (normalized), domain, label,
    #           authorized: bool, created_at}
def get_website(store, client_id, website_id) -> dict
def list_websites(store, client_id) -> list[dict]
def set_authorized(store, client_id, website_id, authorized=True) -> dict
def require_authorized(website: dict) -> None  # raises NotAuthorizedError if not authorized
```

## auditor/engine.py

Vendor ~/workspace/sitepulse/audit.py VERBATIM except:
- prepend header comment:
  `# Vendored from sitepulse (same author, MIT license). Single audit implementation for aafc-engine.`
- keep `audit_site(start_url, max_pages=6)`, `F`, `check_page`, `PageParser`,
  `check_robots_and_sitemap`, `check_broken_links`, `render_client_report`,
  `render_technical_report`, `main` unchanged.
- `from .engine import audit_site` must work; `auditor/__init__.py` re-exports
  `audit_site`.

## auditor/registry.py  (the plugin model, spec R)

```python
CHECKS: dict[str, callable] = {}
def register(name: str):  # decorator; raises ValueError on duplicate name
def list_checks() -> list[str]
def run_audit(store, client_id, website_id, max_pages=6) -> dict
```

- `run_audit`: loads website, calls `require_authorized`, runs every
  registered check in registration order, SAVES audit JSON to
  `audits/<audit_id>.json`, returns the audit dict.
- Check function signature: `fn(url: str, ctx: dict) -> list[dict]`
  where `ctx = {"max_pages": int, "site": dict|None}` (site filled by core check).
- Finding dict schema (FINAL, used repo-wide):
```python
{
  "id": "f_<12hex>", "check": "<check name>", "page": "<url>",
  "issue": "<short title>", "evidence": "<concrete evidence>",
  "severity": "CRITICAL|HIGH|MEDIUM|LOW|PASS",
  "confidence": "CONFIRMED|LIKELY|NEEDS MANUAL REVIEW",
  "why_it_matters": "...", "recommended_fix": "...",
  "implementation_path": "...",   # concrete steps, may be ""
  "verification_method": "...",
  "result": "FAIL|PASS|ERROR",
  "status": "FOUND",              # finding lifecycle state
  "audit_id": "...", "website_id": "...", "client_id": "...",
}
```
- Built-in check `"sitepulse_core"` (registered first): calls
  `engine.audit_site(url, max_pages)`, converts each raw finding via
  `findings.from_engine_finding(...)`, stores raw site dict in ctx for
  other checks. If `site["pages_crawled"] == 0` the audit `status` is
  `"FAILED"` (never fake success); else `"COMPLETE"`.
- Audit dict saved:
```python
{
  "id": "aud_...", "client_id":..., "website_id":..., "url":...,
  "version": <int, 1-based per website>, "started_at":..., "status": "COMPLETE|FAILED",
  "engine": "kiminou-website-audit/1.0", "score":..., "grade":...,
  "pages_crawled":..., "counts": {"critical":..,"warning":..,"info":..},
  "findings": [...], "elapsed_total":...,
  "notes": [...],  # honest limitation notes
}
```
- `get_audit(store, client_id, audit_id)`, `list_audits(store, client_id, website_id=None)`.
- `class AuditFailedError(Exception)`.

## findings.py

```python
SEVERITIES = ["CRITICAL","HIGH","MEDIUM","LOW","PASS"]
CONFIDENCES = ["CONFIRMED","LIKELY","NEEDS MANUAL REVIEW"]
FINDING_STATES = ["FOUND","RECOMMENDED","READY_TO_IMPLEMENT","IMPLEMENTED","VERIFIED"]
SEVERITY_MAP = {"critical":"CRITICAL","warning":"HIGH","info":"LOW"}  # engine -> ours
CONFIDENCE_BY_CHECK: dict  # check name -> confidence; default "NEEDS MANUAL REVIEW"
def from_engine_finding(raw: dict, *, audit_id, website_id, client_id) -> dict
    # raw keys: page, check, severity, title, evidence, why_it_matters,
    #           recommended_fix, technical
    # maps severity; confidence from CONFIDENCE_BY_CHECK (document the table);
    # verification_method derived from check ("re-run <check>"); status "FOUND";
    # result "FAIL"; id generated f_<hex>
def get_finding(store, client_id, finding_id) -> dict  # searches client's audits; raises KeyError
def set_finding_status(store, client_id, finding_id, status) -> dict
    # validates status in FINDING_STATES; rewrites the owning audit file
def finding_key(finding) -> tuple  # (check, page) for re-audit matching
```

## reports.py

```python
class ReportError(Exception): ...

def generate_audit_report(store, client_id, audit_id, out_dir=None) -> tuple[dict, str]
    # raises ReportError if audit["status"] != "COMPLETE"  (FAILED audits -> BLOCKED, never fake)
    # markdown sections (exact headings):
    #   # Website Audit Report — <domain>
    #   ## Score, ## What we checked, ## What we found, ## Where we found it,
    #   ## Why it matters, ## What should change, ## What AAFC can do,
    #   ## How we will verify the fix, ## Limitations
    # findings grouped CRITICAL -> HIGH -> MEDIUM -> LOW -> PASS
    # saves reports/<report_id>.md and .json {id,client_id,website_id,audit_id,
    #   created_at,score,grade,counts,path}; returns (report_dict, md_path)
def generate_verification_report(store, client_id, old_audit_id, new_audit_id) -> tuple[dict, str]
    # uses reaudit.compare; BEFORE -> AFTER with real evidence only;
    # never invents improvement numbers
def get_report(store, client_id, report_id) -> dict
def list_reports(store, client_id) -> list[dict]
```

## fixplans.py

```python
def create_fix_plan(store, client_id, audit_id, project_id=None) -> dict
    # one task per finding with severity in CRITICAL/HIGH/MEDIUM and result FAIL
    # (NEEDS MANUAL REVIEW findings included, flagged "needs manual review first")
    # creates tasks via tasks.create_task(..., finding_id=...); plan:
    # {id:"fix_...", client_id, audit_id, project_id, created_at,
    #  items:[{finding_id, task_id, state:"RECOMMENDED"}]}
    # sets each finding status RECOMMENDED
def advance_finding(store, client_id, finding_id, to_state, note="") -> dict
    # allowed: forward or backward ONE step along FINDING_STATES;
    # to VERIFIED is REJECTED here (ValueError) — only reaudit.verify_finding may set VERIFIED
    # to IMPLEMENTED requires non-empty note (what was done)
def implementation_path_for(finding: dict) -> str
    # concrete per-check steps, e.g. broken_link -> identify source/destination ->
    # fix -> deploy -> re-run link check -> verify
def get_fix_plan(store, client_id, plan_id) -> dict
```

## projects.py

```python
PROJECT_STAGES = ["PROSPECT","PROPOSAL","ACTIVE","DELIVERED","MAINTENANCE","CLOSED"]
def create_project(store, client_id, name, website_id=None, audit_id=None, stage="PROPOSAL") -> dict
    # {id:"prj_...", client_id, name, website_id, audit_id, stage, created_at, updated_at}
def get_project(store, client_id, project_id) -> dict
def list_projects(store, client_id) -> list[dict]
def set_project_stage(store, client_id, project_id, stage) -> dict  # validates in PROJECT_STAGES
```

## tasks.py

```python
TASK_STATES = ["TODO","IN_PROGRESS","DONE","BLOCKED"]
def create_task(store, client_id, project_id, title, finding_id=None, notes="") -> dict
    # {id:"tsk_...", client_id, project_id (may be None), finding_id,
    #  title, notes, status:"TODO", created_at, completed_at:None}
    # raises ValueError if project_id given but project does not exist
def complete_task(store, client_id, task_id, note="") -> dict  # DONE + completed_at
def set_task_status(store, client_id, task_id, status) -> dict
def list_tasks(store, client_id, project_id=None) -> list[dict]
def get_task(store, client_id, task_id) -> dict
```

## money.py

```python
MONEY_STATES = ["PAID","OWED","EXPECTED","PARTIALLY_PAID","OVERDUE","NEEDS_VERIFICATION","CANCELLED"]
MONEY_KINDS = ["PROPOSAL","INVOICE","DEPOSIT","PAYMENT","BALANCE"]
class MoneyError(ValueError): ...

def record_event(store, client_id, kind, amount_dollars, status, evidence="",
                 project_id=None, due_date=None, notes="", amount_paid_dollars=0.0) -> dict
    # kind/status validated; amount stored as amount_cents int (round dollars*100)
    # HARD RULE: status in {OWED, EXPECTED, OVERDUE} REQUIRES non-empty evidence,
    #   else raise MoneyError("refusing to manufacture a receivable: ...")
    # PARTIALLY_PAID requires 0 < amount_paid_cents < amount_cents
    # event: {id:"mon_...", client_id, project_id, kind, amount_cents, currency:"USD",
    #         status, evidence, due_date, notes, created_at}
def set_event_status(store, client_id, event_id, new_status, evidence=None) -> dict
    # moving INTO OWED/EXPECTED/OVERDUE requires evidence (arg or existing)
    # moving to PAID requires evidence of payment
def list_events(store, client_id) -> list[dict]
def outstanding_cents(store, client_id) -> int
    # OWED + OVERDUE full amount + PARTIALLY_PAID remainder; ignores others
```

## deadlines.py

```python
def add_deadline(store, client_id, title, due_date, related_type=None, related_id=None, notes="") -> dict
    # due_date "YYYY-MM-DD"; validates format; {id:"ddl_...", ... done:False}
def list_deadlines(store, client_id, include_done=False) -> list[dict]  # sorted by due_date
def mark_done(store, client_id, deadline_id) -> dict
```

## pipeline.py

```python
STAGES = ["LEAD","INTAKE","AUDIT","REPORT","CONVERSATION","PROPOSAL",
          "PROJECT","IMPLEMENTATION","RE-AUDIT","VERIFIED","MAINTENANCE"]
class PipelineError(ValueError): ...
def get_pipeline(store, client_id) -> dict
    # {client_id, stage:"LEAD", history:[{from,to,at,note}]}; auto-creates on first call
def advance(store, client_id, to_stage, note="") -> dict
    # to_stage must be in STAGES; may move forward exactly ONE stage, or
    # backward to ANY earlier stage (rework); skipping forward raises PipelineError.
    # NEVER changes client record_type (no auto lead->client promotion).
```

## reaudit.py

```python
def compare(store, client_id, old_audit_id, new_audit_id) -> dict
    # both audits must be COMPLETE else ValueError
    # match findings by findings.finding_key; returns:
    # {old_audit_id, new_audit_id, at,
    #  resolved:[finding...], new:[finding...], still_open:[finding...],
    #  counts:{resolved,new,still_open},
    #  summary: "X resolved, Y still open, Z new (evidence-based, no invented numbers)"}
def verify_finding(store, client_id, finding_id, new_audit_id) -> bool
    # True + sets finding VERIFIED if its key absent from new audit's findings;
    # False otherwise (finding still present -> stays IMPLEMENTED/whatever it was)
```

## delivery/base.py

```python
class DeliveryError(Exception): ...
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
class DeliveryProvider:
    name = "base"
    def validate_address(self, to):  # raises DeliveryError on bad email
    def send(self, to, subject, body, *, dry_run=True) -> dict:
        raise NotImplementedError
def log_delivery(store, client_id, record: dict) -> dict
    # appends to delivery_log.json: {provider,to,subject,status,at}
def get_delivery_log(store, client_id) -> list[dict]
```

## delivery/gmail.py

```python
class GmailDelivery(DeliveryProvider):
    name = "gmail"
    def send(self, to, subject, body, *, dry_run=True) -> dict
        # validate_address first
        # dry_run=True -> {"provider":"gmail","to":to,"subject":subject,
        #                  "status":"DRY_RUN","at":...}  (NO email sent)
        # dry_run=False -> subprocess.run(
        #     ["hatch_gws_cli","gmail","+send","--to",to,"--subject",subject,"--body",body],
        #     capture_output=True, text=True, timeout=60, check=False)
        #   returncode 0 -> status SENT; else raise DeliveryError(stderr tail)
```

## delivery/swiftsend.py

```python
class SwiftSendDelivery(DeliveryProvider):
    name = "swiftsend"
    def send(self, to, subject, body, *, dry_run=True) -> dict:
        raise NotImplementedError(
          "SwiftSend delivery is BLOCKED: no SwiftSend API exists in this workspace. "
          "Needed before implementation: API base URL, auth scheme, and endpoint "
          "contract from Kiminou's mom.")
```

`delivery/__init__.py` exports `DeliveryProvider, DeliveryError, GmailDelivery,
SwiftSendDelivery, log_delivery, get_delivery_log`.

## templates/__init__.py

```python
TEMPLATES = ["audit_received","audit_complete","report_ready","fix_plan_ready",
             "project_ready","fixes_completed","reaudit_complete"]
def render(name: str, variables: dict) -> str
    # loads templates/<name>.txt; substitutes {{var}}; raises ValueError
    # listing any missing variables; raises KeyError on unknown template name
```

Each .txt template: professional, concise, NO hard-coded customer data, uses
`{{customer_name}} {{business_name}} {{website}} {{audit_date}} {{report_link}}
{{next_step}}` etc. as appropriate.

## cli.py

argparse, prog `aafc`. Global flags: `--data DIR` (store root), `--json`
(machine-readable output). Subcommands (all call the modules above):

```
client add --name --email [--business --phone --notes --source]
client list | client show <id> | client promote <id>
website add --client <id> --url [--label] [--authorize]
website list --client <id> | website authorize --client <id> --website <id>
audit run --client <id> --website <id> [--pages N]
audit list --client <id> [--website <id>] | audit show --client <id> --audit <id>
checks list
report generate --client <id> --audit <id> [--out PATH]
report verify --client <id> --old <audit> --new <audit> [--out PATH]
fixplan create --client <id> --audit <id> [--project <id>]
fixplan advance --client <id> --finding <id> --to <STATE> [--note TEXT]
project create --client <id> --name [--website <id>] [--audit <id>] [--stage S]
project list --client <id> | project stage --client <id> --project <id> --to <STAGE>
task add --client <id> --title [--project <id>] [--finding <id>] [--notes TEXT]
task list --client <id> [--project <id>] | task complete --client <id> --task <id> [--note TEXT]
money record --client <id> --kind KIND --amount DOLLARS --status STATE --evidence TEXT [--project <id>] [--due YYYY-MM-DD] [--notes TEXT]
money list --client <id> | money outstanding --client <id> | money set-status --client <id> --event <id> --to STATE [--evidence TEXT]
deadline add --client <id> --title --due YYYY-MM-DD [--notes TEXT]
deadline list --client <id> [--all] | deadline done --client <id> --deadline <id>
pipeline show --client <id> | pipeline advance --client <id> --to STAGE [--note TEXT]
reaudit compare --client <id> --old <audit> --new <audit>
deliver --client <id> --to EMAIL --template NAME --provider gmail|swiftsend [--subject TEXT] [--var k=v ...] [--send]
    # default is DRY RUN; --send performs the real send (gmail only; swiftsend raises)
```

Errors: print `ERROR: <message>` to stderr, exit code 1. Success: human-readable
summary; with `--json`, print the returned dict as JSON.

## Global rules for ALL agents

1. Python 3.12, stdlib + `requests` only (runtime deps). Type hints everywhere.
2. Docstrings on every public function.
3. NO personal data anywhere: no Kiminou, no real emails, no Plan A, no
   Mission Ledger, no #owed, no real clients/projects. Tests use fictitious
   clients ("Acme Bakery", "Blue River Plumbing").
4. Atomic writes via store.write_json. Never hand-roll JSON persistence.
5. No network calls except the auditor engine and GmailDelivery real-send path.
6. No secrets in code. No real emails sent in tests (dry_run only).
7. Money HARD RULE (above) is non-negotiable.
8. Failed audits (0 pages crawled) -> status FAILED; reports refuse them.
9. Keep it small and honest. No placeholders, no TODO stubs (except the
   SwiftSend stub, which is REQUIRED to be explicit).

---

## EXTENSION LAYER (additive; base modules untouched)

New modules: `customers.py`, `sources.py`, `footprint.py`,
`social_audit.py`, `crosscheck.py`, `services.py`, `opportunities.py`,
plus `data/services.yaml`. Runtime deps are now stdlib + `requests` +
`pyyaml` (`pyyaml>=6` added to requirements.txt). Money rule: extension
modules NEVER import or call `money.py` (recommendation/proposal !=
revenue); `customers.get_customer_file` reads `money.json` directly,
read-only, only to include existing money events in the cabinet view.

Network note (extends base rule 5): `footprint.discover` and
`social_audit.audit_social` make READ-ONLY public GET requests to the
customer's own public website/profiles. No posts, no private analytics,
nothing fabricated; fetch failures degrade honestly to NOT FOUND/ERROR.

### customers.py (email is the key)

```python
def resolve_customer(store, email) -> {"resolution": "FOUND"|"NEW", "email": normalized, "customer": cabinet|None}
    # uses clients.find_client_by_email (case-insensitive). Missing data stays None/"UNKNOWN".
def register_customer(store, name, email, business_name=None, phone=None, source="UNKNOWN", source_detail="", notes="") -> dict
    # minimal collection: creates base client (record_type LEAD) + sources.set_source + empty
    # offerings.json/communications.json + logs history "customer_registered". Dedupes by email.
def get_customer_file(store, email_or_client_id) -> dict
    # read-only assembled filing cabinet: identity{name,email}, contact{phone},
    # source{source,detail}, business{business_name}, websites[], social_profiles[],
    # offerings[], communications[], audits[], findings[] (all audits), opportunities[],
    # proposals[] (projects stage PROPOSAL), projects[], deliverables[] (reports),
    # money[], history[]. Never restructures base files.
def log_history(store, client_id, event, details="") -> dict
    # append-only history.json [{at, event, details}]. Events used:
    # customer_registered, source_set, footprint_discovered, social_audited,
    # crosschecked, opportunity_map_built, opportunity_status_changed, intake.
def self_service_intake(store, name, email) -> {"resolution","customer","primary_website"|None,"needs":[]|["website"]}
    # FOUND: loads cabinet + primary website (first website), needs=[].
    # NEW: registers minimal, needs=["website","authorization"].
def add_offering(store, client_id, name, status="current"|"past", notes="") -> dict
    # appends offerings.json [{id:"off_...", name, status, notes, added_at}].
def log_communication(store, client_id, channel, direction, subject, body_ref="") -> dict
    # appends communications.json [{id:"com_...", channel, direction, subject, body_ref, logged_at}];
    # body_ref only -- never full secrets.
```

### sources.py (no guessing)

```python
SOURCES = ["referral","website","email","social","campaign","outreach","existing-customer","direct","other","UNKNOWN"]
def set_source(store, client_id, source, detail="", force=False) -> dict
    # validates against SOURCES. PERMANENT: a non-UNKNOWN source that differs raises
    # ValueError unless force=True. Logs history "source_set".
def get_source(store, client_id) -> {"source","detail","set_at"}   # default UNKNOWN
```

### footprint.py (public web only)

```python
PLATFORMS = {"instagram","facebook","linkedin","youtube","tiktok","x","google_business","directories"}
    # each maps to domain patterns for link extraction; google_business/directories have none.
def discover(store, client_id, website_id) -> list[dict]
    # own tiny requests.get (UA + timeout; does NOT import auditor's private fetch).
    # Extracts <a href> links matching platform domains + rel="me" links; counts
    # mailto:/tel: contact signals. Per platform returns
    # {"platform","url"|None,"status":"PUBLICLY VERIFIED"|"REQUIRES ACCOUNT ACCESS"|"NOT FOUND",
    #  "confidence":"CONFIRMED"|"LIKELY"|"UNVERIFIED","evidence":str}.
    # Homepage link = CONFIRMED/PUBLICLY VERIFIED with the link as evidence.
    # No link = NOT FOUND ("no link to <platform> found on homepage").
    # google_business/directories = NOT FOUND ("no search API configured; manual lookup required").
    # Fetch failure -> all NOT FOUND with the fetch error as evidence. Saves to social.json;
    # logs "footprint_discovered".
```

### social_audit.py (per PUBLICLY VERIFIED property)

```python
def audit_social(store, client_id) -> list[dict]
    # standard finding schema (check="social_<platform>_<aspect>", id f_<hex>).
    # Per property: reachable (GET profile; blocked -> result ERROR, severity LOW,
    # confidence NEEDS MANUAL REVIEW, evidence = HTTP status/block note -- never faked);
    # website_linkback (only if fetch succeeded); branding/bio (only from fetched HTML).
    # Results stored under each profile's "last_audit" in social.json.
    # No verified properties -> [] (honest, not failure). Logs "social_audited".
```

### crosscheck.py (first-class findings, check names prefixed "xcheck_")

```python
XCHECK_NAMES = ["xcheck_social_links_missing","xcheck_social_no_website_linkback",
                "xcheck_cta_no_social_path","xcheck_contact_signals_missing"]
def crosscheck(store, client_id) -> list[dict]
    # inputs: latest COMPLETE website audit findings, social.json, websites list.
    # Emits ONLY evidence-backed relationships, each with confidence LIKELY and
    # evidence citing both sides. Appends {"at","findings"} to crosscheck.json;
    # logs "crosschecked".
def latest_crosscheck_findings(store, client_id) -> list[dict]
```

### services.py + data/services.yaml (grounded in reality only)

13 services: website-audit, website-design-build, website-repair, seo,
aeo-discoverability, accessibility-remediation, conversion-optimization,
analytics-setup, lead-capture-forms, email-automation, social-media-system,
content-system, maintenance-retainer. Each entry: name, tagline, covers[],
evidence_triggers[] (REAL check names only -- verified against engine.py:
https, hsts, indexability, title, meta_description, viewport, charset, h1,
heading_order, img_alt, canonical, open_graph, structured_data, lang,
mixed_content, forms, cta, page_weight, ttfb, robots_txt, sitemap,
broken_links, fetch, http_status, parse -- plus crosscheck XCHECK_NAMES;
corrected spec guesses: img_alt not alt_text, broken_links not broken_link,
robots_txt not robots, page_weight/ttfb not performance), implementation_path,
pricing_model one-time|recurring. `load_catalog()` validates every trigger
against KNOWN_CHECKS (ValueError on unknown). Services with empty triggers
(analytics-setup, email-automation) are honest about being undetectable and
never evidence-match.

```python
def load_catalog() -> list[dict]      # yaml.safe_load of data/services.yaml (path relative to package file)
def list_services() -> list[dict]
def get_service(slug) -> dict         # KeyError on unknown slug
def match_services(findings) -> [{"service":slug,"triggered_by":[finding ids],"why":str}]
    # matches ONLY when >=1 finding with result FAIL|ERROR has a check in evidence_triggers.
    # PASS findings never trigger a service.
```

### opportunities.py (evidence-gated; recommendation != revenue)

```python
OPPORTUNITY_STATES = ["DISCOVERED","RECOMMENDED","DISCUSSION","PROPOSAL","APPROVED",
                      "IN PROGRESS","COMPLETED","VERIFIED","DECLINED","NOT CURRENTLY RELEVANT"]
def build_opportunity_map(store, client_id, current_offering=None) -> dict
    # {"customer_id","built_at","current_offering","opportunities":[...]}.
    # Gathers latest COMPLETE audit findings + social findings + crosscheck findings,
    # runs services.match_services, one opportunity per matched service:
    # {id "opp_<hex>", customer_id, service, problem, evidence, current_offering,
    #  additional_offering (service name), why_it_fits, aafc_capability,
    #  implementation_path, pricing_model, status "DISCOVERED", triggering_findings [ids]}.
    # Saves to opportunities.json; dedupe = same service + same triggering findings.
    # Logs "opportunity_map_built". NEVER touches money.
def expand_offerings(store, client_id, current_offering) -> {"current_offering","supported_expansions":[{"service","triggered_by_findings","why_it_fits"}], "note"}
    # ONE->MANY: every catalog service with >=1 triggering finding listed with real
    # finding ids (current offering excluded). Empty -> note
    # "No catalog service is evidence-supported for expansion right now." Never padded.
def set_opportunity_status(store, client_id, opp_id, status) -> dict
    # validates status in OPPORTUNITY_STATES; logs "opportunity_status_changed".
def get_opportunities(store, client_id, status=None) -> list[dict]
```

## CLI EXTENSION (additive; existing commands unchanged)

New subcommands on the existing argparse parser (added 2026-09-26;
extension-layer modules only — `aafc_engine/cli.py` was extended
additively, no existing command was modified):

```
customer resolve --email EMAIL
    # FOUND (with key cabinet facts: business, source, website/audit/
    # opportunity counts) or NEW (prints what is minimally needed:
    # name and website).
customer register --email EMAIL --name NAME [--business B] [--phone P] [--source S] [--notes N]
    # register_customer (dedups by email).
customer file --email EMAIL
    # readable cabinet summary (counts per section); with --json prints
    # the full cabinet.
footprint --email EMAIL [--website ID]
    # footprint.discover on the primary website (first on file) or the
    # given website id; prints platform/status/confidence/evidence table.
audit-full --email EMAIL [--website ID] [--pages N]
    # registry.run_audit + social_audit.audit_social + crosscheck.crosscheck;
    # prints summary counts. Refuses gracefully (ERROR, exit 1) when the
    # website is not authorized.
opportunities --email EMAIL [--current-offering SLUG]
    # build_opportunity_map; prints per opportunity: service, problem,
    # evidence snippet, pricing_model, status.
opportunity-map --email EMAIL [--current-offering SLUG]
    # builds the map and writes opportunity-map-<YYYYMMDD-HHMMSS>.md into
    # the client directory; prints the path. Sections: customer/business/
    # website/date, current offering, opportunities (problem, evidence,
    # additional offering, why it fits, AAFC capability, implementation
    # path, one-time/recurring, status), expansions NOT evidence-supported
    # ("none — no padded items" when empty).
```

Conventions unchanged: `--data DIR` / `--json` globals, human summary or
JSON output, `ERROR: <msg>` to stderr with exit code 1 on failure.
