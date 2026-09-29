"""The template resume built for applicants with no resume on file."""
import fitz
from fastapi.testclient import TestClient

from app import server, sf_client, template_resume
from tests.test_server import _install_mocks

PROFILE = {
    "CurrentDesignation__c": "Testing Engineer",
    "CurrentCompany__c": "Acme Pvt. Ltd.",
    "worked_experience__c": 3.0,
    "SCSCHAMPS__Primary_Skills__c": "BMS Testing;CAN Bus",
    "Skill_List__c": "can bus, FMEA",
    "Highest_Qualification__c": "B.Tech",
    "DateOfBirth__c": "2001-06-07",
    "LanguagesKnown__c": "English;Hindi",
}


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    return " ".join(p.get_text() for p in doc).replace("\n", " ")


def test_renders_only_what_the_record_holds():
    text = _text(template_resume.render(PROFILE))
    for expected in ("Testing Engineer", "Acme Pvt. Ltd.", "3 years", "BMS Testing",
                     "FMEA", "B.Tech", "07 June 2001", "English, Hindi"):
        assert expected in text, expected
    # "can bus" duplicates the primary "CAN Bus" and is dropped.
    assert "can bus" not in text
    # No data, no section.
    assert "CAREER DETAILS" not in text and "Nationality" not in text


def test_email_and_phone_are_never_read():
    assert not {"Email", "PhoneNumber__c", "Phone", "MobilePhone"} & set(template_resume.PROFILE_FIELDS)


def test_name_heads_the_page():
    text = _text(template_resume.render({**PROFILE, "Name": "Suraj Kumar"}))
    assert text.lstrip().startswith("SURAJ KUMAR")
    assert "CANDIDATE PROFILE" not in text
    # No name on the record: the generic heading, never an empty one.
    assert "CANDIDATE PROFILE" in _text(template_resume.render(PROFILE))


def test_a_name_alone_is_not_enough_to_build_from():
    assert not template_resume.has_enough({"Name": "Suraj Kumar"})


def test_personal_details_alone_are_not_enough():
    assert not template_resume.has_enough({"SCSCHAMPS__Gender__c": "Male"})
    assert not template_resume.has_enough({})
    assert template_resume.has_enough({"CurrentDesignation__c": "Engineer"})


def test_mask_builds_template_when_no_resume(monkeypatch):
    captured = _install_mocks(monkeypatch)

    def no_resume(jaid, sf=None):
        raise sf_client.ResumeNotFoundError("No resume found.")

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", no_resume)
    monkeypatch.setattr(server.sf_client, "resolve_contact_id", lambda jaid, sf=None: None)
    monkeypatch.setattr(server.sf_client, "fetch_contact_profile",
                        lambda jaid, fields, sf=None: {**PROFILE, "Name": "Suraj Kumar"})
    monkeypatch.setattr(server.sf_client, "masked_file_url", lambda cvid, sf=None: None)
    # Same watermark as any masked resume: the client's logo, stamped centred.
    logo = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    logo.set_rect(logo.irect, (20, 74, 138))
    monkeypatch.setattr(server.sf_client, "fetch_watermark_png",
                        lambda account_id=None, sf=None: logo.tobytes("png"))

    # Even with the name sent for masking, as the Apex did: it stays.
    body = TestClient(server.app).post("/mask", json={"job_applicant_id": "a0X000000000001",
                                                      "mask_strings": ["Suraj Kumar"]}).json()
    assert body["status"] == "ok", body
    assert body["generated_from_template"] is True
    assert body["watermark_used"] == "image:global"
    assert "Testing Engineer" in _text(captured["pdf_bytes"])
    assert "SURAJ KUMAR" in _text(captured["pdf_bytes"]), "the name was masked off the template"
    page = fitz.open(stream=captured["pdf_bytes"], filetype="pdf")[0]
    assert page.get_images(), "watermark not stamped"


def test_no_template_when_an_unusable_resume_file_exists(monkeypatch):
    _install_mocks(monkeypatch)

    def image_only(jaid, sf=None):
        raise sf_client.ResumeNotFoundError("No resume found.", files_present=True)

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", image_only)
    monkeypatch.setattr(server.sf_client, "fetch_contact_profile",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build")))

    body = TestClient(server.app).post("/mask", json={"job_applicant_id": "a0X000000000001"}).json()
    assert body["status"] == "error" and not body["generated_from_template"]


class _FakeSF:
    """Salesforce stub answering the resume lookup's four queries."""

    def __init__(self, attachments):
        self.attachments = attachments

    def query(self, soql):
        if "FROM Attachment" in soql:
            return {"records": self.attachments}
        return {"records": []}


def test_lookup_reports_whether_files_were_present(monkeypatch):
    monkeypatch.setattr(sf_client, "resolve_contact_id", lambda jaid, sf=None: None)
    cases = [([], False),
             ([{"Name": "masked_a0X000000000001.pdf"}], False),  # our own output only
             ([{"Name": "scan.jpg"}], True)]
    for attachments, present in cases:
        try:
            sf_client.fetch_resume_pdf("a0X000000000001", sf=_FakeSF(attachments))
        except sf_client.ResumeNotFoundError as e:
            assert e.files_present is present, attachments
        else:
            raise AssertionError("expected ResumeNotFoundError")


def test_mask_still_errors_when_contact_is_empty(monkeypatch):
    _install_mocks(monkeypatch)

    def no_resume(jaid, sf=None):
        raise sf_client.ResumeNotFoundError("No resume found.")

    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", no_resume)
    monkeypatch.setattr(server.sf_client, "fetch_contact_profile", lambda jaid, fields, sf=None: {})

    body = TestClient(server.app).post("/mask", json={"job_applicant_id": "a0X000000000001"}).json()
    assert body["status"] == "error"
    assert "No resume found" in body["detail"]
