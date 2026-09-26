"""A resume built from the Contact record, for applicants who never sent one.

Some Job Applicants have no resume file anywhere -- not on the Job Applicant,
not on the Contact, in neither Files nor Attachments -- so /mask used to stop
at "No resume found". The recruiter still has a profile to share: the Contact
carries the candidate's designation, company, experience, skills, education
and personal details. This lays those out in the house template (the layout of
Arvind_EMS-SME.pdf) and hands the result to the normal masking pipeline.

Two rules shape everything here:

  * Nothing is invented. Every line on the page is a Contact field, or a
    sentence assembled only from Contact fields. A section with no data behind
    it is left out rather than padded, and work experience is exactly the
    current company, designation and years the record holds -- no bullets, no
    projects, no employers the record does not name.

  * The name, email and phone are never read. The output is always masked, so
    they are not in PROFILE_FIELDS at all and cannot end up on the page. The
    masking pass run over the result afterwards is a safety net for the free
    text fields (a phone typed into Educational_Details__c), not the defence.
"""
from __future__ import annotations

import datetime as _dt
import html
import io
import re

import fitz

#: Contact fields read for the template, by API name. The name/email/phone
#: fields are deliberately absent -- see the module docstring.
PROFILE_FIELDS = (
    "CurrentDesignation__c", "CurrentCompany__c",
    "worked_experience__c", "Years_of_Experience__c",
    "Current_Location__c",
    "SCSCHAMPS__Primary_Skills__c", "SCSCHAMPS__Technical_Skills__c", "Skill_List__c",
    "Highest_Qualification__c", "Educational_Details__c",
    "current_ctc__c", "SCSCHAMPS__Current_CTC__c", "SCSCHAMPS__Expected_CTC__c",
    "SCSCHAMPS__Notice_Period__c",
    "DateOfBirth__c", "date_ofbirth__c", "SCSCHAMPS__Gender__c",
    "Nationnality__c", "LanguagesKnown__c",
)

#: The org has two fields for several of these; the first one filled wins.
_YOE = ("Years_of_Experience__c", "worked_experience__c")
_CURRENT_CTC = ("SCSCHAMPS__Current_CTC__c", "current_ctc__c")
_DOB = ("DateOfBirth__c", "date_ofbirth__c")

#: Without at least one of these there is no profile to show, only personal
#: details, and a page of those is not worth sending to a client.
_SUBSTANCE = ("CurrentDesignation__c", "CurrentCompany__c", "SCSCHAMPS__Primary_Skills__c",
              "SCSCHAMPS__Technical_Skills__c", "Skill_List__c", "Highest_Qualification__c",
              "Educational_Details__c") + _YOE

_NAVY = "#1F3864"
_ACCENT = "#2E4A8B"


def _text(value) -> str:
    """A field value as display text; '' for blank."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _first(profile: dict, fields: tuple[str, ...]) -> str:
    for f in fields:
        v = _text(profile.get(f))
        if v:
            return v
    return ""


def _split_list(value) -> list[str]:
    """Multi-picklists arrive ';'-joined, hand-typed lists with commas,
    pipes or newlines. Returned in order, case-insensitively de-duplicated."""
    raw = "" if value is None else str(value)
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;,|\n\r•]+", raw):
        part = _text(part)
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


def _years(value: str) -> str:
    """'4' -> '4 years'; anything already worded is left alone."""
    try:
        n = float(value)
    except ValueError:
        return value
    if n.is_integer():
        n = int(n)
    return f"{n} year" if n == 1 else f"{n} years"


def _date(value: str) -> str:
    """Salesforce dates arrive as ISO 'YYYY-MM-DD'; shown as '07 June 2001'."""
    try:
        return _dt.date.fromisoformat(value[:10]).strftime("%d %B %Y")
    except ValueError:
        return value


def has_enough(profile: dict | None) -> bool:
    return bool(profile) and any(_text(profile.get(f)) for f in _SUBSTANCE)


def _skills(profile: dict) -> tuple[list[str], list[str]]:
    """(primary, other) -- other excludes anything already listed as primary."""
    primary = _split_list(profile.get("SCSCHAMPS__Primary_Skills__c"))
    seen = {s.lower() for s in primary}
    other: list[str] = []
    for f in ("SCSCHAMPS__Technical_Skills__c", "Skill_List__c"):
        for s in _split_list(profile.get(f)):
            if s.lower() not in seen:
                seen.add(s.lower())
                other.append(s)
    return primary, other


def _summary(profile: dict, skills: list[str]) -> str:
    """A summary assembled only from fields -- each clause exists only when
    its field does, so it can never claim anything the record doesn't."""
    designation = _text(profile.get("CurrentDesignation__c"))
    company = _text(profile.get("CurrentCompany__c"))
    yoe = _first(profile, _YOE)
    parts = []
    lead = designation or "Professional"
    if yoe:
        lead += f" with {_years(yoe)} of experience"
    if company:
        lead += f", currently working at {company}"
    parts.append(lead.rstrip(".") + ".")
    if skills:
        parts.append("Core skills include " + ", ".join(skills[:8]).rstrip(".") + ".")
    qualification = _text(profile.get("Highest_Qualification__c"))
    if qualification:
        parts.append(f"Holds {qualification.rstrip('.')}.")
    return " ".join(parts) if (designation or yoe or company or skills or qualification) else ""


def _e(s: str) -> str:
    return html.escape(s, quote=False)


def _section(title: str, body: str) -> str:
    return f'<h2>{_e(title)}</h2>{body}'


def _details_line(pairs: list[tuple[str, str]]) -> str:
    items = [f"<b>{_e(k)}:</b> {_e(v)}" for k, v in pairs if v]
    return "<p>" + " &#160;|&#160; ".join(items) + "</p>" if items else ""


def build_html(profile: dict) -> str:
    designation = _text(profile.get("CurrentDesignation__c"))
    company = _text(profile.get("CurrentCompany__c"))
    location = _text(profile.get("Current_Location__c"))
    yoe = _first(profile, _YOE)
    primary, other = _skills(profile)

    out = ['<p class="title">CANDIDATE PROFILE</p>']
    if designation:
        out.append(f'<p class="subtitle">{_e(designation)}</p>')
    meta = [x for x in (location, f"{_years(yoe)} experience" if yoe else "") if x]
    if meta:
        out.append(f'<p class="meta">{" &#160;|&#160; ".join(_e(m) for m in meta)}</p>')

    summary = _summary(profile, primary + other)
    if summary:
        out.append(_section("PROFESSIONAL SUMMARY", f"<p>{_e(summary)}</p>"))

    if primary or other:
        rows = []
        if primary:
            rows.append(f'<p><b class="k">Primary Skills:</b> {_e(", ".join(primary))}</p>')
        if other:
            rows.append(f'<p><b class="k">Technical Skills:</b> {_e(", ".join(other))}</p>')
        out.append(_section("TECHNICAL SKILLS", "".join(rows)))

    if company or designation or yoe:
        head = " &#183; ".join(x for x in (
            f"<b>{_e(company)}</b>" if company else "",
            f'<b class="k">{_e(designation)}</b>' if designation else "") if x)
        right = f"<i>{_e(_years(yoe))} total experience</i>" if yoe else ""
        out.append(_section("WORK EXPERIENCE",
            '<table class="row"><tr>'
            f'<td>{head or "&#160;"}</td><td class="right">{right}</td>'
            "</tr></table>"))

    qualification = _text(profile.get("Highest_Qualification__c"))
    education = _text(profile.get("Educational_Details__c"))
    if qualification or education:
        rows = []
        if qualification:
            rows.append(f'<p><b class="k">Highest Qualification:</b> {_e(qualification)}</p>')
        if education and education.lower() != qualification.lower():
            rows.append(f"<p>{_e(education)}</p>")
        out.append(_section("EDUCATION", "".join(rows)))

    career = _details_line([
        ("Current CTC", _first(profile, _CURRENT_CTC)),
        ("Expected CTC", _text(profile.get("SCSCHAMPS__Expected_CTC__c"))),
        ("Notice Period", _text(profile.get("SCSCHAMPS__Notice_Period__c"))),
    ])
    if career:
        out.append(_section("CAREER DETAILS", career))

    dob = _first(profile, _DOB)
    languages = ", ".join(_split_list(profile.get("LanguagesKnown__c")))
    personal = _details_line([
        ("Date of Birth", _date(dob) if dob else ""),
        ("Gender", _text(profile.get("SCSCHAMPS__Gender__c"))),
        ("Nationality", _text(profile.get("Nationnality__c"))),
        ("Languages", languages),
    ])
    if personal:
        out.append(_section("PERSONAL DETAILS", personal))

    return "<body>" + "".join(out) + "</body>"


_CSS = f"""
* {{ font-family: sans-serif; font-size: 9.5pt; color: #222222; }}
p {{ margin: 0 0 3pt 0; line-height: 1.3; }}
.title {{ font-size: 20pt; font-weight: bold; text-align: center; color: #111111; margin: 0; }}
.subtitle {{ font-size: 10.5pt; font-weight: bold; text-align: center; color: {_ACCENT}; margin: 2pt 0 0 0; }}
.meta {{ text-align: center; color: #555555; margin: 2pt 0 6pt 0; }}
h2 {{ font-size: 10.5pt; font-weight: bold; color: {_NAVY}; margin: 12pt 0 4pt 0; }}
b {{ font-weight: bold; }}
.k {{ color: {_NAVY}; }}
table.row {{ width: 100%; }}
td {{ padding: 0; }}
td.right {{ text-align: right; color: #555555; }}
"""

_PAGE = fitz.paper_rect("letter")
_MARGIN = (54, 48, 54, 48)


def render(profile: dict) -> bytes:
    """The template resume for `profile` (Contact fields by API name), as PDF bytes."""
    story = fitz.Story(html=build_html(profile), user_css=_CSS)
    body = _PAGE + (_MARGIN[0], _MARGIN[1], -_MARGIN[2], -_MARGIN[3])
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more = 1
    while more:
        device = writer.begin_page(_PAGE)
        more, _ = story.place(body)
        story.draw(device)
        writer.end_page()
    writer.close()
    return _rule_sections(buf.getvalue())


def _rule_sections(pdf_bytes: bytes) -> bytes:
    """Draw the template's thin rule above each section heading.

    Story's CSS has no borders, so the rules go on afterwards, found by the
    heading text -- which is also what keeps them exactly aligned with it.
    """
    headings = ("PROFESSIONAL SUMMARY", "TECHNICAL SKILLS", "WORK EXPERIENCE", "EDUCATION",
                "CAREER DETAILS", "PERSONAL DETAILS")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    x0, x1 = _PAGE.x0 + _MARGIN[0], _PAGE.x1 - _MARGIN[2]
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if text in headings:
                    y = line["bbox"][1] - 5
                    page.draw_line((x0, y), (x1, y), color=(0.18, 0.29, 0.55), width=0.6)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out
