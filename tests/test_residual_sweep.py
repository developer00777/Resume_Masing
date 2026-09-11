"""Second-pass (residual sweep) regression and scoring suite.

The first pass removes what it was handed. This suite scores what happens
after it -- the phone numbers and email addresses still on the page once the
Salesforce Contact's values are gone. Those are the three defects reported
off job applicants JA-26753 / JA-26708 / JA-26631:

    an alternate mobile number left unmasked
    an alternate email address left unmasked
    an "Email ID:" label left standing over the white space where the
        address used to be

Scored the same way tests/test_mask_precision.py is, because the trade-off is
the same and pulls in both directions:

    a MISS   leaks candidate PII to the client       (compliance)
    a FALSE  blanks out real resume content          (visible to the recruiter)

Three levels of evidence, cheapest first:

    1. trap tables   -- one line of text, no PDF. What the numbering plan
                        says is and is not a number, and what is and is not
                        an address. This is where the edge cases live.
    2. fixtures      -- whole resumes through the production path, scored.
    3. delta         -- the same corpus with the second pass switched off,
                        so the score says what the change actually bought.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mask, pii, residual  # noqa: E402
from app.server import detect_pii  # noqa: E402

#: Compliance bar: a leak is never acceptable, at any accuracy.
MAX_LEAKS = 0

#: Content bar. Set against what the first pass already holds (96%) -- the
#: second pass adds recall and must not spend precision to get it.
ACCURACY_FLOOR = 0.98


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _make_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 60.0
    for line in lines:
        if line:
            page.insert_text((56, y), line, fontsize=10)
        y += 15.5
        if y > 760:
            page = doc.new_page()
            y = 60.0
    out = doc.tobytes()
    doc.close()
    return out


def _masked_text(lines: list[str], mask_strings: list[str],
                 residual_sweep: bool = True) -> tuple[str, int]:
    masked, hits = mask.mask_pdf_bytes(_make_pdf(lines), mask_strings,
                                       watermark_text="",
                                       residual_sweep=residual_sweep)
    doc = fitz.open(stream=masked, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    return text, hits


# =========================================================================
# 1. trap tables
# =========================================================================

#: Every way the six sampled resumes, and the numbering plan, write an Indian
#: number -- plus the international forms a candidate abroad uses. The
#: starred ones are what the FIRST pass rejects and this one has to catch:
#: they carry no "+", and none of them matches a shape in _PHONE_SHAPES.
PHONE_MUST_FIND = [
    # --- plain national ---
    ("9876543210", ["9876543210"]),
    ("98765 43210", ["98765 43210"]),
    ("98765-43210", ["98765-43210"]),
    # --- country code, written every way ---
    ("+91 9876543210", ["+91 9876543210"]),
    ("+919876543210", ["+919876543210"]),
    ("+91-9876543210", ["+91-9876543210"]),
    ("+91 98765 43210", ["+91 98765 43210"]),
    ("+91 98765-43210", ["+91 98765-43210"]),
    ("91 9876543210", ["91 9876543210"]),                 # * no plus
    ("919876543210", ["919876543210"]),                   # * no plus, no space
    ("0091 9876543210", ["0091 9876543210"]),             # *
    ("(+91) 9876543210", ["(+91) 9876543210"]),           # *
    ("+91 (0) 9876543210", ["+91 (0) 9876543210"]),       # *
    ("09876543210", ["09876543210"]),                     # * trunk prefix
    # --- the labels an alternate number hides behind ---
    ("Alternate Mobile No.: 9812345678", ["9812345678"]),
    ("Alt. Mob - 8123456789", ["8123456789"]),
    ("Whatsapp: 7012345678", ["7012345678"]),
    ("Res: 6012345678", ["6012345678"]),
    ("Mobile9876543210", ["9876543210"]),                 # * PDF lost the space
    ("Contact No - 9876543210 (Personal)", ["9876543210"]),
    # --- two numbers on one line ---
    ("9876543210 / 9123456789", ["9876543210", "9123456789"]),
    ("9876543210, 9123456789", ["9876543210", "9123456789"]),
    ("Mob: +91 9876543210 / +91 9123456789",
     ["+91 9876543210", "+91 9123456789"]),
    # --- landline ---
    ("Tel: 011-23456789", ["011-23456789"]),
    ("Landline 022-2345-6789", ["022-2345-6789"]),
    ("Ph 0755-123456", ["0755-123456"]),
    # --- candidates working abroad ---
    ("+971 50 123 4567", ["+971 50 123 4567"]),
    ("+1 (555) 123-4567", ["+1 (555) 123-4567"]),
    ("(415) 555-0132", ["(415) 555-0132"]),
    ("555-123-4567", ["555-123-4567"]),
]

#: Everything an Indian resume puts on a page that is digits and is not a
#: phone number. The last six are the ones the numbering plan specifically
#: buys: each is ten-or-more digits that a \d{10}-style detector accepts.
PHONE_MUST_NOT_FIND = [
    "Senior Engineer, Acme Corp   2019 - 2023",
    "B.Tech, 2012 - 2016, CGPA 8.94/10.0",
    "Employment 06/2016 - 05/2019",
    "Date of Birth: 12-05-1990",
    "Credential ID 4821-9930-1177",
    "Cert No. 00219384",
    "ISO 9001:2015 Lead Auditor",
    "IEEE 802.11ac / RFC 2616",
    "Python 3.10, PostgreSQL 14.2, Java 8",
    "Version 2.7.1, build 20240115",
    "Cut latency by 45%, throughput 1200 req/s",
    "99.99% uptime, 250 ms p95",
    "Budget of 2,500,000 INR across 12 projects",
    "Scaled from 50000 to 1200000 monthly users",
    "Salary 1200000 per annum",
    "CTC 1800000 per annum",
    "Marks 456789 out of 500000",
    "Rank 14 of 3200 candidates",
    "Gurugram 122003",
    "PIN 560103, Bengaluru",
    "Postal code 400001",
    "Roll No 12345678",
    "Student ID 20120847713",
    "Invoice 987654321",
    "Order Reference 4319876543210",
    "Serial 4455-6677-8899-0011",
    "ISBN 978-3-16-148410-0",
    "DOI 10.1000/182",
    "Patent US 9876543 B2",
    "IFSC HDFC0001234",
    "GSTIN 27AAECS1234F1Z5",
    "Passport J8369854 issued 2019",
    "Timeline 2015 - 2018, 2018 - 2022",
    # --- ten-or-more digits that are emphatically not a subscriber number ---
    "Aadhaar 1234 5678 9012",            # 12, spaced in fours
    "Aadhaar No 987654321098",           # 12, unspaced, starts 9
    "UAN 101234567890",                  # 12
    "Account 000123456789",              # 12
    "Card 4111 1111 1111 1111",          # 16
    "Epoch timestamp 1609459200 processed",   # 10, starts 1
    "Revenue 9500000000 in FY23",        # 10, starts 9: a round magnitude
    "Handled 1500000000 records",        # 10, starts 1
    "Reference No. 9876543210",          # 10, starts 9: labelled an id
    "Employee Code 9876543210",          # 10, starts 9: labelled an id
    "Credential ID 4821 9876543210",     # the id's tail is a perfect mobile
]

EMAIL_MUST_FIND = [
    ("rahul.sharma@gmail.com", ["rahul.sharma@gmail.com"]),
    ("RAHUL.SHARMA@YAHOO.CO.IN", ["RAHUL.SHARMA@YAHOO.CO.IN"]),
    ("Email ID : abc_123@rediffmail.com", ["abc_123@rediffmail.com"]),
    ("E-Mail: a.b+tag@outlook.com", ["a.b+tag@outlook.com"]),
    ("Alternate: anil.k@example.co.in", ["anil.k@example.co.in"]),
    ("first.last@iitb.ac.in", ["first.last@iitb.ac.in"]),
    ("hr@acme-corp.io", ["hr@acme-corp.io"]),
    ("a@b.in", ["a@b.in"]),
    ("9876543210@gmail.com", ["9876543210@gmail.com"]),
    ("(rahul@gmail.com)", ["rahul@gmail.com"]),
    ("Email: rahul@gmail.com.", ["rahul@gmail.com"]),
    ("mailto:rahul@gmail.com", ["rahul@gmail.com"]),
    ("rahul@gmail.com, priya@yahoo.com", ["rahul@gmail.com", "priya@yahoo.com"]),
    # --- the address a PDF broke into two word boxes: the reason an address
    #     that detect_pii() found could still not be placed on the page ---
    ("rahul.sharma@ gmail.com", ["rahul.sharma@ gmail.com"]),
    ("rahul.sharma @gmail.com", ["rahul.sharma @gmail.com"]),
    ("rahul . sharma@gmail.com", ["rahul . sharma@gmail.com"]),
    ("rahul@gmail. com", ["rahul@gmail. com"]),
    ("rahul(at)gmail(dot)com", ["rahul(at)gmail(dot)com"]),
]

EMAIL_MUST_NOT_FIND = [
    "Follow @rahulsharma on X",
    "logo@2x.png in the assets folder",
    "installed react@18.2.0 and webpack@5",
    "bootstrap@5.min.css bundled",
    "linkedin.com/in/rahulsharma",
    "www.acme.com and acme.co.in",
    "Reached out to the team at acme. Then we shipped it",
    "Rate of 45@ per unit",
]


@pytest.mark.parametrize("text,expected", PHONE_MUST_FIND,
                         ids=[t[0][:38] for t in PHONE_MUST_FIND])
def test_phone_is_found(text, expected):
    assert [text[a:b] for a, b in pii.scan_phones(text)] == expected


@pytest.mark.parametrize("text", PHONE_MUST_NOT_FIND,
                         ids=[t[:38] for t in PHONE_MUST_NOT_FIND])
def test_non_phone_is_not_found(text):
    assert [text[a:b] for a, b in pii.scan_phones(text)] == []


@pytest.mark.parametrize("text,expected", EMAIL_MUST_FIND,
                         ids=[t[0][:38] for t in EMAIL_MUST_FIND])
def test_email_is_found(text, expected):
    assert [text[a:b] for a, b in pii.scan_emails(text)] == expected


@pytest.mark.parametrize("text", EMAIL_MUST_NOT_FIND,
                         ids=[t[:38] for t in EMAIL_MUST_NOT_FIND])
def test_non_email_is_not_found(text):
    assert [text[a:b] for a, b in pii.scan_emails(text)] == []


def test_numbering_plan():
    """The predicate the whole pass rests on."""
    assert pii.india_nsn("919876543210") == "9876543210"      # country code
    assert pii.india_nsn("09876543210") == "9876543210"        # trunk prefix
    assert pii.india_nsn("00919876543210") == "9876543210"     # both
    assert pii.india_nsn("9876543210") == "9876543210"         # bare
    assert pii.india_nsn("987654321098") is None               # 12: Aadhaar/UAN
    assert pii.india_nsn("98765432") is None                   # 8
    assert pii.is_indian_mobile("9876543210")
    assert pii.is_indian_mobile("6012345678")                  # the 6-series
    assert not pii.is_indian_mobile("1609459200")              # a timestamp
    assert not pii.is_indian_mobile("5876543210")              # not a mobile series


# =========================================================================
# 2. end-to-end fixtures
# =========================================================================

class Fixture:
    """One resume, what the Contact record holds, and the scored expectations."""

    def __init__(self, name, lines, contact, must_mask, must_survive):
        self.name = name
        self.lines = lines
        self.contact = contact
        self.must_mask = must_mask
        self.must_survive = must_survive

    def mask_strings(self) -> list[str]:
        """Exactly how server.mask_endpoint builds the list."""
        pdf = _make_pdf(self.lines)
        seen, out = set(), []
        for s in list(self.contact) + detect_pii(pdf):
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def run(self, residual_sweep: bool = True) -> str:
        return _masked_text(self.lines, self.mask_strings(), residual_sweep)[0]


FIXTURES = [
    Fixture(
        # The reported defect, reproduced. The Contact record carries one
        # number and one address; the resume carries three more that no
        # upstream system has ever seen, in the forms the first pass rejects.
        "alternate-contacts-known-only-to-the-resume",
        [
            "RAHUL SHARMA",
            "Email ID : rahul.sharma@gmail.com",
            "Alternate Email : rahul.personal@yahoo.co.in",
            "Mobile No.: +91 98765 43210",
            "Alternate Mobile No.: 919812345678",
            "Alt. Contact - 09123456780",
            "WhatsApp 8012345679",
            "",
            "EXPERIENCE",
            "Senior Engineer, Acme Corp                     2019 - 2023",
            "  - Cut p99 latency by 45% and raised throughput to 1200 req/s",
            "  - Owned a budget of 2,500,000 INR across 12 projects",
            "EDUCATION",
            "B.Tech, IIT Bombay, 2012 - 2016, CGPA 8.94/10.0",
            "AWS Certified, Credential ID 4821-9930-1177",
        ],
        contact=["Rahul Sharma", "+919876543210", "rahul.sharma@gmail.com"],
        must_mask=[
            "Rahul Sharma", "rahul.sharma@gmail.com", "98765 43210",
            "rahul.personal@yahoo.co.in",   # alternate email
            "919812345678",                 # alternate mobile, no "+"
            "09123456780",                  # alternate mobile, trunk prefix
            "8012345679",                   # whatsapp
            "Email ID",                     # the label left standing
        ],
        must_survive=[
            "2019 - 2023", "45%", "1200", "2,500,000", "2012 - 2016",
            "8.94/10.0", "4821-9930-1177", "Acme Corp", "IIT Bombay",
        ],
    ),
    Fixture(
        # An Indian resume's identifier block, which is what a second pass
        # is most likely to over-mask: Aadhaar, UAN, PF and PAN all sit in
        # the same ten-to-twelve digit range as a mobile number.
        "identifier-dense-indian-resume",
        [
            "PRIYA VENKATESAN",
            "Mob: 9845098450 | priya.v@example.com",
            "",
            "PERSONAL DETAILS",
            "Aadhaar No 987654321098",
            "Aadhaar 1234 5678 9012",
            "PAN ABCDE1234F",
            "UAN 101234567890",
            "Passport J8369854 issued 2019",
            "Date of Birth: 12-05-1990",
            "PIN 560103, Bengaluru",
            "",
            "Employee Code 9876543211",
            "Reference No. 9876543212",
            "Bank Account 000123456789, IFSC HDFC0001234",
            "Revenue 9500000000 in FY23 across 2019 - 2023",
        ],
        contact=["Priya Venkatesan", "9845098450", "priya.v@example.com"],
        # Not 98765432xx: the Aadhaar number below starts with those ten
        # digits, and a surviving id that CONTAINS the phone is
        # indistinguishable from a leak by substring search.
        must_mask=["Priya Venkatesan", "priya.v@example.com", "9845098450"],
        must_survive=[
            "987654321098", "1234 5678 9012", "ABCDE1234F", "101234567890",
            "J8369854", "12-05-1990", "560103", "9876543211", "9876543212",
            "000123456789", "HDFC0001234", "9500000000", "2019 - 2023",
        ],
    ),
    Fixture(
        # No Contact record at all: the second pass is the only thing
        # standing between the client and the candidate's contact block.
        "no-contact-record-sweep-only",
        [
            "MEERA IYER",
            "meera.iyer@example.com   91 9812345678",
            "Secondary Email ID: meera.i@rediffmail.com",
            "Landline 022-2345-6789",
            "",
            "Timeline: 2015 - 2018, 2018 - 2022, 2022 - Present",
            "Certificates: 1102-4455-7788, 5566-1122-3344",
            "Metrics: 99.99% uptime, 250 ms p95, 15000 rps",
        ],
        contact=[],
        must_mask=["meera.iyer@example.com", "9812345678",
                   "meera.i@rediffmail.com", "022-2345-6789"],
        must_survive=["2015 - 2018", "2018 - 2022", "2022 - Present",
                      "1102-4455-7788", "5566-1122-3344",
                      "99.99%", "250", "15000"],
    ),
    Fixture(
        # A candidate working abroad, whose contact block is a mix of
        # international and Indian numbers.
        "overseas-candidate-mixed-formats",
        [
            "DEEPAK RAO",
            "deepak.rao@acme-corp.io  |  +971 50 123 4567  |  (415) 555-0132",
            "India contact: 0091 9900112233",
            "",
            "Scaled the platform from 50000 to 1200000 monthly users",
            "Reduced cloud spend from 480000 to 310000 per year",
            "ISBN 978-3-16-148410-0, DOI 10.1000/182",
        ],
        contact=["Deepak Rao", "deepak.rao@acme-corp.io"],
        must_mask=["deepak.rao@acme-corp.io", "50 123 4567",
                   "555-0132", "9900112233"],
        must_survive=["50000", "1200000", "480000", "310000",
                      "978-3-16-148410-0", "10.1000/182"],
    ),
    Fixture(
        # The contact block as a resume actually types it, rather than as a
        # parser would like it. Every value here is invisible to the first
        # pass for its own reason, and none of them is exotic:
        #   "91 9812345678"      no "+", so no international evidence, and
        #                        twelve digits match no shape
        #   "08012345678"        trunk prefix, eleven digits, same problem
        #   "...@ outlook.com"   a space after the "@": EMAIL_RE does not
        #                        match it, so detect_pii never reports it and
        #                        nothing downstream ever looks for it
        "contact-block-the-first-pass-cannot-read",
        [
            "ANIL KUMAR",
            "Mob: 9845098451 / 91 9812345678",
            "Emergency 08012345678",
            "anil.kumar@gmail.com | anil.k.official@ outlook.com",
            "",
            "EXPERIENCE",
            "Project Lead, Globex                      2018 - 2022",
            "  - Processed 1500000000 records, 99.99% success, p95 250 ms",
            "  - Certified: Credential ID 4821-9930-1177, Cert No. 00219384",
            "Aadhaar 1234 5678 9012 | PAN ABCDE1234F | PIN 560103",
        ],
        contact=["Anil Kumar", "9845098451", "anil.kumar@gmail.com"],
        must_mask=["Anil Kumar", "9845098451", "anil.kumar@gmail.com",
                   "9812345678", "08012345678", "anil.k.official"],
        must_survive=["2018 - 2022", "1500000000", "99.99%", "250",
                      "4821-9930-1177", "00219384", "1234 5678 9012",
                      "ABCDE1234F", "560103", "Globex"],
    ),
]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
def test_fixture_masks_only_pii(fixture):
    text = _norm(fixture.run())
    leaked = [s for s in fixture.must_mask if _norm(s) in text]
    destroyed = [s for s in fixture.must_survive if _norm(s) not in text]
    assert not leaked, f"{fixture.name}: PII survived masking: {leaked}"
    assert not destroyed, f"{fixture.name}: non-PII content was masked: {destroyed}"


# =========================================================================
# the specific defects that were reported
# =========================================================================

def test_alternate_mobile_without_a_plus_is_masked():
    """"91 9876543210" and "09876543210" are the same number as the Contact's.

    Neither matches a shape in _PHONE_SHAPES and neither starts with "+", so
    the first-pass detector rejects both and the Contact's own "+91..." does
    not match them literally either. The numbering plan is what identifies
    them."""
    text, _ = _masked_text(
        ["RAHUL SHARMA",
         "Mobile: +91 98765 43210",
         "Alternate: 91 9812345678",
         "Alternate 2: 09123456780",
         "Acme Corp   2019 - 2023"],
        ["Rahul Sharma", "+919876543210"])
    stripped = _norm(text)
    for leak in ("9812345678", "9123456780", "9876543210"):
        assert leak not in stripped, f"alternate mobile leaked: {leak}"
    assert "2019-2023" in stripped, "over-masked the date range"


def test_email_id_label_is_removed_with_the_value():
    """The reported "the emailid tag is also not removed".

    _CONTACT_LABEL_RE knew "Email" but not "ID", so absorption stopped at the
    colon and the masked page kept "Email ID:" hanging over white space --
    which tells the reader exactly what was taken out."""
    text, _ = _masked_text(
        ["Email ID : rahul.sharma@gmail.com",
         "Alternate E - Mail ID : rahul.p@yahoo.co.in",
         "Mob No. : 9876543210",
         "Acme Corp   2019 - 2023"],
        ["rahul.sharma@gmail.com"])
    stripped = _norm(text)
    assert "rahul.sharma@gmail.com" not in stripped
    assert "rahul.p@yahoo.co.in" not in stripped
    assert "9876543210" not in stripped
    for label in ("EmailID", "E-MailID", "MobNo"):
        assert label not in stripped, f"label left standing: {label!r}"
    assert "2019-2023" in stripped


def test_email_split_across_word_boxes_is_masked():
    """An address the PDF kerned apart cannot be found by search_for().

    detect_pii() reads page.get_text() and sees the address; mask places it
    with page.search_for(), which is an exact substring match and finds
    nothing. The address is therefore detected, counted, and left on the
    page -- the second failure mode behind "in some resumes the email is not
    removed". The sweep builds the rect from the word boxes instead."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((56, 60), "RAHUL SHARMA", fontsize=12)
    # Three separate word boxes, exactly as a kerned address extracts.
    page.insert_text((56, 80), "rahul.sharma@", fontsize=10)
    page.insert_text((120, 80), "gmail", fontsize=10)
    page.insert_text((150, 80), ".com", fontsize=10)
    page.insert_text((56, 100), "Acme Corp   2019 - 2023", fontsize=10)
    pdf = doc.tobytes()
    doc.close()

    masked, hits = mask.mask_pdf_bytes(pdf, ["rahul.sharma@gmail.com"],
                                       watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    text = _norm(doc[0].get_text())
    doc.close()
    assert hits >= 1
    assert "rahul.sharma@" not in text and "gmail" not in text, \
        f"a broken-up address leaked: {text!r}"
    assert "2019-2023" in text, "over-masked the date range"


def test_mailto_link_is_removed():
    """Redaction deletes the glyphs; the link annotation keeps the address.

    Nothing on the page shows it, and every PDF reader still has it."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((56, 60), "rahul.sharma@gmail.com", fontsize=10)
    page.insert_link({"kind": fitz.LINK_URI,
                      "from": fitz.Rect(56, 48, 200, 64),
                      "uri": "mailto:rahul.sharma@gmail.com"})
    page.insert_link({"kind": fitz.LINK_URI,
                      "from": fitz.Rect(56, 80, 200, 96),
                      "uri": "https://linkedin.com/in/rahulsharma"})
    pdf = doc.tobytes()
    doc.close()

    masked, _ = mask.mask_pdf_bytes(pdf, ["rahul.sharma@gmail.com"],
                                    watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    uris = [l.get("uri", "") for l in doc[0].get_links()]
    doc.close()
    assert not any("rahul.sharma" in u for u in uris), \
        f"the address survived in a link annotation: {uris}"
    assert any("linkedin" in u for u in uris), "removed an unrelated link"


def test_document_metadata_is_scrubbed():
    """Word writes the candidate's name into /Title and /Author.

    It is not on any page, so redaction never touches it, and it is the
    first thing a PDF reader shows in the title bar."""
    doc = fitz.open()
    doc.new_page().insert_text((56, 60), "Acme Corp   2019 - 2023", fontsize=10)
    doc.set_metadata({"title": "Rahul Sharma CV 2024",
                      "author": "Rahul Sharma",
                      "subject": "rahul.sharma@gmail.com",
                      "keywords": "9876543210"})
    pdf = doc.tobytes()
    doc.close()

    masked, _ = mask.mask_pdf_bytes(pdf, ["Rahul Sharma"], watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    meta = doc.metadata
    doc.close()
    for key in residual._PII_METADATA_KEYS:
        assert not meta.get(key), f"{key} still holds {meta.get(key)!r}"


def test_sweep_leaves_a_clean_page_untouched():
    """No contact details on the page means no redactions at all."""
    text, hits = _masked_text(
        ["EXPERIENCE",
         "Senior Engineer, Acme Corp   2019 - 2023",
         "Credential ID 4821-9930-1177, Cert No. 00219384",
         "Aadhaar 1234 5678 9012, UAN 101234567890",
         "Revenue 3400000 to 9100000 between 2020 and 2023"],
        [])
    assert hits == 0, f"the sweep redacted {hits} regions on a page with no PII"
    assert "4821-9930-1177" in text and "101234567890" in text


# =========================================================================
# 3. the score
# =========================================================================

def _score(residual_sweep: bool) -> tuple[int, int, list[str], list[str]]:
    correct = total = 0
    leaks: list[str] = []
    over: list[str] = []
    for fx in FIXTURES:
        text = _norm(fx.run(residual_sweep=residual_sweep))
        for s in fx.must_mask:
            total += 1
            if _norm(s) not in text:
                correct += 1
            else:
                leaks.append(f"{fx.name}: {s!r}")
        for s in fx.must_survive:
            total += 1
            if _norm(s) in text:
                correct += 1
            else:
                over.append(f"{fx.name}: {s!r}")
    return correct, total, leaks, over


def test_second_pass_score():
    """The number the deployment bar is set against, and what it cost.

    Recall is measured over must_mask (did the PII go), specificity over
    must_survive (did the resume survive). Both are reported with and
    without the second pass, so the score says what changed rather than just
    what it is.
    """
    after_c, after_t, after_leaks, after_over = _score(True)
    before_c, before_t, before_leaks, before_over = _score(False)

    pii_total = sum(len(f.must_mask) for f in FIXTURES)
    content_total = sum(len(f.must_survive) for f in FIXTURES)

    def line(tag, correct, total, leaks, over):
        return (f"\n  {tag:<18} accuracy {correct / total:.4f} ({correct}/{total})"
                f"   recall {(pii_total - len(leaks)) / pii_total:.4f}"
                f"   specificity {(content_total - len(over)) / content_total:.4f}"
                f"   leaks {len(leaks)}   over-masked {len(over)}")

    report = ("\nSECOND-PASS SCORE"
              + line("first pass only", before_c, before_t, before_leaks, before_over)
              + line("both passes", after_c, after_t, after_leaks, after_over)
              + f"\n  leaks closed: {len(before_leaks) - len(after_leaks)}"
              + f"\n  still leaking: {after_leaks}"
              + f"\n  over-masked:   {after_over}")
    print(report)

    assert len(after_leaks) <= MAX_LEAKS, report
    assert after_c / after_t >= ACCURACY_FLOOR, report
    # The point of the exercise: the second pass must close leaks, and must
    # not pay for them out of the resume's content.
    assert len(after_leaks) < len(before_leaks), report
    assert len(after_over) <= len(before_over), report


def test_sweep_is_not_quadratic_on_a_long_page():
    """The scan runs per line, over a page of text, on every masked resume.

    Nested quantifiers in an address pattern are the classic way this
    degrades from milliseconds to minutes on a resume that is mostly prose.
    """
    import time
    lines = [f"Project {i}: delivered 2021, saved 45000 USD, contact acme. "
             f"Reviewed 1200 tickets and 3400 tests, ref 4821-9930-1177"
             for i in range(120)]
    start = time.monotonic()
    _masked_text(lines, ["Rahul Sharma"])
    assert time.monotonic() - start < 20.0
