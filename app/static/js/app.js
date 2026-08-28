(function () {
  "use strict";

  var ctx = window.APP_CTX || {};
  var authHeaders = ctx.api_key ? { "X-API-Key": ctx.api_key } : {};

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

  maskBtn.addEventListener("click", function () {
    var ids = parseIds(jaIds.value);
    maskSummary.classList.remove("d-none", "summary-error");

    if (ids.length === 0) {
      maskSummary.classList.add("summary-error");
      maskSummary.textContent = "Enter at least one Job Applicant Id before masking.";
      return;
    }

    setMaskBusy(true);
    maskSummary.textContent = "Masking " + ids.length + " profile(s)...";

    fetch("/mask/batch", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders),
      body: JSON.stringify({ items: ids.map(function (id) { return { job_applicant_id: id }; }) }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          maskSummary.classList.remove("summary-error");
          maskSummary.textContent = data.succeeded + " succeeded, " + data.failed + " failed.";
          renderStatusRows(data.results);
        } else {
          maskSummary.classList.add("summary-error");
          maskSummary.textContent = data.detail || "Masking failed.";
        }
      })
      .catch(function (e) {
        maskSummary.classList.add("summary-error");
        maskSummary.textContent = "Request failed: " + e.message;
      })
      .finally(function () {
        setMaskBusy(false);
      });
  });

  // ── User Settings tab ─────────────────────────────────────────────────

  var settingsForm = document.getElementById("settingsForm");
  var loginHostSelect = document.getElementById("login_host_select");
  var loginHostCustom = document.getElementById("login_host_custom");
  var settingsSpinner = document.getElementById("settingsSpinner");
  var settingsMsg = document.getElementById("settingsMsg");

  loginHostSelect.addEventListener("change", function () {
    loginHostCustom.classList.toggle("d-none", loginHostSelect.value !== "__custom__");
  });

  // Convenience: orgUrl (from MassMaskingController.generatemassmasking(),
  // e.g. "https://acme--partialcpy.sandbox.my.salesforce.com/services/Soap/c/59.0/00D...")
  // always carries the org's own My Domain host -- extract it and pre-select
  // "Custom My Domain host" with that value filled in, so whoever fills in
  // Settings doesn't have to go look it up and retype it.
  if (ctx.org_url) {
    var match = /^https?:\/\/([^/]+)/.exec(ctx.org_url);
    if (match) {
      loginHostSelect.value = "__custom__";
      loginHostCustom.value = match[1];
      loginHostCustom.classList.remove("d-none");
    }
  }

  // Translate the UI's environment picker into sf_client.py's existing
  // SF_DOMAIN convention ("login" / "test" / a full My Domain host) rather
  // than inventing new domain-parsing logic server-side.
  function resolveLoginHost() {
    var v = loginHostSelect.value;
    if (v === "login.salesforce.com") return "login";
    if (v === "test.salesforce.com") return "test";
    if (v === "__custom__") return loginHostCustom.value.trim();
    return "";
  }

  settingsForm.addEventListener("submit", function (e) {
    e.preventDefault();
    settingsMsg.classList.remove("text-success", "text-danger");
    settingsMsg.textContent = "";

    var loginHost = resolveLoginHost();
    if (!loginHost) {
      settingsMsg.classList.add("text-danger");
      settingsMsg.textContent = "Select (or enter) a Salesforce environment.";
      return;
    }

    var body = {
      login_host: loginHost,
      password: document.getElementById("password").value,
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
          settingsMsg.classList.add("text-success");
          settingsMsg.textContent = "Settings saved.";
        } else {
          settingsMsg.classList.add("text-danger");
          settingsMsg.textContent = data.detail || "Failed to save settings.";
        }
      })
      .catch(function (e) {
        settingsMsg.classList.add("text-danger");
        settingsMsg.textContent = "Request failed: " + e.message;
      })
      .finally(function () {
        settingsSpinner.classList.add("d-none");
        settingsForm.querySelector('button[type="submit"]').disabled = false;
      });
  });
})();
