"""Server tests — Salesforce + upload MOCKED, PyMuPDF real (so masking is genuinely exercised offline).

Proves end-to-end through /mask:
  • fetch_resume_pdf is monkeypatched to return a generated sample PDF (no network, no real Salesforce)
  • upload_masked_pdf is a no-op capture that returns a fake ContentVersion Id
  • the masked PDF we hand to "upload" has PII redacted, experience/marks intact, and a watermark
  • /health returns ok

Run: ~/salesforce-ats/.venv/bin/python -m pytest tests/test_server.py -q
"""
import os
import sys

import fitz
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import server, sf_client  # noqa: E402

SAMPLE_PII = ["John Doe", "+91 98765 43210", "john.doe@example.com"]


def _make_sample_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "John Doe\n"
        "Phone: +91 98765 43210\n"
        "Email: john.doe@example.com\n\n"
        "Experience: 5 years at Acme Corp\n"
        "10th: 85%   12th: 88%",
        fontsize=12,
    )
    data = doc.tobytes()
    doc.close()
    return data


def _install_mocks(monkeypatch):
    """Force creds-configured, stub the Salesforce round-trip, capture the uploaded bytes."""
    captured = {}

    monkeypatch.setattr(sf_client, "creds_configured", lambda client_key=None: True)
    # connect() must never hit the network in tests.
    monkeypatch.setattr(sf_client, "connect", lambda client_key=None, force_refresh=False: object())
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: True)
    monkeypatch.setattr(server.sf_client, "connect", lambda client_key=None, force_refresh=False: object())

    def fake_fetch(job_applicant_id, sf=None):
        return _make_sample_pdf(), "pdf"

    def fake_upload(job_applicant_id, pdf_bytes, filename, sf=None):
        captured["pdf_bytes"] = pdf_bytes
        captured["filename"] = filename
        captured["job_applicant_id"] = job_applicant_id
        return "068000000000001AAA"

    def fake_watermark(account_id=None, sf=None):
        captured["watermark_account_id"] = account_id
        return None

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", fake_fetch)
    monkeypatch.setattr(server.sf_client, "resolve_account_id", lambda jaid, sf=None: None)
    monkeypatch.setattr(server.sf_client, "fetch_watermark_png", fake_watermark)
    monkeypatch.setattr(server.sf_client, "upload_masked_pdf", fake_upload)
    return captured


def test_health():
    client = TestClient(server.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "salesforce_configured" in resp.json()


def test_mask_with_explicit_strings(monkeypatch):
    captured = _install_mocks(monkeypatch)
    client = TestClient(server.app)

    resp = client.post(
        "/mask",
        json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["masked_content_version_id"] == "068000000000001AAA"
    assert body["redacted_regions"] >= 3

    # Inspect the masked bytes that were handed to "upload".
    masked = fitz.open(stream=captured["pdf_bytes"], filetype="pdf")
    text = "".join(p.get_text() for p in masked)
    masked.close()

    for pii in ["John Doe", "98765", "john.doe@example.com"]:
        assert pii not in text, f"PII leaked: {pii!r}"
    assert "Experience" in text and "85%" in text, "over-masked experience/marks"
    assert captured["filename"] == "masked_a0X000000000001.pdf"


def test_mask_falls_back_to_detect_pii(monkeypatch):
    """No mask_strings supplied -> detect_pii() regex should still strip email + phone."""
    captured = _install_mocks(monkeypatch)
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000002"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    masked = fitz.open(stream=captured["pdf_bytes"], filetype="pdf")
    text = "".join(p.get_text() for p in masked)
    masked.close()
    assert "john.doe@example.com" not in text, "regex email not redacted"
    assert "98765" not in text, "regex phone not redacted"


def test_mask_merges_structured_contact_pii_with_regex_fallback(monkeypatch):
    """Regression for a real, confirmed PII leak: some resume templates
    (e.g. Microsoft's built-in "Contoso" template) extract with the
    phone/email silently blank or garbled, so detect_pii()'s regex-on-text
    approach finds nothing -- even though the candidate's real contact info
    sits right there, correct, on the Contact record. When mask_strings
    isn't supplied (the real Salesforce flow never sends it), the
    candidate's structured Name/Phone/Email must be pulled from the
    related Contact and merged in, not left to regex alone.

    "John Doe" is present in the sample PDF's text but detect_pii() alone
    NEVER catches it (it only regex-matches email/phone, never names) --
    proving this specific redaction only happened because the structured
    Contact lookup fed it in, not as a side effect of the existing regex path."""
    captured = _install_mocks(monkeypatch)
    monkeypatch.setattr(server.sf_client, "resolve_contact_id", lambda jaid, sf=None: "003RESOLVEDCONTACT01")
    monkeypatch.setattr(server.sf_client, "fetch_contact_pii_strings",
                        lambda cid, sf=None: ["John Doe"])
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000002"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    masked = fitz.open(stream=captured["pdf_bytes"], filetype="pdf")
    text = "".join(p.get_text() for p in masked)
    masked.close()
    assert "John Doe" not in text, "structured Contact name was not merged into mask_strings"
    assert "john.doe@example.com" not in text, "regex-detected PII still redacted too"


def test_mask_calls_fetch_contact_pii_strings_when_contact_resolves(monkeypatch):
    captured = _install_mocks(monkeypatch)
    calls = {}
    monkeypatch.setattr(server.sf_client, "resolve_contact_id", lambda jaid, sf=None: "003RESOLVEDCONTACT01")

    def fake_fetch_contact_pii(cid, sf=None):
        calls["contact_id"] = cid
        return ["Jane Candidate", "jane.candidate@example.com"]

    monkeypatch.setattr(server.sf_client, "fetch_contact_pii_strings", fake_fetch_contact_pii)
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000002"})
    assert resp.status_code == 200, resp.text
    assert calls["contact_id"] == "003RESOLVEDCONTACT01"


def test_mask_skips_contact_pii_lookup_when_no_contact_resolves(monkeypatch):
    """No related Contact (resolve_contact_id returns None) -> must not call
    fetch_contact_pii_strings at all, just fall back to detect_pii()."""
    _install_mocks(monkeypatch)
    monkeypatch.setattr(server.sf_client, "resolve_contact_id", lambda jaid, sf=None: None)

    def fail_if_called(cid, sf=None):
        raise AssertionError("fetch_contact_pii_strings should not run without a resolved contact_id")

    monkeypatch.setattr(server.sf_client, "fetch_contact_pii_strings", fail_if_called)
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000002"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"  # still succeeds via detect_pii() alone


def test_mask_explicit_mask_strings_skips_contact_pii_lookup(monkeypatch):
    """When the caller DOES supply mask_strings explicitly, the structured
    Contact lookup must not run at all -- explicit input always wins."""
    _install_mocks(monkeypatch)

    def fail_if_called(jaid, sf=None):
        raise AssertionError("resolve_contact_id should not run when mask_strings is explicit")

    monkeypatch.setattr(server.sf_client, "resolve_contact_id", fail_if_called)
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000002", "mask_strings": ["x"]})
    assert resp.status_code == 200, resp.text


def test_detect_pii_ignores_stacked_dates_across_lines():
    """_PHONE_RE must not span newlines -- real resumes stack education years and
    date ranges on separate lines (e.g. "2023\\n2014\\n2016"), which previously
    matched as one 8+ digit "phone number" and got redacted, blacking out real
    content. Regression for the false-positive found testing real resumes."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "July 2019 - December 2023\n"
        "2014\n"
        "2016\n"
        "Jeewanbharti\n"
        "8949775210 | jeewanbharti560@gmail.com",
        fontsize=12,
    )
    pdf_bytes = doc.tobytes()
    doc.close()

    detected = server.detect_pii(pdf_bytes)
    assert "8949775210" in detected
    assert "jeewanbharti560@gmail.com" in detected
    assert not any("\n" in d for d in detected), \
        f"phone/email regex matched across a newline: {detected!r}"


def test_mask_errors_when_creds_missing(monkeypatch):
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: False)
    client = TestClient(server.app)
    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000003"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "not configured" in body["detail"].lower()


def test_fetch_resume_pdf_rejects_bad_salesforce_id():
    """SOQL-injection guard: a malformed Id (e.g. containing a quote) is rejected
    before it ever reaches a query string — validated before sf.query() is called,
    so no Salesforce connection needs mocking here."""
    import pytest
    with pytest.raises(sf_client.InvalidIdError):
        sf_client.fetch_resume_pdf("x' OR Id != '", sf=object())


def test_mask_endpoint_surfaces_invalid_id_as_error(monkeypatch):
    """The /mask route itself turns InvalidIdError into a clean {status: error},
    not a 500."""
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: True)
    monkeypatch.setattr(server.sf_client, "connect", lambda client_key=None: object())
    client = TestClient(server.app)
    resp = client.post("/mask", json={"job_applicant_id": "x' OR Id != '", "mask_strings": ["x"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "invalid" in body["detail"].lower()


def test_mask_unenforced_without_api_key(monkeypatch):
    """MASK_API_KEY unset -> no auth required (back-compat default)."""
    monkeypatch.delenv("MASK_API_KEY", raising=False)
    _install_mocks(monkeypatch)
    client = TestClient(server.app)
    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_mask_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    _install_mocks(monkeypatch)
    client = TestClient(server.app)

    no_key = client.post("/mask", json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII})
    assert no_key.status_code == 401

    wrong_key = client.post(
        "/mask", json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII},
        headers={"X-API-Key": "nope"},
    )
    assert wrong_key.status_code == 401

    right_key = client.post(
        "/mask", json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII},
        headers={"X-API-Key": "s3cr3t"},
    )
    assert right_key.status_code == 200
    assert right_key.json()["status"] == "ok"


def test_popup_is_real_html_and_carries_the_key(monkeypatch):
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    client = TestClient(server.app)
    resp = client.get("/popup")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in resp.text  # not JSON-escaped
    assert 'const API_KEY = "s3cr3t"' in resp.text


def test_candidate_mask_profile_index_renders(monkeypatch):
    """The actual Salesforce-embedded UI (Jinja2 templates + static assets),
    served at the exact path Salesforce hits -- was a 404, then a redirect
    stopgap to /popup, now the real page."""
    monkeypatch.setenv("SF_USERNAME", "someone@org.partialcpy")
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    client = TestClient(server.app)
    resp = client.get("/candidate/MaskProfileIndex")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Mask Profile" in resp.text
    assert "someone@org.partialcpy" in resp.text  # readonly uname field
    assert '"api_key": "s3cr3t"' in resp.text  # templated ctx for app.js's fetch() auth
    assert "/static/css/style.css" in resp.text
    assert "/static/js/app.js" in resp.text


def test_candidate_mask_profile_index_prefills_ids_from_real_query_param():
    """sfjobapplicantid is the REAL param name the massMasking LWC sends
    (fetched from the live org via the Tooling API to confirm) -- an
    earlier guess used 'ids' before that source was available; this is the
    one that actually matters."""
    client = TestClient(server.app)
    resp = client.get("/candidate/MaskProfileIndex?sfjobapplicantid=a0X000000000001;a0X000000000002")
    assert resp.status_code == 200
    assert "a0X000000000001;a0X000000000002" in resp.text


def test_candidate_mask_profile_index_prefills_ids_from_legacy_alias():
    """'ids' kept as a harmless fallback alias from before the real LWC
    source was available -- must keep working in case anything still uses it."""
    client = TestClient(server.app)
    resp = client.get("/candidate/MaskProfileIndex?ids=a0X000000000001,a0X000000000002")
    assert resp.status_code == 200
    assert "a0X000000000001,a0X000000000002" in resp.text


def test_candidate_mask_profile_index_real_param_wins_over_legacy_alias():
    client = TestClient(server.app)
    resp = client.get("/candidate/MaskProfileIndex?sfjobapplicantid=a0X000000000REAL&ids=a0X000000000LEGACY")
    assert resp.status_code == 200
    assert "a0X000000000REAL" in resp.text
    assert "a0X000000000LEGACY" not in resp.text


def test_candidate_mask_profile_index_uses_uname_query_param_over_env(monkeypatch):
    """uname comes from Apex's UserInfo.getUserName() (the Salesforce user
    viewing the page) -- takes priority over SF_USERNAME (the API
    integration user, a separate concept) when both are present."""
    monkeypatch.setenv("SF_USERNAME", "integration.user@org.com")
    client = TestClient(server.app)
    resp = client.get("/candidate/MaskProfileIndex?uname=recruiter@org.com")
    assert resp.status_code == 200
    assert "recruiter@org.com" in resp.text
    assert "integration.user@org.com" not in resp.text


def test_candidate_mask_profile_index_passes_real_sf_url_param_into_ctx():
    """sfURL is the REAL param name the massMasking LWC sends -- an earlier
    guess used 'orgUrl'."""
    client = TestClient(server.app)
    sf_url = "https://acme--partialcpy.sandbox.my.salesforce.com/services/Soap/c/59.0/00D000000000001"
    resp = client.get("/candidate/MaskProfileIndex", params={"sfURL": sf_url})
    assert resp.status_code == 200
    assert "acme--partialcpy.sandbox.my.salesforce.com" in resp.text


def test_candidate_mask_profile_index_passes_org_url_legacy_alias():
    client = TestClient(server.app)
    org_url = "https://acme--partialcpy.sandbox.my.salesforce.com/services/Soap/c/59.0/00D000000000001"
    resp = client.get("/candidate/MaskProfileIndex", params={"orgUrl": org_url})
    assert resp.status_code == 200
    assert "acme--partialcpy.sandbox.my.salesforce.com" in resp.text


def test_candidate_static_assets_are_served():
    client = TestClient(server.app)
    css = client.get("/static/css/style.css")
    assert css.status_code == 200
    js = client.get("/static/js/app.js")
    assert js.status_code == 200


def test_candidate_settings_requires_api_key(monkeypatch):
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    client = TestClient(server.app)
    resp = client.post("/candidate/settings", json={
        "password": "pw", "client_key": None, "client_secret": None, "login_host": "test",
    })
    assert resp.status_code == 401


def test_candidate_settings_delegates_to_register_default_credentials(monkeypatch):
    captured = {}

    def fake_register(password, client_id, client_secret, login_host):
        captured["password"] = password
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        captured["login_host"] = login_host

    monkeypatch.setattr(server.sf_client, "register_default_credentials", fake_register)
    client = TestClient(server.app)
    resp = client.post("/candidate/settings", json={
        "password": "pw+token", "client_key": "ck", "client_secret": "cs", "login_host": "test",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert captured == {"password": "pw+token", "client_id": "ck", "client_secret": "cs", "login_host": "test"}


def test_candidate_settings_surfaces_missing_credentials_error(monkeypatch):
    def fake_register(*a, **k):
        raise server.sf_client.MissingCredentialsError("Missing required field(s): password, login_host")

    monkeypatch.setattr(server.sf_client, "register_default_credentials", fake_register)
    client = TestClient(server.app)
    resp = client.post("/candidate/settings", json={
        "password": "", "client_key": None, "client_secret": None, "login_host": "",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "missing" in body["detail"].lower()


def test_candidate_settings_status_requires_api_key(monkeypatch):
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    client = TestClient(server.app)
    resp = client.get("/candidate/settings")
    assert resp.status_code == 401


def test_candidate_settings_status_reports_unconfigured(monkeypatch):
    monkeypatch.setattr(server.sf_client, "default_credentials_status",
                        lambda: {"configured": False, "login_host": None, "has_client_credentials": False})
    client = TestClient(server.app)
    resp = client.get("/candidate/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["configured"] is False


def test_candidate_settings_status_reports_configured_without_secrets(monkeypatch):
    monkeypatch.setattr(server.sf_client, "default_credentials_status",
                        lambda: {"configured": True, "login_host": "test", "has_client_credentials": True})
    client = TestClient(server.app)
    resp = client.get("/candidate/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["login_host"] == "test"
    assert body["has_client_credentials"] is True
    assert "password" not in resp.text.lower()
    assert "secret" not in [k.lower() for k in body.keys()]


def test_candidate_settings_delete_requires_api_key(monkeypatch):
    monkeypatch.setenv("MASK_API_KEY", "s3cr3t")
    client = TestClient(server.app)
    resp = client.delete("/candidate/settings")
    assert resp.status_code == 401


def test_candidate_settings_delete_delegates_and_reports_nothing_to_clear(monkeypatch):
    monkeypatch.setattr(server.sf_client, "remove_default_credentials", lambda: False)
    client = TestClient(server.app)
    resp = client.delete("/candidate/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "no stored settings" in body["detail"].lower()


def test_candidate_settings_delete_confirms_clear(monkeypatch):
    monkeypatch.setattr(server.sf_client, "remove_default_credentials", lambda: True)
    client = TestClient(server.app)
    resp = client.delete("/candidate/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_candidate_settings_surfaces_postgres_not_configured(monkeypatch):
    def fake_register(*a, **k):
        raise server.sf_client.PostgresNotConfiguredError("Postgres isn't provisioned.")

    monkeypatch.setattr(server.sf_client, "register_default_credentials", fake_register)
    client = TestClient(server.app)
    resp = client.post("/candidate/settings", json={
        "password": "pw", "client_key": None, "client_secret": None, "login_host": "test",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"


def test_mask_auto_resolves_account_id_for_per_client_watermark(monkeypatch):
    """The join-view button only passes job_applicant_id (no account_id) -- the
    service should resolve the client Account itself (SCSCHAMPS__Account__c on
    the Job Applicant row) and use it to look up that client's watermark."""
    captured = _install_mocks(monkeypatch)
    monkeypatch.setattr(server.sf_client, "resolve_account_id",
                        lambda jaid, sf=None: "001RESOLVEDACCT001")
    client = TestClient(server.app)

    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000001", "mask_strings": SAMPLE_PII})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert captured["watermark_account_id"] == "001RESOLVEDACCT001"


def test_mask_explicit_account_id_skips_auto_resolve(monkeypatch):
    """If the caller already passes account_id, don't override it with a lookup."""
    captured = _install_mocks(monkeypatch)

    def _should_not_be_called(jaid, sf=None):
        raise AssertionError("resolve_account_id should not run when account_id is given")

    monkeypatch.setattr(server.sf_client, "resolve_account_id", _should_not_be_called)
    client = TestClient(server.app)

    resp = client.post("/mask", json={
        "job_applicant_id": "a0X000000000001", "account_id": "001EXPLICIT0001AA",
        "mask_strings": SAMPLE_PII,
    })
    assert resp.status_code == 200
    assert captured["watermark_account_id"] == "001EXPLICIT0001AA"


def test_mask_with_inline_watermark_base64(monkeypatch):
    """watermark_base64 in the request should be decoded and used directly, no SOQL fetch."""
    import base64

    captured = _install_mocks(monkeypatch)

    def fail_if_called(account_id=None, sf=None):
        raise AssertionError("fetch_watermark_png should not be called when watermark_base64 is set")

    monkeypatch.setattr(server.sf_client, "fetch_watermark_png", fail_if_called)

    client = TestClient(server.app)
    # A real (if minimal) 1x1 transparent PNG — PyMuPDF's decoder requires valid image data.
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    resp = client.post(
        "/mask",
        json={
            "job_applicant_id": "a0X000000000004",
            "mask_strings": SAMPLE_PII,
            "watermark_base64": base64.b64encode(tiny_png).decode("ascii"),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["watermark_used"] == "image:inline_base64"


def test_mask_errors_on_invalid_watermark_base64(monkeypatch):
    _install_mocks(monkeypatch)
    client = TestClient(server.app)
    resp = client.post(
        "/mask",
        json={
            "job_applicant_id": "a0X000000000005",
            "mask_strings": SAMPLE_PII,
            "watermark_base64": "not-valid-base64!!!",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "base64" in body["detail"].lower()


def test_mask_errors_on_unknown_client_key(monkeypatch):
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: True)

    def fake_connect(client_key=None):
        raise sf_client.UnknownClientError(f"Unknown client_key '{client_key}'. Configured: []")

    monkeypatch.setattr(server.sf_client, "connect", fake_connect)

    client = TestClient(server.app)
    resp = client.post(
        "/mask",
        json={"job_applicant_id": "a0X000000000006", "client_key": "nonexistent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "nonexistent" in body["detail"]


def test_mask_unknown_client_key_message_not_swallowed_by_creds_configured(monkeypatch):
    """Regression: creds_configured(client_key=...) must not cause /mask to
    report the generic 'Salesforce credentials not configured.' for an
    UNREGISTERED client_key -- the caller needs the specific 'Unknown
    client_key' message to know to register it, not a message implying the
    whole deployment is unconfigured. Exercises the REAL creds_configured()
    and connect() (only the SF_CLIENTS_JSON registry is faked via env),
    unlike test_mask_errors_on_unknown_client_key above which mocks
    creds_configured itself and therefore can't catch this class of bug --
    found via live regression testing against a real deployment."""
    monkeypatch.setenv("SF_CLIENTS_JSON", '{"known-key": {"client_id":"x","client_secret":"y",'
                                          '"token_url":"https://x/token","instance_url":"https://x"}}')
    sf_client.invalidate_registry_cache()
    client = TestClient(server.app)
    try:
        resp = client.post("/mask", json={"job_applicant_id": "a0X000000000001", "client_key": "totally-unknown"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert "unknown" in body["detail"].lower() and "totally-unknown" in body["detail"], body
        assert "not configured" not in body["detail"].lower(), \
            f"generic message leaked through instead of the specific one: {body['detail']!r}"
    finally:
        sf_client.invalidate_registry_cache()


def test_mask_surfaces_salesforce_auth_failure_cleanly_not_500(monkeypatch):
    """Regression: connect() raising SalesforceAuthenticationError (bad
    password/token, from env vars OR a bad Settings-tab-saved override) must
    return a clean {status: error} response, not an uncaught 500. This was
    the actual root cause of a real "/mask/batch 500" report -- neither
    /mask nor /mask/batch caught this exception type at all before the fix."""
    def fake_connect(client_key=None, force_refresh=False):
        raise sf_client.SalesforceAuthenticationError(
            "Salesforce rejected the configured credentials: INVALID_LOGIN")

    monkeypatch.setattr(server.sf_client, "connect", fake_connect)
    client = TestClient(server.app)
    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "rejected" in body["detail"].lower()


def test_mask_batch_surfaces_salesforce_auth_failure_cleanly_not_500(monkeypatch):
    def fake_connect(client_key=None, force_refresh=False):
        raise sf_client.SalesforceAuthenticationError(
            "Salesforce rejected the configured credentials: INVALID_LOGIN")

    monkeypatch.setattr(server.sf_client, "connect", fake_connect)
    client = TestClient(server.app)
    resp = client.post("/mask/batch", json={"items": [{"job_applicant_id": "a0X000000000001"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "rejected" in body["detail"].lower()


def test_watermark_upload_surfaces_salesforce_auth_failure_cleanly(monkeypatch):
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: True)

    def fake_connect(client_key=None, force_refresh=False):
        raise sf_client.SalesforceAuthenticationError(
            "Salesforce rejected the configured credentials: INVALID_LOGIN")

    monkeypatch.setattr(server.sf_client, "connect", fake_connect)
    client = TestClient(server.app)
    resp = client.post("/watermark/upload", data={"account_id": "001XXXXXXXXXXXXAAA"},
                       files={"file": ("logo.png", b"\x89PNG\r\n\x1a\n" + b"x" * 10, "image/png")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "rejected" in body["detail"].lower()


def test_mask_retries_once_on_expired_session(monkeypatch):
    """A SalesforceExpiredSession on the first attempt should trigger exactly one
    forced-refresh retry via with_session(), not surface as a failure."""
    from simple_salesforce.exceptions import SalesforceExpiredSession

    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: True)

    connect_calls = []

    def fake_connect(client_key=None, force_refresh=False):
        connect_calls.append(force_refresh)
        return object()

    monkeypatch.setattr(server.sf_client, "connect", fake_connect)

    fetch_calls = []

    def fake_fetch(job_applicant_id, sf=None):
        fetch_calls.append(1)
        if len(fetch_calls) == 1:
            raise SalesforceExpiredSession(url="x", status=401, resource_name="ContentVersion", content=b"")
        return _make_sample_pdf(), "pdf"

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", fake_fetch)
    monkeypatch.setattr(server.sf_client, "resolve_account_id", lambda jaid, sf=None: None)
    monkeypatch.setattr(server.sf_client, "fetch_watermark_png", lambda account_id=None, sf=None: None)
    monkeypatch.setattr(server.sf_client, "upload_masked_pdf",
                        lambda jaid, pdf_bytes, filename, sf=None: "068000000000009AAA")

    client = TestClient(server.app)
    resp = client.post("/mask", json={"job_applicant_id": "a0X000000000009", "mask_strings": SAMPLE_PII})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert len(fetch_calls) == 2, "expected exactly one retry"
    assert connect_calls == [False, True], "second connect() must force a fresh token"


def test_mask_batch_all_succeed(monkeypatch):
    captured = _install_mocks(monkeypatch)
    client = TestClient(server.app)

    resp = client.post("/mask/batch", json={
        "items": [
            {"job_applicant_id": "a0X000000000010", "mask_strings": SAMPLE_PII},
            {"job_applicant_id": "a0X000000000011", "mask_strings": SAMPLE_PII},
        ],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert [r["job_applicant_id"] for r in body["results"]] == ["a0X000000000010", "a0X000000000011"]
    assert all(r["result"]["status"] == "ok" for r in body["results"])
    assert captured["job_applicant_id"] == "a0X000000000011", "last item's upload should be the one captured"


def test_mask_batch_partial_failure_does_not_abort_batch(monkeypatch):
    """One item's ResumeNotFoundError must not prevent the rest of the batch from running."""
    _install_mocks(monkeypatch)

    def fake_fetch(job_applicant_id, sf=None):
        if job_applicant_id == "a0X000000000BAD":
            raise sf_client.ResumeNotFoundError(f"No resume found for Job Applicant '{job_applicant_id}'.")
        return _make_sample_pdf(), "pdf"

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", fake_fetch)
    client = TestClient(server.app)

    resp = client.post("/mask/batch", json={
        "items": [
            {"job_applicant_id": "a0X000000000GOOD1", "mask_strings": SAMPLE_PII},
            {"job_applicant_id": "a0X000000000BAD", "mask_strings": SAMPLE_PII},
            {"job_applicant_id": "a0X000000000GOOD2", "mask_strings": SAMPLE_PII},
        ],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    statuses = {r["job_applicant_id"]: r["result"]["status"] for r in body["results"]}
    assert statuses["a0X000000000GOOD1"] == "ok"
    assert statuses["a0X000000000BAD"] == "error"
    assert statuses["a0X000000000GOOD2"] == "ok"


def test_mask_batch_errors_when_creds_missing(monkeypatch):
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda client_key=None: False)
    client = TestClient(server.app)
    resp = client.post("/mask/batch", json={"items": [{"job_applicant_id": "a0X000000000012"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"


def test_mask_batch_rejects_empty_items():
    client = TestClient(server.app)
    resp = client.post("/mask/batch", json={"items": []})
    assert resp.status_code == 422


def test_mask_inline_never_touches_salesforce(monkeypatch):
    """/mask/inline must not call connect/fetch/upload -- it's pure bytes-in/bytes-out."""
    import base64

    def fail_if_called(*a, **k):
        raise AssertionError("mask/inline must not touch Salesforce")

    monkeypatch.setattr(server.sf_client, "connect", fail_if_called)
    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", fail_if_called)
    monkeypatch.setattr(server.sf_client, "upload_masked_pdf", fail_if_called)

    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={
        "resume_base64": base64.b64encode(_make_sample_pdf()).decode("ascii"),
        "mask_strings": SAMPLE_PII,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["redacted_regions"] > 0

    masked_bytes = base64.b64decode(body["masked_pdf_base64"])
    doc = fitz.open(stream=masked_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()
    assert "John Doe" not in text
    assert "98765" not in text
    assert "Acme Corp" in text, "non-PII content must survive masking"


def test_mask_inline_falls_back_to_detect_pii():
    import base64

    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={
        "resume_base64": base64.b64encode(_make_sample_pdf()).decode("ascii"),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["redacted_regions"] > 0


def test_mask_inline_errors_on_invalid_resume_base64():
    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={"resume_base64": "not-valid-base64!!!"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "base64" in body["detail"].lower()


def test_mask_inline_errors_on_malformed_pdf_bytes():
    import base64

    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={
        "resume_base64": base64.b64encode(b"this is not a pdf").decode("ascii"),
        "mask_strings": ["x"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "masking failed" in body["detail"].lower()


def test_mask_inline_with_watermark_base64():
    import base64

    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={
        "resume_base64": base64.b64encode(_make_sample_pdf()).decode("ascii"),
        "mask_strings": SAMPLE_PII,
        "watermark_base64": base64.b64encode(tiny_png).decode("ascii"),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["watermark_used"] == "image:inline_base64"


def test_mask_inline_requires_api_key_when_configured(monkeypatch):
    import base64

    monkeypatch.setenv("MASK_API_KEY", "secret-123")
    client = TestClient(server.app)
    resp = client.post("/mask/inline", json={
        "resume_base64": base64.b64encode(_make_sample_pdf()).decode("ascii"),
        "mask_strings": SAMPLE_PII,
    })
    assert resp.status_code == 401


# ── Admin API (/admin/clients) ──────────────────────────────────────────────

def test_admin_routes_fail_closed_without_admin_api_key(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    client = TestClient(server.app)
    assert client.get("/admin/clients").status_code == 503
    assert client.put("/admin/clients/00D000000000001AAA", json={
        "client_id": "x", "client_secret": "y",
        "token_url": "https://x/token", "instance_url": "https://x",
    }).status_code == 503
    assert client.delete("/admin/clients/00D000000000001AAA").status_code == 503


def test_admin_routes_reject_wrong_admin_api_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-admin-key")
    client = TestClient(server.app)
    resp = client.get("/admin/clients", headers={"X-Admin-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_admin_list_clients_delegates_and_omits_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-admin-key")
    monkeypatch.setattr(server.sf_client, "list_registered_clients", lambda: [
        {"client_key": "00D000000000001AAA", "client_id": "acme-id",
         "token_url": "https://acme/token", "instance_url": "https://acme", "source": "db"},
    ])
    client = TestClient(server.app)
    resp = client.get("/admin/clients", headers={"X-Admin-API-Key": "correct-admin-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["clients"][0]["client_key"] == "00D000000000001AAA"
    assert "client_secret" not in body["clients"][0]


def test_admin_upsert_client_delegates_with_allow_overwrite_true(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-admin-key")
    captured = {}

    def fake_register(client_key, client_id, client_secret, token_url, instance_url, allow_overwrite=True):
        captured.update(locals())

    monkeypatch.setattr(server.sf_client, "register_client", fake_register)
    client = TestClient(server.app)
    resp = client.put("/admin/clients/00D000000000001AAA",
                      headers={"X-Admin-API-Key": "correct-admin-key"},
                      json={"client_id": "acme-id", "client_secret": "acme-secret",
                            "token_url": "https://acme/token", "instance_url": "https://acme"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert captured["client_key"] == "00D000000000001AAA"
    assert captured["allow_overwrite"] is True


def test_admin_delete_client_returns_error_when_not_found(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-admin-key")
    monkeypatch.setattr(server.sf_client, "remove_client", lambda client_key: False)
    client = TestClient(server.app)
    resp = client.delete("/admin/clients/00D000000000001AAA",
                         headers={"X-Admin-API-Key": "correct-admin-key"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_admin_upsert_client_surfaces_postgres_not_configured(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-admin-key")

    def fake_register(*a, **k):
        raise server.sf_client.PostgresNotConfiguredError("Postgres isn't provisioned.")

    monkeypatch.setattr(server.sf_client, "register_client", fake_register)
    client = TestClient(server.app)
    resp = client.put("/admin/clients/00D000000000001AAA",
                      headers={"X-Admin-API-Key": "correct-admin-key"},
                      json={"client_id": "x", "client_secret": "y",
                            "token_url": "https://x/token", "instance_url": "https://x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


# ── Self-service registration (/clients/self-register) ─────────────────────

def test_self_register_delegates_with_allow_overwrite_false(monkeypatch):
    captured = {}

    def fake_register(client_key, client_id, client_secret, token_url, instance_url, allow_overwrite=True):
        captured.update(locals())

    monkeypatch.setattr(server.sf_client, "register_client", fake_register)
    client = TestClient(server.app)
    resp = client.post("/clients/self-register", json={
        "client_key": "00D000000000001AAA", "client_id": "acme-id", "client_secret": "acme-secret",
        "token_url": "https://acme/token", "instance_url": "https://acme",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert captured["allow_overwrite"] is False


def test_self_register_uses_mask_api_key_not_admin_key(monkeypatch):
    """Self-registration must be gated by MASK_API_KEY (require_api_key), not
    ADMIN_API_KEY -- it's reachable from inside Salesforce at the same trust
    boundary as /mask, deliberately not the higher-privilege admin lane."""
    monkeypatch.setenv("MASK_API_KEY", "the-mask-key")
    monkeypatch.setattr(server.sf_client, "register_client", lambda *a, **k: None)
    client = TestClient(server.app)

    # Missing X-API-Key -> 401 (require_api_key), not 503 (require_admin_api_key)
    resp = client.post("/clients/self-register", json={
        "client_key": "00D000000000001AAA", "client_id": "id", "client_secret": "secret",
        "token_url": "https://x/token", "instance_url": "https://x",
    })
    assert resp.status_code == 401

    resp = client.post("/clients/self-register", headers={"X-API-Key": "the-mask-key"}, json={
        "client_key": "00D000000000001AAA", "client_id": "id", "client_secret": "secret",
        "token_url": "https://x/token", "instance_url": "https://x",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_self_register_surfaces_already_registered_error(monkeypatch):
    def fake_register(*a, **k):
        raise server.sf_client.ClientAlreadyRegisteredError("already there")

    monkeypatch.setattr(server.sf_client, "register_client", fake_register)
    client = TestClient(server.app)
    resp = client.post("/clients/self-register", json={
        "client_key": "00D000000000001AAA", "client_id": "id", "client_secret": "secret",
        "token_url": "https://x/token", "instance_url": "https://x",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "already registered" in body["detail"].lower()


def test_health_includes_registry_backend(monkeypatch):
    monkeypatch.setattr(server.sf_client, "registry_backend", lambda: "db+env")
    client = TestClient(server.app)
    resp = client.get("/health")
    assert resp.json()["registry_backend"] == "db+env"
