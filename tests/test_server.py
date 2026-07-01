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

    monkeypatch.setattr(sf_client, "creds_configured", lambda: True)
    # connect() must never hit the network in tests.
    monkeypatch.setattr(sf_client, "connect", lambda: object())
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda: True)
    monkeypatch.setattr(server.sf_client, "connect", lambda: object())

    def fake_fetch(job_applicant_id, sf=None):
        return _make_sample_pdf()

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


def test_mask_errors_when_creds_missing(monkeypatch):
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda: False)
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
    monkeypatch.setattr(server.sf_client, "creds_configured", lambda: True)
    monkeypatch.setattr(server.sf_client, "connect", lambda: object())
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
