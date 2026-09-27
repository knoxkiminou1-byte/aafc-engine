"use strict";
/* AAFC Quick Audit frontend — no frameworks, no analytics, no tracking. */

const form = document.getElementById("audit-form");
const urlInput = document.getElementById("url");
const goBtn = document.getElementById("go");
const statusEl = document.getElementById("status");
const reportEl = document.getElementById("report");

function showStatus(html) {
  statusEl.innerHTML = html;
  statusEl.classList.remove("hidden");
}
function hideReport() {
  reportEl.classList.add("hidden");
  reportEl.innerHTML = "";
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const SEV_LABEL = { critical: "Critical", warning: "Warning", info: "Improvement" };

function findingHTML(f) {
  const sev = SEV_LABEL[f.severity] || f.severity;
  return (
    '<article class="finding">' +
    "<h3><span class='sev " + esc(f.severity) + "'>" + esc(sev) + "</span>" +
    esc(f.title) + "</h3>" +
    "<p><strong>What we found:</strong> " + esc(f.evidence) + "</p>" +
    "<p><strong>Why it matters:</strong> " + esc(f.why_it_matters) + "</p>" +
    "<p><strong>The fix:</strong> " + esc(f.recommended_fix) + "</p>" +
    "<p class='page'>Seen on: " + esc(f.page) +
    (f.page_kind && f.page_kind !== "content" ? " (" + esc(f.page_kind) + " page)" : "") +
    "</p></article>"
  );
}

function renderReport(payload) {
  const r = payload.result || {};
  const status = (payload.status || r.status || "UNKNOWN").toUpperCase();
  const badgeClass = status === "COMPLETE" ? "complete" : status === "PARTIAL" ? "partial" : "blocked";
  const SEV_ORDER = { critical: 0, warning: 1, info: 2 };
  const findings = (r.findings || []).slice().sort((a, b) =>
    (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3));
  // Count what the customer actually sees: every rendered finding card.
  // (The API's counts[] only covers score-driving findings; using it here
  // made the summary undercount the cards on screen.)
  const nCrit = findings.filter(f => f.severity === "critical").length;
  const nWarn = findings.filter(f => f.severity === "warning").length;
  const nInfo = findings.filter(f => f.severity === "info").length;

  let html = "<h2>Audit report</h2>";
  html += "<p><span class='badge " + badgeClass + "'>" + esc(status) + "</span> " +
          "<span class='page'>" + esc(r.audited_url || "") + "</span></p>";

  if (r.score == null) {
    html += "<p><strong>No score this run.</strong> " +
      "The site couldn't be loaded, so there was nothing to grade — fix the loading problem and run it again.</p>";
  } else {
    html += "<div class='score'><span class='n'>" + esc(r.score) + "<small>/100</small></span>";
    if (r.grade) html += "<span class='grade'>" + esc(r.grade) + "</span>";
    else html += "<span class='grade'>–</span>";
    html += "</div>";
    if (status === "PARTIAL") {
      html += "<p><em>Partial audit:</em> this audit couldn't check everything it set out to " +
              "— the time budget ran out or some pages refused automated checks — " +
              "so this score covers only the pages completed and the final grade is withheld. " +
              "Run the full CLI audit for the complete result.</p>";
    }
  }

  html += "<p>Checked <strong>" + esc(r.pages_crawled) + "</strong> page(s) in " +
          esc(r.elapsed_total) + "s — <strong>" + esc(nCrit) + "</strong> critical, " +
          "<strong>" + esc(nWarn) + "</strong> warnings, <strong>" + esc(nInfo) +
          "</strong> improvements.</p>";

  if (findings.length) {
    html += "<h2>Findings</h2>" + findings.map(findingHTML).join("");
  } else if (r.score != null) {
    html += "<p>Good news: nothing urgent turned up.</p>";
  }

  if (r.notes && r.notes.length) {
    html += "<h2>Notes</h2><ul class='notes'>" +
      r.notes.map(n => "<li>" + esc(n) + "</li>").join("") + "</ul>";
  }
  html += "<footer>Automated first pass by the AAFC audit engine (Quick Audit mode). " +
          "A human review should confirm critical findings before acting on them.</footer>";

  reportEl.innerHTML = html;
  reportEl.classList.remove("hidden");
  reportEl.scrollIntoView({ behavior: "smooth", block: "start" });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;
  goBtn.disabled = true;
  hideReport();
  showStatus("<span class='spin'></span> Auditing <strong>" + esc(url) +
             "</strong>… this takes up to ~40 seconds.");

  try {
    const resp = await fetch("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showStatus("<strong>Couldn't run the audit:</strong> " + esc(payload.error || ("HTTP " + resp.status)));
      return;
    }
    statusEl.classList.add("hidden");
    renderReport(payload);
  } catch (err) {
    showStatus("<strong>Couldn't run the audit:</strong> " + esc(err.message || err));
  } finally {
    goBtn.disabled = false;
  }
});
