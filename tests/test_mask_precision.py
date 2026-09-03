"""Masking precision regression suite.

The service exists to remove exactly three things from a resume — candidate
name, phone number(s), email address(es) — and to leave everything else on the
page alone. Both halves of that are defects when they fail, in opposite
directions:

    a MISS   leaks candidate PII to the client        (compliance)
    a FALSE  blanks out real resume content           (visible to the recruiter)

So this suite scores both. Every fixture declares the strings that must be
gone from the masked PDF and the strings that must still be there, each one
counts as a decision, and the run asserts an overall accuracy floor of 96%
plus zero PII leaks.

The fixtures run the *production* path end-to-end: build a PDF, derive
mask_strings the way server.mask_endpoint does (Salesforce Contact fields
merged with server.detect_pii of the resume text), mask, then read the text
back out of the masked PDF. Checking the output PDF's extracted text is the
real ground truth — redaction deletes glyphs, so a string that survives
extraction genuinely survived masking.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mask, pii  # noqa: E402
from app.server import detect_pii  # noqa: E402

#: The floor the user set for production. Kept as a named constant so a
#: regression reads as "accuracy dropped below the agreed bar", not as an
#: anonymous number in an assert.
ACCURACY_FLOOR = 0.96


def _norm(s: str) -> str:
    """Collapse whitespace away before substring tests.

    PDF text extraction is free to re-space a line (and does, around redacted
    regions), so "2019 - 2023" can come back as "2019-2023". Comparing
    whitespace-stripped text keeps the assertions about content rather than
    about layout.
    """
    return re.sub(r"\s+", "", s)


def _make_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 60.0
    for line in lines:
        if line:
            page.insert_text((56, y), line, fontsize=10)
        y += 15.5
        if y > 760:                     # spill onto a second page
            page = doc.new_page()
            y = 60.0
    out = doc.tobytes()
    doc.close()
    return out


class Fixture:
    """One resume plus what masking it is supposed to do.

    `contact` is what the Salesforce Contact record holds (name, phone, email
    — the trusted structured source). `must_mask` and `must_survive` are the
    scored expectations.
    """

    def __init__(self, name: str, lines: list[str], contact: list[str],
                 must_mask: list[str], must_survive: list[str]):
        self.name = name
        self.lines = lines
        self.contact = contact
        self.must_mask = must_mask
        self.must_survive = must_survive

    def mask_strings(self) -> list[str]:
        """Exactly how server.mask_endpoint builds the list: Contact fields
        first, then the resume-text regex scan, deduped."""
        pdf = _make_pdf(self.lines)
        seen: set[str] = set()
        out: list[str] = []
        for s in list(self.contact) + detect_pii(pdf):
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def run(self) -> str:
        """Mask the fixture and return the masked PDF's extracted text."""
        pdf = _make_pdf(self.lines)
        masked, _ = mask.mask_pdf_bytes(pdf, self.mask_strings(), watermark_text="")
        doc = fitz.open(stream=masked, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
        return text


# --- the corpus -----------------------------------------------------------
# Every `must_survive` entry below is a real thing resumes contain that the
# old `\+?\d[\d ().\-]{8,}\d` detector either did redact or could redact.

FIXTURES = [
    Fixture(
        "numeric-dense-indian-resume",
        [
            "RAHUL SHARMA",
            "Email: rahul.sharma@example.com | Phone: +91 98765 43210",
            "",
            "EXPERIENCE",
            "Senior Engineer, Acme Corp                     2019 - 2023",
            "Software Engineer, Globex                 06/2016 - 05/2019",
            "  - Cut p99 latency by 45% and raised throughput to 1200 req/s",
            "  - Owned a budget of 2,500,000 INR across 12 projects",
            "",
            "EDUCATION",
            "B.Tech, IIT Bombay, 2012 - 2016, CGPA 8.94/10.0",
            "",
            "CERTIFICATIONS",
            "AWS Certified, Credential ID 4821-9930-1177",
            "ISO 9001:2015 Lead Auditor, Cert No. 00219384",
            "IEEE 802.11ac / RFC 2616 contributor",
            "",
            "SKILLS: Python 3.10, Java 8, PostgreSQL 14.2",
            "Address: 221-B, Sector 45, Gurugram 122003",
        ],
        contact=["Rahul Sharma", "+919876543210", "rahul.sharma@example.com"],
        must_mask=["Rahul Sharma", "rahul.sharma@example.com", "98765 43210"],
        must_survive=[
            "2019 - 2023", "06/2016 - 05/2019", "2012 - 2016",
            "45%", "1200", "2,500,000", "8.94/10.0",
            "4821-9930-1177", "00219384", "802.11ac", "2616",
            "3.10", "14.2", "122003", "221-B",
        ],
    ),
    Fixture(
        "us-format-phone-and-ids",
        [
            "JORDAN A. WHITFIELD",
            "jordan.whitfield@example.org  |  (415) 555-0132  |  linkedin.com/in/jaw",
            "",
            "PROFESSIONAL EXPERIENCE",
            "Director of Engineering, Initech          Jan 2018 - Present",
            "Staff Engineer, Umbrella Co.              2014 - 2018",
            "  - Scaled the platform from 50000 to 1200000 monthly users",
            "  - Reduced cloud spend from 480000 to 310000 per year",
            "",
            "EDUCATION",
            "M.S. Computer Science, Stanford University, 2012",
            "Student ID 20120847713",
            "",
            "PUBLICATIONS",
            "ISBN 978-3-16-148410-0, DOI 10.1000/182",
            "Patent US 9876543 B2, filed 2015",
            "",
            "Mailing: 1600 Amphitheatre Parkway, Mountain View, CA 94043",
        ],
        contact=["Jordan A. Whitfield", "(415) 555-0132", "jordan.whitfield@example.org"],
        must_mask=["Jordan A. Whitfield", "jordan.whitfield@example.org", "555-0132"],
        must_survive=[
            "2018 - Present", "2014 - 2018", "50000", "1200000",
            "480000", "310000", "20120847713",
            "978-3-16-148410-0", "10.1000/182", "9876543", "94043",
            "1600 Amphitheatre Parkway",
        ],
    ),
    Fixture(
        # The name is a prefix of an unrelated word on the page. search_for()
        # has no word-boundary option, so a naive hit blanks the city name too.
        "name-is-a-prefix-of-another-word",
        [
            "Sunny",
            "sunny@example.net   Mobile 9876501234",
            "",
            "Relocated to Sunnyvale, CA in 2019 and to Sunnyside in 2021.",
            "Sunnybrook Labs - Research Assistant, 2017 - 2019",
        ],
        contact=["Sunny", "9876501234", "sunny@example.net"],
        must_mask=["sunny@example.net", "9876501234"],
        must_survive=["Sunnyvale", "Sunnyside", "Sunnybrook", "2017 - 2019", "2021"],
    ),
    Fixture(
        # Salesforce stores E.164; the resume renders a spaced national number.
        # The digit-equivalence matcher has to bridge that without also
        # matching the 8- and 11-digit identifiers on the page.
        "phone-format-mismatch-with-nearby-ids",
        [
            "PRIYA VENKATESAN",
            "priya.v@example.com",
            "Mob: 98765 43210",
            "",
            "Employee No 98765432",
            "Order Reference 4312233445566",
            "Invoice 987654321",
            "Aadhaar 1234 5678 9012",
            "Batch 2019 - 2021",
        ],
        contact=["Priya Venkatesan", "+91 98765 43210", "priya.v@example.com"],
        must_mask=["Priya Venkatesan", "priya.v@example.com", "98765 43210"],
        # An id that *ends* in the phone's own ten digits is a separate case,
        # covered precisely by test_id_labelled_run_is_not_masked_as_phone --
        # it cannot be scored here because a surviving id containing those
        # digits is indistinguishable from a leak by substring search.
        must_survive=["98765432", "4312233445566", "987654321",
                      "1234 5678 9012", "2019 - 2021"],
    ),
    Fixture(
        # A second email written only in the resume body, absent from the
        # Contact record — this is what the regex fallback is actually for.
        "second-email-only-in-resume",
        [
            "ANIL KUMAR",
            "Primary: anil.kumar@example.com",
            "Alternate: anil.k.personal@example.co.in",
            "Tel: 022-2345-6789",
            "",
            "Revenue grown 3400000 to 9100000 between 2020 and 2023.",
            "Version 2.7.1 release owner. Rank 14 of 3200.",
        ],
        contact=["Anil Kumar", "022-2345-6789", "anil.kumar@example.com"],
        must_mask=["Anil Kumar", "anil.kumar@example.com",
                   "anil.k.personal@example.co.in", "022-2345-6789"],
        must_survive=["3400000", "9100000", "2020", "2023", "2.7.1", "3200"],
    ),
    Fixture(
        # No Contact record at all (resolve_contact_id returned nothing), so
        # only the regex fallback runs. The name is expected to survive here —
        # detect_pii has no name detection, by design.
        "regex-fallback-only",
        [
            "MEERA IYER",
            "meera.iyer@example.com  +91-9812345678",
            "",
            "Timeline: 2015 - 2018, 2018 - 2022, 2022 - Present",
            "Certificates: 1102-4455-7788, 5566-1122-3344",
            "Metrics: 99.99% uptime, 250 ms p95, 15000 rps",
        ],
        contact=[],
        must_mask=["meera.iyer@example.com", "9812345678"],
        must_survive=["2015 - 2018", "2018 - 2022", "2022 - Present",
                      "1102-4455-7788", "5566-1122-3344",
                      "99.99%", "250", "15000"],
    ),
    Fixture(
        # Two pages: a phone candidate must not form across the page seam, and
        # PII repeated in a page footer must still be caught on both pages.
        "multi-page-with-repeated-footer",
        ["DEEPAK RAO", "deepak.rao@example.com", "Cell: 9900112233", ""]
        + [f"Project {i}: delivered in 2021, saved 45000 USD, {i}00 users" for i in range(40)]
        + ["deepak.rao@example.com  |  9900112233"],
        contact=["Deepak Rao", "9900112233", "deepak.rao@example.com"],
        must_mask=["Deepak Rao", "deepak.rao@example.com", "9900112233"],
        must_survive=["2021", "45000", "Project 39"],
    ),
]


# --- detector-level trap table -------------------------------------------
# Cheap, no PDF. Each entry is a line of resume text and the phone numbers
# that should be detected in it -- empty means "nothing here is a phone".

PHONE_TRAPS = [
    # --- must detect ---
    ("Phone: +91 98765 43210", ["+91 98765 43210"]),
    ("Mobile: 9876543210", ["9876543210"]),
    ("Tel: (415) 555-0132", ["(415) 555-0132"]),
    ("Contact 555-123-4567", ["555-123-4567"]),
    ("M: +1 (555) 123-4567", ["+1 (555) 123-4567"]),
    ("98765 43210", ["98765 43210"]),
    ("Ph. 022-2345-6789", ["022-2345-6789"]),
    ("+919876543210", ["+919876543210"]),
    ("Mob 0755-123456", ["0755-123456"]),
    ("Cell 9876-543-210", ["9876-543-210"]),
    # --- must NOT detect ---
    ("Senior Engineer, Acme Corp   2019 - 2023", []),
    ("B.Tech, 2012 - 2016, CGPA 8.94/10.0", []),
    ("Employment 06/2016 - 05/2019", []),
    ("Credential ID 4821-9930-1177", []),
    ("Cert No. 00219384", []),
    ("ISO 9001:2015 Lead Auditor", []),
    ("IEEE 802.11ac / RFC 2616", []),
    ("Python 3.10, PostgreSQL 14.2, Java 8", []),
    ("Cut latency by 45%, throughput 1200 req/s", []),
    ("Budget of 2,500,000 INR across 12 projects", []),
    ("Gurugram 122003", []),
    ("Roll No 12345678", []),
    ("Aadhaar 1234 5678 9012", []),
    ("ISBN 978-3-16-148410-0", []),
    ("DOI 10.1000/182", []),
    ("Student ID 20120847713", []),
    ("Order Reference 4319876543210", []),
    ("Invoice 987654321", []),
    ("Scaled from 50000 to 1200000 monthly users", []),
    ("Salary 1200000 per annum", []),
    ("Account 000123456789", []),
    ("Patent US 9876543 B2", []),
    ("Maximum: 1234567 records processed", []),
    ("Timeline 2015 - 2018, 2018 - 2022", []),
    ("Serial 4455-6677-8899-0011", []),
    ("Version 2.7.1, build 20240115", []),
    ("Rank 14 of 3200 candidates", []),
    ("99.99% uptime, 250 ms p95", []),
    ("Mailing: 1600 Amphitheatre Parkway, CA 94043", []),
    ("Postal code 400001", []),
    ("IFSC HDFC0001234", []),
    ("Marks 456789 out of 500000", []),
]


@pytest.mark.parametrize("text,expected", PHONE_TRAPS,
                         ids=[t[0][:40] for t in PHONE_TRAPS])
def test_phone_detector_trap(text, expected):
    """The detector must find real phones and nothing else."""
    assert pii.find_phones(text) == expected


def test_phone_digits_equivalence():
    """Country code / trunk prefix tolerated; anything else is not."""
    eq = pii.phone_digits_equivalent
    assert eq("919876543210", "9876543210")       # E.164 vs national
    assert eq("09876543210", "9876543210")        # trunk prefix
    assert eq("9876543210", "9876543210")
    # All four were accepted by the old "substring either way, +/-3 digits" rule.
    assert not eq("98765432", "9876543210")       # id that only prefixes the number
    assert not eq("98765432109", "9876543210")    # differs at the end
    assert not eq("1239876543210", "98765432")    # short local number, long run
    assert not eq("12349876543210", "9876543210")  # 4-digit prefix is too much


def test_id_labelled_run_is_not_masked_as_phone():
    """A longer identifier ending in the candidate's own digits must survive.

    As bare digits "4319876543210" is a legitimate +43 number and also a
    legitimate order reference — only the label to its left separates them, so
    this is the case _phone_rects resolves from context rather than from the
    number itself.
    """
    pdf = _make_pdf(["Mob: 98765 43210", "Order Reference 4319876543210"])
    masked, _ = mask.mask_pdf_bytes(pdf, ["+919876543210"], watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    text = _norm("\n".join(p.get_text() for p in doc))
    doc.close()
    assert "4319876543210" in text
    assert "9876543210" not in text.replace("4319876543210", "")


# Shapes taken from real PhoneNumber__c values on the live org, where 439 of
# ~1990 Contacts (22%) hold more than one number in that single field. Digits
# are substituted but the separator layout is exactly as stored.
MULTI_PHONE_VALUES = [
    ("9876543210    9123456789",       ["9876543210", "9123456789"]),
    ("9876543210  22  9123456789",     ["9876543210", "9123456789"]),
    ("9876543210   123456",            ["9876543210"]),
    ("9876543210   123456 9123456789", ["9876543210", "9123456789"]),
    ("9876543210 +91-9123456789",      ["9876543210", "+91-9123456789"]),
    ("9876543210 (A)",                 ["9876543210"]),
    ("9876543210, 9123456789",         ["9876543210", "9123456789"]),
    ("9876543210 / 9123456789",        ["9876543210", "9123456789"]),
]


@pytest.mark.parametrize("value,expected", MULTI_PHONE_VALUES,
                         ids=[v[0][:34] for v in MULTI_PHONE_VALUES])
def test_multi_number_contact_field_is_split(value, expected):
    """A Contact field holding several numbers must yield each one.

    Left whole these values exceed MAX_PHONE_DIGITS, so classify() calls them
    a name and they get searched as one long literal that appears in no
    resume -- the candidate's phone then goes unmasked from the Contact
    record entirely."""
    assert pii.split_phone_list(value) == expected


@pytest.mark.parametrize("value", [
    "Rahul Sharma",
    "Rahul  Sharma",          # two spaces: still one name, not two numbers
    "Sean O Brien",
    "rahul.sharma@example.com",
    "9876543210",
    "+91 98765 43210",
    "98765 43210",            # the single space is INSIDE the number
    "022-2345-6789",
])
def test_single_values_are_never_split(value):
    """Splitting must not touch a name, an email, or one ordinary number."""
    assert pii.split_phone_list(value) == [value]


def test_multi_number_field_masks_every_number_and_nothing_else():
    """End-to-end for the 22% case, against the traps it sits next to."""
    pdf = _make_pdf([
        "PRIYA VENKATESAN",
        "Mobile: 9876543210",
        "Alternate: 9123456789",
        "Senior Engineer, Acme Corp    2019 - 2023",
        "B.Tech 2012 - 2016, CGPA 8.94/10.0",
        "Credential ID 4821-9930-1177",
    ])
    # exactly as it comes off the Contact record
    masked, _ = mask.mask_pdf_bytes(
        pdf, ["Priya Venkatesan", "9876543210    9123456789"], watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    text = _norm(doc[0].get_text())
    doc.close()

    assert "9876543210" not in text, "first number leaked"
    assert "9123456789" not in text, "second number leaked"
    assert "PriyaVenkatesan" not in text.replace("PRIYA", "Priya")
    for survivor in ("2019-2023", "2012-2016", "8.94/10.0", "4821-9930-1177"):
        assert survivor in text, f"over-masked {survivor}"


def test_expand_dedupes_across_fields():
    """The same number in two Contact fields must not be redacted twice."""
    assert pii.expand(["9876543210    9123456789", "9123456789", "", None]) == \
        ["9876543210", "9123456789"]


def test_name_shorter_than_three_chars_is_never_matched():
    """A 1-2 character name would hit half the page; refuse rather than guess."""
    pdf = _make_pdf(["Li Wei", "An analysis of an anomaly in Anaheim."])
    masked, hits = mask.mask_pdf_bytes(pdf, ["An"], watermark_text="")
    doc = fitz.open(stream=masked, filetype="pdf")
    text = doc[0].get_text()
    doc.close()
    assert hits == 0
    assert "analysis" in text and "anomaly" in text and "Anaheim" in text


def test_redaction_fill_is_white():
    """Redacted regions must come out white, not black."""
    pdf = _make_pdf(["Rahul Sharma", "Phone: 9876543210"])
    masked, hits = mask.mask_pdf_bytes(pdf, ["9876543210"], watermark_text="")
    assert hits >= 1

    doc = fitz.open(stream=masked, filetype="pdf")
    page = doc[0]
    # The number is gone, so locate the region by where it used to be.
    src = fitz.open(stream=pdf, filetype="pdf")
    rect = src[0].search_for("9876543210")[0]
    src.close()

    pix = page.get_pixmap(clip=rect)
    doc.close()
    pixels = {pix.pixel(x, y)
              for x in range(0, pix.width, max(1, pix.width // 12))
              for y in range(0, pix.height, max(1, pix.height // 4))}
    assert pixels == {(255, 255, 255)}, f"redacted area is not white: {sorted(pixels)}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
def test_fixture_masks_only_pii(fixture):
    """Per-fixture view: nothing leaks, nothing extra is blanked."""
    text = _norm(fixture.run())

    leaked = [s for s in fixture.must_mask if _norm(s) in text]
    destroyed = [s for s in fixture.must_survive if _norm(s) not in text]

    assert not leaked, f"{fixture.name}: PII survived masking: {leaked}"
    assert not destroyed, f"{fixture.name}: non-PII content was masked: {destroyed}"


def test_overall_accuracy_meets_floor():
    """Aggregate score across the whole corpus.

    This is the number the deployment bar is set against: every must-mask and
    must-survive expectation in every fixture counts as one decision.
    """
    correct = total = 0
    leaks: list[str] = []
    false_positives: list[str] = []

    for fx in FIXTURES:
        text = _norm(fx.run())
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
                false_positives.append(f"{fx.name}: {s!r}")

    accuracy = correct / total
    report = (f"\naccuracy {accuracy:.4f} ({correct}/{total} decisions)"
              f"\nPII leaks ({len(leaks)}): {leaks}"
              f"\nover-masked ({len(false_positives)}): {false_positives}")

    # A leak is a compliance failure, not a rounding error -- held to zero
    # independently of the accuracy floor.
    assert not leaks, report
    assert accuracy >= ACCURACY_FLOOR, report
    print(report)
