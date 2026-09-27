"""AAFC Engine — hosted Quick Audit API (Vercel serverless).

This module drives the REAL audit engine (``aafc_engine.auditor.engine``) —
no mocks, no stubs. It is deployed as a Vercel Python serverless function
(``api/index.py`` serves all ``/api/*`` routes) and is equally importable
locally, which is how the end-to-end test suite exercises it.

Hosted mode is an explicitly labeled **Quick Audit**:

* max 3 pages
* ~40 second internal time budget
* HTTP-only (no headless Chromium ships in the serverless bundle)

A budget overrun returns ``status="PARTIAL"`` with whatever completed —
never a fake full grade. The full six-page rendered audit remains available
through the CLI (``aafc audit run``).

Deploy-readiness rules honored here:

* stdlib + ``flask`` + ``aafc_engine`` only — Playwright is never imported
  on this path (the engine degrades to HTTP-only on its own when the
  renderer is unavailable).
* All paths are relative to this project (``Path(__file__)``); nothing
  reads or writes outside the project except the in-memory job cache.
  No disk writes happen at runtime.
* No localhost assumptions: the frontend calls relative ``/api/audit``
  URLs; the audit target is any public http(s) URL.
* Minimal SSRF guard: only ``http``/``https`` targets whose resolved IPs
  are all globally routable are fetched — enforced for the initial URL and
  for every redirect target via ``engine.FETCH_GUARD``. Private/loopback/
  link-local targets are rejected with 400 (direct) or recorded as blocked
  fetches (redirects).
* No analytics, no tracking, no secrets. No AI/Muse API is used at runtime;
  the engine is deterministic checks only on this path.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

from aafc_engine.auditor import engine

# ---------------------------------------------------------------------------
# Quick-audit limits (enforced server-side; client input cannot widen them)
# ---------------------------------------------------------------------------
QUICK_MAX_PAGES = 3
QUICK_TIME_BUDGET = 40.0  # seconds; Vercel Hobby functions cap at 60s
# Hard wall-clock cap for the whole audit call. The engine's own budget
# (above) handles every *normal* slow case; this backstop covers the
# pathological ones a per-request timeout cannot touch — a DNS resolver
# that never answers (socket.getaddrinfo has no timeout parameter) or a
# WAF that tarpits the connection by dribbling bytes slower than the
# read timeout. Without it the function dies at Vercel's 60s kill and the
# user gets a bare 504 instead of an honest PARTIAL. Must stay < 60.
QUICK_HARD_CAP = 52.0
_DNS_TIMEOUT = 5.0
_DNS_TTL = 300.0

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = Flask(__name__)

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 128


# ---------------------------------------------------------------------------
# URL validation + SSRF guard
# ---------------------------------------------------------------------------
def _normalize_target(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Provide a website URL to audit.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must be a valid http(s) address, e.g. https://example.com")
    return raw


# ---------------------------------------------------------------------------
# DNS with a timeout (the SSRF guard's other half)
# ---------------------------------------------------------------------------
_dns_cache: dict[str, tuple[float, set[str] | None]] = {}
_dns_lock = threading.Lock()


def _resolve_ips(hostname: str) -> set[str] | None:
    """Resolve a hostname to its IP set, bounded by _DNS_TIMEOUT.

    ``socket.getaddrinfo`` takes no timeout, so a wedged resolver would hang
    the audit (and the serverless function) forever — this runs it on a
    daemon thread and gives up after _DNS_TIMEOUT seconds. Results are
    cached briefly; a failure/timeout caches as None so one bad resolver
    cannot be hammered per fetch. Returns None when the host cannot be
    resolved (treated as not-public by the guard: fail closed).
    """
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(hostname)
        if hit and hit[0] > now:
            return hit[1]
    box: dict = {}

    def _do() -> None:
        try:
            box["infos"] = socket.getaddrinfo(hostname, None)
        except OSError as exc:
            box["error"] = exc

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(_DNS_TIMEOUT)
    infos = box.get("infos")
    if infos:
        ips = {info[4][0] for info in infos}
        with _dns_lock:
            _dns_cache[hostname] = (now + _DNS_TTL, ips)
        return ips
    with _dns_lock:
        _dns_cache[hostname] = (now + 30.0, None)
    return None


def _host_is_public(hostname: str) -> bool:
    """True when every resolved IP for the host is globally routable.

    Blocks loopback, private, link-local, multicast, reserved and
    unspecified addresses so the public endpoint cannot be used to probe
    internal networks (SSRF). Unresolvable hosts fail closed (False).
    """
    ips = _resolve_ips(hostname)
    if not ips:
        return False
    for ip in ips:
        try:
            if not ipaddress.ip_address(ip).is_global:
                return False
        except ValueError:
            return False
    return True


def _validate_target(raw: str) -> str:
    url = _normalize_target(raw)
    hostname = urlparse(url).hostname or ""
    if not _host_is_public(hostname):
        raise ValueError(
            "That host does not resolve to a public address — "
            "the hosted audit only accepts public websites."
        )
    return url


def _fetch_guard(url: str) -> None:
    """Engine fetch guard: reject any non-public target.

    Enforced for the initial URL and for every redirect target (the engine
    follows redirects automatically — see ``engine.FETCH_GUARD``). Raises
    ``ValueError`` on a blocked URL, which the engine converts into a failed
    fetch rather than following it.
    """
    hostname = urlparse(url).hostname or ""
    if not _host_is_public(hostname):
        raise ValueError(f"non-public fetch target blocked: {hostname or url}")


# The guard is enforced per audit call via engine.fetch_guard() below — never
# set module-globally at import time, so importing this module (CLI,
# tests) cannot change engine behavior for other callers. The CLI never
# sets it, so local/private audits keep working there.


# ---------------------------------------------------------------------------
# The real engine call
# ---------------------------------------------------------------------------
def _timeout_partial(url: str) -> dict:
    """Honest PARTIAL envelope when the audit wedges past QUICK_HARD_CAP.

    No score, no grade, no fabricated findings — just the truth: the site
    stopped responding mid-audit (tarpitted connection or wedged resolver),
    so nothing was verified. The caller must never present this as a result.
    """
    finding = {
        "page": url, "check": "timeout", "severity": "warning",
        "title": "Audit timed out — the site stopped responding",
        "evidence": (f"The audit did not finish within {QUICK_HARD_CAP:.0f}s: "
                     "the site's server stopped answering mid-audit "
                     "(connection tarpitted or DNS wedged)."),
        "why_it_matters": ("If an automated check cannot get a timely answer "
                           "from the site, some visitors and search engines "
                           "cannot either — every one of those visits is lost."),
        "recommended_fix": ("Check that the site (or its firewall/CDN) is not "
                            "rate-limiting or tarpitting automated checks, "
                            "then re-run the audit."),
        "technical": f"hard_cap={QUICK_HARD_CAP}s exceeded; no page verified",
        "page_kind": "content", "verification": "HTTP_FETCH",
        "review_hint": False,
    }
    return {
        "audited_url": url,
        "engine": "kiminou-website-audit/1.0",
        "status": "PARTIAL",
        "pages_crawled": 0,
        "score": None,
        "grade": None,
        "counts": {"critical": 0, "warning": 1, "info": 0,
                   "utility_page_findings_excluded_from_score": 0,
                   "unverifiable_findings_excluded_from_score": 0},
        "findings": [finding],
        "mode": "quick",
        "notes": ["Audit hit the hosted time cap before any page was verified; "
                  "run the full CLI audit (aafc audit run) for the complete result."],
    }


def run_quick_audit(url: str,
                    max_pages: int = QUICK_MAX_PAGES,
                    time_budget: float = QUICK_TIME_BUDGET) -> dict:
    """Run a genuine quick audit with the real engine and return its result.

    HTTP-only by design on this path (no Chromium in serverless). The
    engine dict is returned as-is, plus a ``mode``/``limits`` envelope so
    clients can tell a quick audit from the full CLI audit.

    The SSRF fetch guard is enforced for every fetch the engine performs
    here (initial URL and all redirect targets).

    The engine runs on a watchdog thread bounded by QUICK_HARD_CAP: if a
    pathological target (tarpitted socket, wedged DNS) defeats the engine's
    own per-request budget, the caller still gets an honest PARTIAL instead
    of a bare 504 from the platform killing the function.
    """
    box: dict = {}
    done = threading.Event()

    def _work() -> None:
        try:
            with engine.fetch_guard(_fetch_guard):
                box["result"] = engine.audit_site(
                    url,
                    max_pages=min(max_pages, QUICK_MAX_PAGES),
                    render=False,  # hosted path never ships a browser
                    time_budget=min(time_budget, QUICK_TIME_BUDGET),
                )
        except Exception as exc:  # never let the worker die silently
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    finished = done.wait(QUICK_HARD_CAP)
    if finished and "result" in box:
        result = box["result"]
    elif finished:
        raise box.get("error", RuntimeError("audit worker failed"))
    else:
        # Worker is wedged (daemon thread: cannot block the response and
        # dies with the instance). Report PARTIAL honestly.
        result = _timeout_partial(url)
    result["mode"] = "quick"
    result["limits"] = {
        "max_pages": QUICK_MAX_PAGES,
        "time_budget_seconds": QUICK_TIME_BUDGET,
        "render": "http_only",
        "note": ("Hosted Quick Audit: up to 3 pages, ~40s budget, HTTP-only. "
                 "For the full 6-page rendered audit, run the CLI: aafc audit run."),
    }
    return result


def _remember_job(result: dict) -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        if len(_JOBS) >= _MAX_JOBS:
            _JOBS.pop(next(iter(_JOBS)))
        _JOBS[job_id] = result
    return job_id


def get_job(job_id: str) -> dict | None:
    """Return a cached quick-audit result, or None.

    The cache is in-memory only: on a cold start (or a different
    instance) a previous job id will not resolve. Callers must say so
    honestly instead of pretending the job is still running.
    """
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/api/audit", methods=["POST"])
def api_audit_post():
    data = request.get_json(force=False, silent=True) or {}
    raw_url = data.get("url", "")
    try:
        url = _validate_target(raw_url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        result = run_quick_audit(url)
    except Exception as exc:  # never leak a traceback; never fake a result
        return jsonify({"error": f"Audit failed: {exc}"}), 500
    job_id = _remember_job(result)
    # Synchronous by design: the audit completed in this invocation, so the
    # response carries the job id, the status, and the full result together.
    return jsonify({
        "id": job_id,
        "status": result["status"],
        "mode": "quick",
        "result": result,
    })


@app.route("/api/audit", methods=["GET"])
def api_audit_get():
    job_id = (request.args.get("id") or "").strip()
    if not job_id:
        return jsonify({"error": "Pass ?id=<job id> from a POST /api/audit response."}), 400
    result = get_job(job_id)
    if result is None:
        return jsonify({
            "error": ("Job not found. Results are kept in memory on the "
                      "instance that ran the audit — after a cold start or "
                      "on another instance the id no longer resolves. "
                      "Re-run POST /api/audit for a fresh result."),
        }), 404
    return jsonify({"id": job_id, "status": result["status"],
                    "mode": "quick", "result": result})


# ---------------------------------------------------------------------------
# Local-dev static serving (on Vercel the /web output directory serves these
# statically before any function is hit; these routes only matter locally)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    if filename.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(WEB_DIR, filename)


if __name__ == "__main__":  # local dev only; Vercel provides the server
    app.run(host="127.0.0.1", port=8000)
