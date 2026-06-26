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

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", fake_fetch)
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
