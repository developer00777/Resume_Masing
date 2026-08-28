"""Tests for sf_client.fetch_resume_pdf()'s multi-location lookup: modern
Files (ContentVersion) vs. legacy Attachment, on the Job Applicant directly
vs. the related Contact (SCSCHAMPS__Contact_Talent__c).

Not hypothetical -- confirmed against real Salesforce data on this org that
candidate resumes are legacy .docx Attachments on the related Contact, none
of the four locations checked here are made up. Regex/query-substring
matching stands in for a real Salesforce connection; no network involved.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import sf_client

JA_ID = "a0X000000000001"
CONTACT_ID = "003000000000001AAA"


class _ScriptedSF:
    """Fake Salesforce connection: .query() returns canned results keyed by
    a substring match against the SOQL, in the order given -- lets each test
    script exactly which of the lookup tiers has data, matching what a real
    org with resumes in only one of four possible locations looks like."""

    def __init__(self, rules):
        self.rules = rules  # list of (substring, result_dict)

    def query(self, soql):
        for substr, result in self.rules:
            if substr in soql:
                return result
        return {"records": []}


def _content_version_link(doc_id="069AAA"):
    return {"records": [{"ContentDocumentId": doc_id}]}


def _content_version_record(ext, version_data="/services/data/v59.0/sobjects/ContentVersion/068AAA/VersionData"):
    return {"records": [{"VersionData": version_data, "FileExtension": ext}]}


def _attachment_record(name, body="/services/data/v59.0/sobjects/Attachment/00PAAA/Body"):
    return {"records": [{"Id": "00PAAA", "Name": name, "Body": body}]}


@pytest.fixture(autouse=True)
def _stub_download(monkeypatch):
    """Every test controls exactly what bytes 'download' returns, keyed off
    the exact path it was asked to fetch -- avoids any real HTTP call."""
    captured = {}

    def fake_download(sf, path):
        captured["path"] = path
        return b"FAKE-FILE-BYTES:" + path.encode()

    monkeypatch.setattr(sf_client, "_download_version_data", fake_download)
    return captured


def test_finds_modern_file_on_job_applicant_directly():
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", _content_version_link()),
        ("FROM ContentVersion", _content_version_record("pdf")),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "pdf"
    assert data.startswith(b"FAKE-FILE-BYTES:")


def test_falls_back_to_modern_file_on_related_contact():
    sf = _ScriptedSF([
        # No ContentDocumentLink on the Job Applicant itself
        (f"LinkedEntityId = '{JA_ID}'", {"records": []}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: CONTACT_ID}]}),
        (f"LinkedEntityId = '{CONTACT_ID}'", _content_version_link("069CCC")),
        ("FROM ContentVersion", _content_version_record("docx")),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "docx"


def test_falls_back_to_legacy_attachment_on_job_applicant():
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", {"records": []}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: None}]}),
        (f"FROM Attachment WHERE ParentId = '{JA_ID}'", _attachment_record("resume.pdf")),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "pdf"


def test_falls_back_to_legacy_attachment_on_related_contact():
    """The confirmed real-world case: JA-26491 / Sunil Punnar's resume is
    exactly this -- a .docx legacy Attachment on the related Contact, with
    nothing on the Job Applicant record at all in any location."""
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", {"records": []}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: CONTACT_ID}]}),
        (f"LinkedEntityId = '{CONTACT_ID}'", {"records": []}),
        (f"FROM Attachment WHERE ParentId = '{JA_ID}'", {"records": []}),
        (f"FROM Attachment WHERE ParentId = '{CONTACT_ID}'", _attachment_record("Sunil[1_1].docx")),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "docx"


def test_prefers_pdf_over_docx_within_the_same_location():
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", _content_version_link()),
        ("FROM ContentVersion", {"records": [
            {"VersionData": "/path/to/docx", "FileExtension": "docx"},
            {"VersionData": "/path/to/pdf", "FileExtension": "pdf"},
        ]}),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "pdf"
    assert b"/path/to/pdf" in data


def test_ignores_non_resume_file_types():
    """A .jpg/.png attachment (e.g. a photo) must not be picked as the
    resume -- only pdf/docx/doc are recognized."""
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", {"records": []}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: None}]}),
        (f"FROM Attachment WHERE ParentId = '{JA_ID}'", _attachment_record("headshot.jpg")),
    ])
    with pytest.raises(sf_client.ResumeNotFoundError):
        sf_client.fetch_resume_pdf(JA_ID, sf=sf)


def test_raises_resume_not_found_when_nothing_anywhere():
    sf = _ScriptedSF([])  # every query returns {"records": []} via the default
    with pytest.raises(sf_client.ResumeNotFoundError):
        sf_client.fetch_resume_pdf(JA_ID, sf=sf)
