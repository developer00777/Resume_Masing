"""PII detection & classification -- precision-first.

The service must redact exactly three things and nothing else:

    candidate name, phone number(s), email address(es)

Everything else a resume contains has to survive untouched. Resumes are dense
with numbers that a permissive phone regex happily swallows, and every one of
those is a visible defect in the masked copy the recruiter reads:

    employment/education date ranges  2019 - 2023, 2012 - 2016
    credential & certificate ids      4821-9930-1177, Cert No. 00219384
    standards / references            ISO 9001:2015, IEEE 802.11ac, RFC 2616
    library versions                  Python 3.10, PostgreSQL 14.2
    metrics                           45%, 1200 req/s, CGPA 8.94/10.0
    money, ids, pin/zip               2,500,000 INR, Roll No 12345678, 122003

The previous detector was `+?\\d[\\d ().-]{8,}\\d` -- "a long-ish run of digits
and separators" -- which matched the first three groups above outright.

Design (the part that matters): a digit run is a phone number only if it
carries a POSITIVE signal that it is one. Absence of evidence is a reject, not
an accept. That single inversion is what keeps date ranges, ISBNs and
credential ids out, because none of them look like a dialable number or sit
behind a phone label. A second reject layer then catches the rare non-phone
that is labelled or shaped like one anyway.

Two different jobs live here, and they are deliberately NOT the same
strictness:

  * DETECTION (`find_phones`/`find_emails`) reads untrusted free text scraped
    out of a PDF, so it is strict -- a miss is recoverable (the Salesforce
    Contact record usually carries the same value, and both sources are
    merged), a false positive is not.
  * CLASSIFICATION (`classify`) labels a string we have already been told to
    mask -- a Salesforce Contact field, or one the caller passed explicitly.
    That value is trusted; we only need to know which matching strategy it
    wants, not whether to honour it.
"""
from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\d+")

#: E.164 caps a full international number at 15 digits, so nothing longer can
#: be a phone however it is punctuated. 7 is the shortest local number.
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15

EN_DASH = "–"
EM_DASH = "—"


def digits(s: str) -> str:
    """Just the digits of `s`, in order ("+91 98765 43210" -> "919876543210")."""
    return "".join(_DIGITS_RE.findall(str(s)))


# --- email ----------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def find_emails(text: str) -> list[str]:
    """Email addresses in `text`. The shape is distinctive enough that this
    needs no validation layer -- an '@' with a dotted TLD after it is not
    something resume prose produces by accident."""
    return EMAIL_RE.findall(text)


# --- phone ----------------------------------------------------------------

# Candidate harvesting. Note [ \t] and NOT \s: \s matches newlines, which lets
# a run span two unrelated stacked lines (a left-column date and a right-column
# one) and fuse them into a number that was never on the page. Kept
# deliberately loose because every candidate then goes through _is_phone();
# this regex only bounds where we look.
# The optional leading "(" matters: without it the candidate for
# "(415) 555-0132" starts at the 4, and "415) 555-0132" matches none of the
# shapes below, so a perfectly ordinary US number went undetected.
_PHONE_CAND_RE = re.compile(r"\+?\(?\d[\d \t().\-" + EN_DASH + EM_DASH + r"]{4,}\d")

#: Dialable shapes we recognise outright, matched end-to-end. This is the main
#: positive signal. Every entry is a real-world phone layout, and none is a
#: shape a date range, version string or ISBN can take.
_PHONE_SHAPES = tuple(re.compile(p) for p in (
    r"\(\d{3}\)[ .\-]?\d{3}[ .\-]?\d{4}",     # (555) 123-4567
    r"\d{3}[ .\-]\d{3}[ .\-]\d{4}",           # 555-123-4567
    r"\d{5}[ .\-]\d{5}",                      # 98765 43210   (IN mobile)
    r"\d{4}[ .\-]\d{3}[ .\-]\d{3}",           # 9876-543-210
    r"\d{4}[ .\-]\d{6}",                      # 0755-123456   (IN landline)
    r"0\d{2}[ .\-]\d{4}[ .\-]\d{4}",          # 022-2345-6789
    r"\d{10}",                                # 9876543210
))

#: A phone label sitting flush against the number (anchored with $). Checked
#: only after the id-label test below, so "Contact ID: 12345678" reads as an
#: id rather than as a contact number.
_PHONE_LABEL_RE = re.compile(
    r"(?:^|[^A-Za-z])"
    r"(?:phone|mobile|mob|cell|cellular|tel|telephone|contact|whatsapp|"
    r"ph|mo|m|t|c)"
    r"[ \t]*(?:no\.?|num(?:ber)?|#)?[ \t]*[:\-|" + EN_DASH + EM_DASH + r"]?[ \t]*$",
    re.I,
)

#: A label that positively identifies the run as something other than a phone.
#: Beats both the phone label and the shape test: "Employee 9876543210" is an
#: id even though a bare 10-digit run is otherwise a good phone shape.
#:
#: Deliberately no standalone "no"/"number"/"#" alternative -- those words only
#: mean "identifier" when they trail a noun ("Roll No", "Order Number"), which
#: the optional suffix group at the end already covers. As a standalone
#: alternative it also swallowed "Contact Number: 9876543210", classifying a
#: phone number as an id and leaking it.
_ID_LABEL_RE = re.compile(
    r"(?:^|[^A-Za-z])"
    r"(?:id|ids|credential|cert(?:ificate)?|licen[cs]e|"
    r"roll|reg(?:istration)?|enrol(?:l)?ment|seat|batch|badge|"
    r"pan|gst(?:in)?|tin|aadha?ar|uidai|passport|visa|ssn|"
    r"invoice|order|ref(?:erence)?|txn|transaction|"
    r"iso|iec|ieee|rfc|ansi|astm|isbn|issn|doi|orcid|patent|"
    r"pin(?:code)?|zip|postal|postcode|"
    r"acc(?:ount|t)?|ifsc|swift|iban|routing|"
    r"employee|emp|staff|student|matric|"
    r"score|rank|marks|salary|ctc|package|budget|revenue|"
    r"ver(?:sion)?|v|build|release|sku|part|model|serial)"
    r"[ \t]*(?:no\.?|num(?:ber)?|#)?[ \t]*[:\-|" + EN_DASH + EM_DASH + r"]?[ \t]*$",
    re.I,
)


def _year_like(group: str) -> bool:
    return len(group) == 4 and 1900 <= int(group) <= 2099


def _is_phone(candidate: str, pre_context: str = "") -> bool:
    """Is `candidate` actually a phone number?

    `pre_context` is the text immediately to its left on the same line, used
    only as label evidence. Returns False whenever there is no positive
    evidence -- see the module docstring for why reject is the default.
    """
    cand = candidate.strip().strip("-." + EN_DASH + EM_DASH + " \t")
    if not cand:
        return False

    if not (MIN_PHONE_DIGITS <= len(digits(cand)) <= MAX_PHONE_DIGITS):
        return False

    groups = _DIGITS_RE.findall(cand)

    # --- reject layer -----------------------------------------------------
    # Two 4-digit years is a date range ("2019 - 2023", "06-2016 - 05-2019"),
    # never a phone number.
    if sum(1 for g in groups if _year_like(g)) >= 2:
        return False
    # Three or more uniform 4-digit groups is a formatted identifier
    # ("4821-9930-1177", "1234 5678 9012").
    if len(groups) >= 3 and all(len(g) == 4 for g in groups):
        return False
    if _ID_LABEL_RE.search(pre_context):
        return False

    # --- positive evidence ------------------------------------------------
    if cand.startswith("+"):
        return True                                    # written for international dialling
    if _PHONE_LABEL_RE.search(pre_context):
        return True                                    # "Mobile: 12345678"
    return any(r.fullmatch(cand) for r in _PHONE_SHAPES)


def find_phones(text: str) -> list[str]:
    """Phone numbers in `text`. Strict: an unrecognised digit run is dropped."""
    out: list[str] = []
    for m in _PHONE_CAND_RE.finditer(text):
        # Same-line left context only -- a label on the previous line says
        # nothing about this run.
        pre = text[max(0, m.start() - 40):m.start()].rsplit("\n", 1)[-1]
        cand = m.group(0).strip()
        if _is_phone(cand, pre):
            out.append(cand)
    return out


# --- classification of strings we were told to mask -----------------------

#: Trusted-input shape test: phone punctuation only, nothing alphabetic.
_PHONE_STRING_RE = re.compile(r"[+()\d \t.\-" + EN_DASH + EM_DASH + r"/]+")

EMAIL, PHONE, NAME = "email", "phone", "name"


def classify(s: str) -> str:
    """Which matching strategy `s` needs when we go looking for it in a PDF.

    Lenient on purpose: `s` already comes from a trusted source, so this
    decides *how* to match it, not *whether* to. NAME is the catch-all, matched
    as a literal on whole-word boundaries.
    """
    s = str(s).strip()
    if EMAIL_RE.fullmatch(s):
        return EMAIL
    if _PHONE_STRING_RE.fullmatch(s) and MIN_PHONE_DIGITS <= len(digits(s)) <= MAX_PHONE_DIGITS:
        return PHONE
    return NAME


def is_id_context(pre_text: str) -> bool:
    """Does `pre_text` end in a label naming something that is not a phone?

    Used at mask time as well as at detection time. A trusted Contact phone
    number still has to be *located* on the page, and digit-equivalence alone
    cannot tell "+43 1 9876543210" from an order reference that happens to end
    in the same ten digits -- only the label to its left can.
    """
    return bool(_ID_LABEL_RE.search(pre_text))


def phone_digits_equivalent(a: str, b: str) -> bool:
    """Do two digit strings denote the same phone number?

    Tolerates a country code and/or trunk prefix on one side only -- the
    Contact record holds a normalised "+919876543210" while the resume renders
    "98765 43210" -- by anchoring on the END of the number, the part that never
    changes, and allowing at most a 3-digit prefix of difference. The previous
    rule accepted a substring match *anywhere* with a +/-3 length slack, so an
    unrelated id sitting inside a longer run counted as a hit.

    Suffix-anchoring cannot, on its own, rule out a longer identifier that
    happens to end in the same digits -- "+43 1 9876543210" and a 13-digit
    order reference are indistinguishable as bare digits. is_id_context()
    resolves that case from the surrounding text instead.
    """
    short, long = sorted((a, b), key=len)
    if len(short) < MIN_PHONE_DIGITS or len(long) > MAX_PHONE_DIGITS:
        return False
    extra = len(long) - len(short)
    if extra > 3 or not long.endswith(short):
        return False
    if extra == 0:
        return True
    if long[0] == "0":
        return True                     # national trunk prefix
    # A country code never starts with 0, so the remaining legitimate case is
    # CC + a complete national number. Requiring the short side to be a full
    # national number stops a 7-8 digit local number from matching the tail of
    # an arbitrary longer run.
    return len(short) >= 10
