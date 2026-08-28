(function () {
  "use strict";

  var ctx = window.APP_CTX || {};
  var authHeaders = ctx.api_key ? { "X-API-Key": ctx.api_key } : {};

  // The Mask Profile tab's setup below runs before the User Settings tab's
  // (script order), and an uncaught error partway through would abort the
  // whole IIFE -- silently leaving the settings form's submit listener
  // never attached (typing a password then clicking Save would then just
  // do nothing, no request ever sent, and no error visible). Wrap each
  // tab's setup independently so a bug in one can never take out the other.
  try {

  // ── Mask Profile tab ──────────────────────────────────────────────────

  var jaIds = document.getElementById("jaIds");
  var readyCard = document.getElementById("readyCard");
  var readyCount = document.getElementById("readyCount");
  var manualEntry = document.getElementById("manualEntry");
  var maskBtn = document.getElementById("maskBtn");
  var maskSpinner = document.getElementById("maskSpinner");
  var maskSummary = document.getElementById("maskSummary");
  var statusBody = document.getElementById("statusBody");

  function parseIds(raw) {
    // MassMaskingController.generatemassmasking() (Apex) joins ids with ';'
    // (String.join(ids, ';')); manual paste may use commas/whitespace/newlines
    // instead -- accept all of them.
    return (raw || "")
      .split(/[\s,;]+/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  // Salesforce launched this page with pre-selected records (prefill_ids set)
  // -> show the summary card, keep the raw textarea hidden (still holds the
  // ids for the POST body). Otherwise this is a manual/standalone open ->
  // show the textarea for pasting ids by hand.
  var prefillIds = parseIds(ctx.prefill_ids);
  if (prefillIds.length > 0) {
    readyCard.classList.remove("d-none");
    readyCount.textContent = String(prefillIds.length);
  } else {
    manualEntry.classList.remove("d-none");
    jaIds.classList.remove("d-none");
  }

  function renderStatusRows(results) {
    statusBody.innerHTML = "";
    if (!results || results.length === 0) {
      statusBody.innerHTML = '<tr><td colspan="3" class="empty-row">No profiles masked yet.</td></tr>';
      return;
    }
    results.forEach(function (r) {
      var tr = document.createElement("tr");
      var ok = r.result && r.result.status === "ok";
      var detail = ok
        ? "Masked ContentVersion: " + (r.result.masked_content_version_id || "")
        : (r.result && r.result.detail) || "Unknown error";
      tr.innerHTML =
        "<td>" + escapeHtml(r.job_applicant_id) + "</td>" +
        '<td><span class="status-badge ' + (ok ? "ok" : "error") + '">' + (ok ? "Masked" : "Failed") + "</span></td>" +
        "<td>" + escapeHtml(detail) + "</td>";
      statusBody.appendChild(tr);
    });
  }

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = String(s == null ? "" : s);
    return div.innerHTML;
  }

  function setMaskBusy(busy) {
    maskBtn.disabled = busy;
    maskSpinner.classList.toggle("d-none", !busy);
  }

  // LibreOffice conversion alone measures ~2-4s per .docx resume (real
  // measurement, not an estimate); add Salesforce round-trips and upload
  // time and a full 200-item batch in ONE blocking request can run well
  // past any reasonable HTTP timeout, hanging with nothing to show for it.
  // Chunking keeps each request's wall-clock time bounded, gives visible
  // incremental progress, and means a failure partway through doesn't
  // lose the results already completed.
  var MASK_CHUNK_SIZE = 5;

  function chunk(arr, size) {
    var out = [];
    for (var i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
    return out;
  }

  maskBtn.addEventListener("click", async function () {
    var ids = parseIds(jaIds.value);
    maskSummary.classList.remove("d-none", "summary-error");

    if (ids.length === 0) {
      maskSummary.classList.add("summary-error");
      maskSummary.textContent = "Enter at least one Job Applicant Id before masking.";
      return;
    }

    setMaskBusy(true);
    var chunks = chunk(ids, MASK_CHUNK_SIZE);
    var allResults = [];
    var succeeded = 0;
    var failed = 0;

    for (var c = 0; c < chunks.length; c++) {
      var done = c * MASK_CHUNK_SIZE;
      maskSummary.classList.remove("summary-error");
      maskSummary.textContent = "Masking " + Math.min(done + MASK_CHUNK_SIZE, ids.length)
        + " of " + ids.length + " profile(s)...";

      try {
        var resp = await fetch("/mask/batch", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, authHeaders),
          body: JSON.stringify({ items: chunks[c].map(function (id) { return { job_applicant_id: id }; }) }),
        });
        var data = await resp.json();
        if (data.status === "ok") {
          succeeded += data.succeeded;
          failed += data.failed;
          allResults = allResults.concat(data.results);
        } else {
          failed += chunks[c].length;
          allResults = allResults.concat(chunks[c].map(function (id) {
            return { job_applicant_id: id, result: { status: "error", detail: data.detail || "Batch failed." } };
          }));
        }
      } catch (e) {
        failed += chunks[c].length;
        allResults = allResults.concat(chunks[c].map(function (id) {
          return { job_applicant_id: id, result: { status: "error", detail: "Request failed: " + e.message } };
        }));
      }

      renderStatusRows(allResults);
    }

    maskSummary.classList.toggle("summary-error", failed > 0);
    maskSummary.textContent = succeeded + " succeeded, " + failed + " failed.";
    setMaskBusy(false);
  });

  } catch (e) {
    console.error("Mask Profile tab setup failed:", e);
  }

  // ── User Settings tab ─────────────────────────────────────────────────
  try {

  var settingsForm = document.getElementById("settingsForm");
  var loginHostSelect = document.getElementById("login_host_select");
  var loginHostCustom = document.getElementById("login_host_custom");
  var settingsSpinner = document.getElementById("settingsSpinner");
  var settingsMsg = document.getElementById("settingsMsg");

  loginHostSelect.addEventListener("change", function () {
    loginHostCustom.classList.toggle("d-none", loginHostSelect.value !== "__custom__");
  });

  function applyLoginHost(hostValue) {
    if (hostValue === "login") {
      loginHostSelect.value = "login.salesforce.com";
    } else if (hostValue === "test") {
      loginHostSelect.value = "test.salesforce.com";
    } else {
      loginHostSelect.value = "__custom__";
      loginHostCustom.value = hostValue;
      loginHostCustom.classList.remove("d-none");
    }
  }

  if (ctx.settings_configured && ctx.settings_login_host) {
    // Ground truth: what's actually saved server-side right now. Takes
    // priority over the orgUrl guess below -- this is real state, not a
    // heuristic extracted from a URL.
    applyLoginHost(ctx.settings_login_host);
  } else if (ctx.org_url) {
    // Convenience fallback when nothing's saved yet: orgUrl (from
    // MassMaskingController.generatemassmasking(), e.g.
    // "https://acme--partialcpy.sandbox.my.salesforce.com/services/Soap/c/59.0/00D...")
    // always carries the org's own My Domain host -- extract it and
    // pre-select "Custom My Domain host" with that value filled in, so
    // whoever fills in Settings doesn't have to go look it up and retype it.
    var match = /^https?:\/\/([^/]+)/.exec(ctx.org_url);
    if (match) {
      applyLoginHost(match[1]);
    }
  }

  // Translate the UI's environment picker into sf_client.py's existing
  // SF_DOMAIN convention ("login" / "test" / a full My Domain host) rather
  // than inventing new domain-parsing logic server-side. Returns "" if
  // nothing is selected -- deliberately NOT validated client-side (see
  // below): the server already gives a clear, correct error for a missing
  // login_host, so there's no separate client-side gate to get wrong or
  // silently block on.
  function resolveLoginHost() {
    var v = loginHostSelect.value;
    if (v === "login.salesforce.com") return "login";
    if (v === "test.salesforce.com") return "test";
    if (v === "__custom__") return loginHostCustom.value.trim();
    return "";
  }

  function showSettingsMessage(text, isError) {
    // Bootstrap alert, not just small colored text -- a save/fail result
    // must be impossible to miss, not something that can go unnoticed
    // right after clicking Save.
    settingsMsg.className = "mt-3 alert " + (isError ? "alert-danger" : "alert-success");
    settingsMsg.textContent = text;
    settingsMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  settingsForm.addEventListener("submit", function (e) {
    e.preventDefault();
    settingsMsg.className = "mt-2 small";
    settingsMsg.textContent = "";

    // Always submit -- resolveLoginHost() may return "" (nothing picked
    // yet), and the server's own validation ("Missing required field(s):
    // password, login_host") is the single source of truth for what's
    // wrong, shown via showSettingsMessage() below. A client-side-only
    // block here previously failed SILENTLY (a small, easy-to-miss text
    // line, no request ever sent) -- that was the actual "settings aren't
    // saving" bug: typing a password without first touching the
    // Environment dropdown did nothing, with no clear signal why.
    var body = {
      login_host: resolveLoginHost(),
      // Blank means "keep whatever's already saved" (server-side default),
      // so submit null rather than "" for an untouched field -- consistent
      // with client_key/client_secret below, and lets someone update just
      // the environment without re-entering a password they already saved.
      password: document.getElementById("password").value || null,
      client_key: document.getElementById("client_key").value || null,
      client_secret: document.getElementById("client_secret").value || null,
    };

    settingsSpinner.classList.remove("d-none");
    settingsForm.querySelector('button[type="submit"]').disabled = true;

    fetch("/candidate/settings", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders),
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          showSettingsMessage("Settings saved.", false);
        } else {
          showSettingsMessage(data.detail || "Failed to save settings.", true);
        }
      })
      .catch(function (e) {
        showSettingsMessage("Request failed: " + e.message, true);
      })
      .finally(function () {
        settingsSpinner.classList.add("d-none");
        settingsForm.querySelector('button[type="submit"]').disabled = false;
      });
  });

  } catch (e) {
    console.error("User Settings tab setup failed:", e);
  }
})();
