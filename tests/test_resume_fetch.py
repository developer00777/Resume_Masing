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


def test_skips_our_own_masked_output_on_the_job_applicant():
    """The masked copy we uploaded must never become the next run's source.

    upload_masked_pdf() writes "masked_<ja_id>.pdf" back onto the same Job
    Applicant, so it is a .pdf and it is newer than the resume -- it wins the
    "newest usable file" contest and gets masked again. Confirmed on live
    data (JA-25368, JA-25599, JA-26461): re-masking redacted 0 regions,
    because the PII had already been removed from that copy.

    The real cost is that re-running can then never repair a record. The
    original is never read again, so whatever the first pass got wrong is
    frozen in and every later pass reproduces it byte for byte."""
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", _content_version_link()),
        ("FROM ContentVersion", {"records": [
            # the masked output, newest -- must be skipped
            {"VersionData": "/path/to/masked", "FileExtension": "pdf",
             "Title": f"masked_{JA_ID}"},
            {"VersionData": "/path/to/real", "FileExtension": "pdf",
             "Title": "Candidate Resume"},
        ]}),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "pdf"
    assert b"/path/to/real" in data, "masked output was used as the source resume"


def test_masked_output_alone_is_not_a_resume():
    """If the ONLY file is our masked output, that is 'no resume found'.

    Reporting it honestly lets the caller fall through to the related
    Contact, which on this org is where the original actually lives -- all
    three live records checked had the masked copy as their only Job
    Applicant file."""
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", _content_version_link()),
        ("FROM ContentVersion", {"records": [
            {"VersionData": "/path/to/masked", "FileExtension": "pdf",
             "Title": f"masked_{JA_ID}"},
        ]}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: CONTACT_ID}]}),
        (f"FROM Attachment WHERE ParentId = '{CONTACT_ID}'",
         _attachment_record("Candidate Resume.pdf")),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert ext == "pdf"
    assert b"masked" not in data


def test_skips_masked_output_stored_as_a_legacy_attachment():
    """Same rule for legacy Attachments, which key the name off Name."""
    sf = _ScriptedSF([
        (f"LinkedEntityId = '{JA_ID}'", {"records": []}),
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'",
         {"records": [{sf_client._JOB_APPLICANT_CONTACT_FIELD: None}]}),
        (f"FROM Attachment WHERE ParentId = '{JA_ID}'", {"records": [
            {"Name": f"masked_{JA_ID}.pdf", "Body": "/path/to/masked"},
            {"Name": "Candidate Resume.pdf", "Body": "/path/to/real"},
        ]}),
    ])
    data, ext = sf_client.fetch_resume_pdf(JA_ID, sf=sf)
    assert b"/path/to/real" in data


def test_raises_resume_not_found_when_nothing_anywhere():
    sf = _ScriptedSF([])  # every query returns {"records": []} via the default
    with pytest.raises(sf_client.ResumeNotFoundError):
        sf_client.fetch_resume_pdf(JA_ID, sf=sf)


# ── fetch_contact_pii_strings() ──────────────────────────────────────────────
# Regression coverage for a real, confirmed PII leak: some resume templates
# (e.g. Microsoft's built-in "Contoso" template, which renders the phone/
# email via a Word content control) extract with that contact info silently
# blank or garbled -- detect_pii()'s regex-on-text approach finds nothing,
# even though the candidate's real email/phone sit right there, correct, on
# the Contact record. Confirmed against two real candidates on the live org
# (details not reproduced here -- synthetic values used below instead);
# their real emails were completely absent from detect_pii()'s output
# despite being present and correct on Contact.Email.

def test_fetch_contact_pii_strings_returns_populated_fields_only():
    sf = _ScriptedSF([
        ("FROM Contact WHERE Id = '003000000000001AAA'", {"records": [{
            "Name": "Jane Candidate", "Phone": None, "MobilePhone": None, "HomePhone": None,
            "OtherPhone": None, "PhoneNumber__c": "5550100001",
            "SCSCHAMPS__PhoneNumber__c": None, "SCSCHAMPS__AlternatePhoneNumber__c": None,
            "Email": "jane.candidate@example.com", "SCSCHAMPS__AlternateEmail__c": None,
        }]}),
    ])
    result = sf_client.fetch_contact_pii_strings(CONTACT_ID, sf=sf)
    assert result == ["Jane Candidate", "5550100001", "jane.candidate@example.com"]


def test_fetch_contact_pii_strings_empty_when_contact_not_found():
    sf = _ScriptedSF([])
    assert sf_client.fetch_contact_pii_strings(CONTACT_ID, sf=sf) == []


def test_fetch_contact_pii_strings_never_raises_on_query_failure():
    class _RaisingSF:
        def query(self, soql):
            raise Exception("simulated Salesforce error")
    assert sf_client.fetch_contact_pii_strings(CONTACT_ID, sf=_RaisingSF()) == []
