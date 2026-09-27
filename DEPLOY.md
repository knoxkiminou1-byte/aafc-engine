# DEPLOY.md — AAFC Engine → Vercel

How to take this repo from GitHub to a live hosted **Quick Audit** on
Vercel, and what the hosted version can and cannot do.

## What gets deployed

* **Frontend** — `web/` (static): URL input, running state, per-page
  client-friendly report. No analytics, no tracking, no cookies.
* **API** — `api/index.py` (Python serverless function): drives the REAL
  audit engine (`aafc_engine.auditor.engine`), not a mock.
  * `POST /api/audit` with `{"url": "https://example.com"}` → runs the
    audit synchronously and returns `{id, status, mode, result}`.
  * `GET /api/audit?id=<id>` → returns the cached result **on the same
    warm instance**, else an honest 404 (see limits).
* **Engine** — `aafc_engine/` runs HTTP-only on Vercel (no Playwright in
  the serverless bundle, by design).

## Prerequisites

* The repo is published on GitHub (this step is done by the repo owner):
  `https://github.com/knoxkiminou1-byte/aafc-engine`
* A Vercel account with permission to **Add New → Project**.
* No environment variables, no secrets, no paid services are required.

## Exact deployment steps

1. In Vercel: **Add New → Project**.
2. **Import** `knoxkiminou1-byte/aafc-engine` (the GitHub repo above).
   Do not import any other repo, and do not change any other project.
3. On the **Configure Project** screen:
   * **Framework Preset:** `Other`
   * **Root Directory:** `./` (the repository root — leave as-is)
   * **Build Command:** leave empty (none needed)
   * **Output Directory:** `web`
   * **Install Command:** leave default (`pip install -r requirements.txt`)
   * **Environment Variables:** none
4. **Deploy.**
5. When the build finishes, open the production URL and verify:
   * The homepage loads: title "AAFC Website Health Check".
   * Enter `https://example.com`, click **Run audit**, and wait.
     Within ~40 seconds you should see a real report (status, score,
     findings). `example.com` scores low on purpose — it has almost no
     content — which proves the engine really ran.
   * `POST /api/audit` with a bad URL (`{"url": "not a url"}`) returns
     HTTP 400 with an error message (not a crash page).

## Known limits (read before demoing to a client)

* **60-second function cap (Hobby).** The audit enforces its own
  **~40-second internal budget** and max **3 pages** server-side
  (`QUICK_MAX_PAGES` / `QUICK_TIME_BUDGET` in `api/index.py`). If the
  budget is exceeded the API returns `status: "PARTIAL"` with whatever
  completed — the grade is withheld, never faked.
* **HTTP-only on Vercel.** Rendered-DOM verification needs headless
  Chromium, which does not ship in the serverless bundle. The API result
  says so explicitly (`result.render.mode == "http_only_requested"`,
  plus a notes line). Findings that need rendered-DOM confirmation are
  reported as needing review, never scored.
* **Job ids are warm-instance only.** `GET /api/audit?id=…` resolves
  only on the instance that ran the audit (in-memory cache). After a
  cold start it returns an honest 404 telling you to re-run the POST.
  The UI uses the POST response directly, so this never affects it.
* **No persistence.** The hosted path stores nothing: no client
  records, no audit history, no cookies. Client isolation holds
  trivially — there is nothing to leak between visitors.
* **Full audits stay on the CLI.** The six-page rendered audit
  (`aafc audit run`, default `--render` when Chromium is installed)
  is unchanged and is the tool for real client delivery.

## Local development

```bash
cd aafc-engine
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-local.txt
.venv/bin/python -m playwright install chromium   # one-time, for the rendered path
.venv/bin/python -m pytest tests/ -q               # full suite
.venv/bin/python api/index.py                      # local API+frontend on http://127.0.0.1:8000
./aafc audit run --client <id> --website <id>      # full rendered CLI audit
./aafc checks capabilities                         # renderer + runtime report
```

## Troubleshooting

* **Build fails on `pip install`:** `requirements.txt` is a fully pinned
  lock (direct + transitive deps). Direct deps live in `requirements.in`;
  recompile with `pip install -r requirements.in && pip freeze > requirements.txt`.
  Confirm the Install Command is the default and that no extra requirements
  file was referenced. The lock was built on Python 3.12 — match Vercel's
  Python runtime version.
* **Audit returns 500:** the API never leaks tracebacks; check the
  Vercel function logs for the exception message.
* **"Job not found" on GET:** expected after a cold start — re-run POST.
* **A page that needs JavaScript scores oddly on hosted:** expected —
  hosted is HTTP-only; run the CLI rendered audit for the real verdict.
