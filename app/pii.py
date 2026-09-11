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
    # An unpunctuated, unlabelled ten-digit run, constrained to the Indian
    # mobile series. It used to be a bare \d{10}, which also accepts
    # "Processed 1500000000 records" and "Handled 1200000000 users" -- both
    # of which were being redacted out of real resumes. See is_indian_mobile()
    # below; the second pass applies the same rule, so the two agree.
    r"[6-9]\d{9}",                            # 9876543210
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
    r"ver(?:sion)?|v|build|release|sku|part|model|serial|code)"
    # "Employee Code 9876543210" reads as a mobile number the moment you stop
    # reading the label, so "code" belongs in the trailing noun group as well
    # as in the list above -- it is how HR systems name an id.
    r"[ \t]*(?:no\.?|num(?:ber)?|code|id|#)?[ \t]*"
    r"[:\-|" + EN_DASH + EM_DASH + r"]?[ \t]*$",
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


#: Separators that mean "and another number", as opposed to the single spaces
#: *inside* one number ("98765 43210"). Splitting on a single space would tear
#: every normally-formatted Indian mobile in half, so a single space is only
#: used as a last resort (see _regroup below). A "+" is always a new number,
#: however little whitespace precedes it.
_PHONE_LIST_SPLIT_RE = re.compile(r"\s{2,}|[,;/|\n\r]+|\s+(?=\+)")

#: One phone-shaped run inside a value that also contains other characters --
#: "9876543210 (A)", where a trailing annotation stops the whole value from
#: being recognisable as phone punctuation at all. Anchored to end on a digit
#: so it does not trail the separators leading up to the annotation.
_PHONE_RUN_RE = re.compile(r"\+?\d(?:[\d ().\-]*\d)?")


def _valid(part: str) -> bool:
    return MIN_PHONE_DIGITS <= len(digits(part)) <= MAX_PHONE_DIGITS


def _regroup(part: str) -> list[str]:
    """Recover numbers from an oversized part, splitting on single spaces.

    Handles "999999 9999999999" -- a 6-digit fragment and a real mobile
    separated by one space, which the primary split leaves fused at 16 digits.
    Accumulates tokens until they form a valid number and drops what cannot,
    rather than guessing at boundaries: a fabricated number would be worse
    than a missed one, since it could match unrelated resume content.
    """
    out: list[str] = []
    buffer: list[str] = []
    for token in part.split():
        candidate = buffer + [token]
        if len(digits(" ".join(candidate))) > MAX_PHONE_DIGITS:
            if buffer and _valid(" ".join(buffer)):
                out.append(" ".join(buffer))
            buffer = [token]
        else:
            buffer = candidate
    if buffer and _valid(" ".join(buffer)):
        out.append(" ".join(buffer))
    return out


def split_phone_list(value: str) -> list[str]:
    """Split one Contact field holding several phone numbers into each number.

    Confirmed against live data: 22% of Contacts on this org (439 of ~1990)
    have two or more numbers crammed into a single PhoneNumber__c
    ("9876543210    9123456789"), 16-32 digits in total. Left whole, such a
    value is over MAX_PHONE_DIGITS, so classify() calls it a NAME, so it gets
    looked for as one literal 20-digit string -- which appears in no resume.
    The candidate's phone then goes unmasked from the Contact record
    entirely, and only the resume text scan stands between that and a leak.

    Returns [value] unchanged unless the value really does hold multiple valid
    numbers, so a name that happens to contain two spaces is never torn apart.
    """
    value = str(value).strip()
    if len(digits(value)) <= MAX_PHONE_DIGITS and _PHONE_STRING_RE.fullmatch(value):
        return [value]                    # a single number already

    numbers: list[str] = []
    for part in (p.strip() for p in _PHONE_LIST_SPLIT_RE.split(value)):
        if not part:
            continue
        if _valid(part) and _PHONE_STRING_RE.fullmatch(part):
            numbers.append(part)
        elif len(digits(part)) > MAX_PHONE_DIGITS:
            numbers.extend(_regroup(part))
        else:
            # Mixed content ("9876543210 (A)"): keep only the phone-shaped runs.
            numbers.extend(r.strip() for r in _PHONE_RUN_RE.findall(part) if _valid(r))
    return numbers or [value]


def expand(values: list[str]) -> list[str]:
    """Every PII string to actually look for, deduped in first-seen order.

    Only splitting happens here: a multi-number phone field becomes one entry
    per number. Everything else passes through untouched.
    """
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or not str(value).strip():
            continue
        for part in split_phone_list(value):
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return out


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


# =========================================================================
# SECOND PASS -- residual sweep
# =========================================================================
# Everything above answers "is this string, which somebody handed us, PII?".
# Everything below answers a different question: once the values we were
# *told* about have been removed from the page, what PII is still sitting
# there?
#
# That is a strictly harder problem and it needs its own rules, because the
# values that leaked in production share one property -- no upstream system
# ever knew about them:
#
#     an alternate mobile typed into the resume body and nowhere else
#     a personal address next to the work one on the Contact record
#     "91 9876543210" and "09876543210" -- real numbers the first-pass
#         detector rejects, because neither matches a shape in _PHONE_SHAPES
#         and neither starts with "+"
#
# So this pass does not reuse the first pass's evidence rules. It replaces
# them with the actual numbering plan. India's NNP fixes every subscriber
# number at ten digits and reserves the 6/7/8/9 series for mobiles, which
# means a ten-digit run starting 6-9 *is* a mobile -- no label, no
# punctuation and no country code required. That one fact is what lets this
# pass be simultaneously more aggressive and more precise than the first: a
# bare \d{10} (which _PHONE_SHAPES accepts) also matches a unix timestamp, a
# ten-digit order id and a ten-figure revenue number, and [6-9]\d{9} matches
# none of them.
#
# Recall is bought from the numbering plan, not by loosening the guards --
# every reject rule the first pass applies is applied here too.

#: India's national significant number is exactly this long, always.
NSN_LEN = 10

#: The mobile series. Landline area codes live in 1-5; 6-9 are mobile only.
MOBILE_FIRST_DIGITS = frozenset("6789")

#: How the country code and/or trunk prefix get written in front of those ten
#: digits. Longest first, so "0091..." is not read as trunk "0" + 13 digits.
_IN_PREFIXES = ("0091", "091", "91", "0")


def india_nsn(d: str) -> str | None:
    """The ten-digit Indian national number inside digit-string `d`, or None.

    Accepts it bare, with the country code (91), with the trunk prefix (0),
    or with both -- between them those cover every way the reported resumes
    wrote it. Anything that is not exactly ten digits after a recognised
    prefix is not an Indian number and gets None, which is what keeps
    12-digit Aadhaar/UAN numbers and 11-16 digit account numbers out: none of
    them reduce to ten.
    """
    for p in _IN_PREFIXES:
        if len(d) == NSN_LEN + len(p) and d.startswith(p):
            return d[len(p):]
    return d if len(d) == NSN_LEN else None


def is_indian_mobile(d: str) -> bool:
    """Does digit-string `d` denote an Indian mobile number?

    The whole second pass rests on this predicate, so it is deliberately the
    narrowest true statement available: ten national digits, the first of
    them in the mobile series.
    """
    nsn = india_nsn(d)
    return nsn is not None and nsn[0] in MOBILE_FIRST_DIGITS


#: Layouts that are a dialable number anywhere in the world and that nothing
#: else on a resume is written as. Note what is *not* here, unlike
#: _PHONE_SHAPES: a bare \d{10}, and a bare \d{5} \d{5}. In this pass a run
#: carrying neither a country code nor a label is accepted only when the
#: numbering plan vouches for it.
_RESIDUAL_SHAPES = tuple(re.compile(p) for p in (
    r"\(\d{3}\)[ .\-]?\d{3}[ .\-]?\d{4}",     # (415) 555-0132
    r"\d{3}[ .\-]\d{3}[ .\-]\d{4}",           # 555-123-4567
    r"0\d{2}[ .\-]\d{4}[ .\-]\d{4}",          # 022-2345-6789
    r"0\d{1,4}[ .\-]\d{6,8}",                 # 011-23456789, 0755-123456
))

#: dd-mm-yyyy and friends. Slash-separated dates never survive harvesting (a
#: "/" is not phone punctuation, so it splits the run), but dashed ones do.
_DATE_RUN_RE = re.compile(
    r"^\d{1,2}[ ]?[.\-" + EN_DASH + EM_DASH + r"][ ]?"
    r"\d{1,2}[ ]?[.\-" + EN_DASH + EM_DASH + r"][ ]?\d{2,4}$"
)

#: A generous run of phone punctuation bounded by digits. It only decides
#: where to look; every window inside it is then validated. "/" and "," are
#: excluded on purpose -- they are what separates 06/2016 from 05/2019 and
#: what groups 2,500,000, and excluding them splits both into fragments too
#: short to be a number at all.
_RESIDUAL_RUN_RE = re.compile(
    r"[(+]{0,2}\d[\d \t()+.\-" + EN_DASH + EM_DASH + r"]*\d"
)


def _residual_phone_ok(cand: str, start: int, end: int, text: str) -> bool:
    """Is `text[start:end]` (which is `cand`) a phone number?

    The reject layer runs first and is inherited wholesale from the first
    pass: date ranges, uniform 4-digit id groups, decimals and id labels are
    exactly as unwelcome here. Only then does the numbering plan get a vote.
    """
    d = digits(cand)
    if not (MIN_PHONE_DIGITS <= len(d) <= MAX_PHONE_DIGITS):
        return False

    groups = _DIGITS_RE.findall(cand)
    line_start = text.rfind("\n", 0, start) + 1
    pre = text[line_start:start]

    # --- reject layer -----------------------------------------------------
    if sum(1 for g in groups if _year_like(g)) >= 2:
        return False                       # "2019 - 2023"
    if len(groups) >= 3 and all(len(g) == 4 for g in groups):
        return False                       # "4821-9930-1177", Aadhaar
    if "." in cand and not all(len(g) in (3, 4) for g in groups):
        return False                       # "8.94/10.0", "2.7.1", "802.11ac"
    if _DATE_RUN_RE.match(cand.strip()):
        return False
    if (len(groups) == 1 and len(d) == NSN_LEN and d.endswith("000000")
            and not _PHONE_LABEL_RE.search(pre)):
        # The one shape that is ten digits, starts 6-9, and is not a phone: a
        # round magnitude written without separators. "9500000000 in annual
        # revenue" is 9.5 billion; a subscriber number with six trailing
        # zeros is not something the series ever allocates.
        return False

    if text[end:end + 1].isalpha():
        return False                       # glued suffix: "9876543 B2"

    if _ID_LABEL_RE.search(pre):
        return False                       # "Employee No 9876543210"
    if start > line_start and text[start - 1].isalpha() \
            and not _PHONE_LABEL_RE.search(pre):
        # Glued to a word. "Mobile9876543210" is a number whose space the PDF
        # lost; "HDFC0001234" is an IFSC code. The label is what separates them.
        return False

    # --- positive evidence ------------------------------------------------
    if is_indian_mobile(d):
        return True
    lead = cand.lstrip()
    if lead.startswith(("+", "(+")) or d.startswith("00"):
        return len(d) >= 8                 # written for international dialling
    if _PHONE_LABEL_RE.search(pre):
        return True                        # "Alt. Mobile - 22 2345 6789"
    nsn = india_nsn(d)
    if nsn is not None and len(d) > NSN_LEN and nsn[0] != "0" and len(groups) >= 2:
        return True                        # "91 22 2345 6789", "0120-2345678"
    return any(r.fullmatch(cand.strip()) for r in _RESIDUAL_SHAPES)


def scan_phones(text: str) -> list[tuple[int, int]]:
    """Spans of `text` holding a phone number, left to right, non-overlapping.

    One harvested run can hold more than one number -- "9876543210 9123456789"
    is 20 digits and therefore no number at all -- so each run is walked as
    digit groups and the LONGEST valid window starting at each group wins.
    Longest-first is what makes that safe: for "+91 98765 43210" the 7-digit
    prefix "+91 98765" is also internationally plausible, and taking it would
    redact half the number and leave the other half on the page.
    """
    out: list[tuple[int, int]] = []
    for run_m in _RESIDUAL_RUN_RE.finditer(text):
        run, base = run_m.group(0), run_m.start()
        # An id label applies to the whole run, not only to the digits
        # touching it: in "Credential ID 4821 9876543210" the second group
        # reads as a perfect mobile number once you stop looking at the label,
        # and it is not one. Testing per-window would clear the run as a whole
        # and then accept its tail.
        line_start = text.rfind("\n", 0, base) + 1
        if _ID_LABEL_RE.search(text[line_start:base]):
            continue
        groups = [(m.start(), m.end()) for m in _DIGITS_RE.finditer(run)]
        i = 0
        while i < len(groups):
            taken = None
            for j in range(len(groups) - 1, i - 1, -1):
                # From the first group the window starts at the run start, so
                # a leading "+" or "(" belongs to it -- without the "(",
                # "(415) 555-0132" presents as "415) 555-0132" and matches no
                # shape at all.
                s = 0 if i == 0 else groups[i][0]
                e = groups[j][1]
                if len(digits(run[s:e])) > MAX_PHONE_DIGITS:
                    continue
                if _residual_phone_ok(run[s:e], base + s, base + e, text):
                    taken = (base + s, base + e, j)
                    break
            if taken is None:
                i += 1
            else:
                out.append((taken[0], taken[1]))
                i = taken[2] + 1
    return out


# --- residual email -------------------------------------------------------
# EMAIL_RE above is the right shape test for clean text. It is not enough
# here, because the second pass reads text reconstructed from the PDF's own
# word boxes, and a PDF is free to break one address into several of them:
# kerning around "@" and "." is the usual cause, and a resume whose address
# extracted as "rahul.sharma@ gmail.com" is a resume whose email
# page.search_for() could never find -- which is one of the two ways an
# address survived masking in production.
#
# So harvesting tolerates whitespace, and the whitespace is paid for by a
# stricter TLD rule: an address that had to be stitched back together must
# end in a TLD we actually recognise. Without that, "...@acme. Then we
# shipped" reads as an address in the TLD "Then".

#: Mail providers seen on Indian candidate resumes. Not a filter -- a company
#: or university address is just as much PII -- but a positive signal used
#: when an address had to be reassembled from a badly-broken extraction.
FREEMAIL_DOMAINS = frozenset("""
gmail.com googlemail.com yahoo.com yahoo.co.in yahoo.in ymail.com rocketmail.com
hotmail.com outlook.com live.com msn.com rediffmail.com rediff.com
icloud.com me.com protonmail.com proton.me zoho.com zohomail.in aol.com
gmx.com mail.com yandex.com inbox.com fastmail.com tutanota.com
""".split())

#: TLDs accepted for an address that contains whitespace. Covers the generic
#: and Indian second-levels a candidate's address actually ends in.
_KNOWN_TLDS = frozenset("""
com net org edu gov mil int in co io ai me us uk ca au nz de fr nl es it se ch
jp cn sg ae sa qa om kw bh my ph id th vn hk tw kr ru br za ng ke
info biz name pro mobi xyz online site tech dev app live cloud email
""".split())

#: What an "address" ending in one of these really is: a file name
#: ("logo@2x.png") or a pinned package ("bootstrap@5.min.css"). A version
#: pin like "react@18.2.0" is already rejected -- its TLD is not alphabetic.
_NON_EMAIL_TLDS = frozenset("""
png jpg jpeg gif svg webp bmp ico pdf doc docx xls xlsx ppt pptx zip rar tar gz
exe dll html htm js jsx css scss json xml csv txt yml yaml
""".split())

#: "(at)" / "[dot]" obfuscation. Cheap to support and it costs no precision,
#: because the surrounding pattern still has to produce a real TLD.
_AT_TOKEN_RE = re.compile(r"[ \t]*(?:@|[(\[]\s*at\s*[)\]])[ \t]*", re.I)
_DOT_TOKEN_RE = re.compile(r"[ \t]*(?:\.|[(\[]\s*dot\s*[)\]])[ \t]*", re.I)

#: The local part, allowing the whitespace a PDF injects around a dot but
#: NOT plain spaces -- "Contact me at rahul . sharma@x.com" must yield
#: "rahul . sharma@x.com" and not swallow "Contact me at". Each alternative
#: requires a literal space, so it cannot overlap the base character class,
#: which is what stops this nesting from backtracking catastrophically.
_LOCAL = (r"[A-Za-z0-9._%+\-]+"
          r"(?:[ \t]+\.[ \t]*[A-Za-z0-9._%+\-]+"
          r"|[ \t]*\.[ \t]+[A-Za-z0-9._%+\-]+){0,4}")

_SCAN_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"(" + _LOCAL + r")"
    r"[ \t]*(?:@|[(\[]\s*at\s*[)\]])[ \t]*"
    r"([A-Za-z0-9\-]+(?:[ \t]*(?:\.|[(\[]\s*dot\s*[)\]])[ \t]*[A-Za-z0-9\-]+){1,5})"
    r"(?![A-Za-z0-9\-])",
    re.I,
)


def normalize_email(raw: str) -> str | None:
    """`raw` as a canonical address, or None if it is not one.

    Undoes both things the harvest tolerates -- the injected whitespace and
    the (at)/(dot) obfuscation -- and then applies the checks that decide
    whether what is left is an address at all.
    """
    s = _DOT_TOKEN_RE.sub(".", _AT_TOKEN_RE.sub("@", raw))
    s = re.sub(r"[ \t]+", "", s)
    if s.count("@") != 1:
        return None
    local, domain = s.split("@")
    labels = domain.split(".")
    if not local or len(labels) < 2 or not all(labels):
        return None
    tld = labels[-1].lower()
    if not tld.isalpha() or not (2 <= len(tld) <= 24):
        return None
    if tld in _NON_EMAIL_TLDS:
        return None                        # "logo@2x.png"
    if any(c.isspace() for c in raw) and tld not in _KNOWN_TLDS:
        return None                        # "...@acme. Then we shipped"
    return s


def scan_emails(text: str) -> list[tuple[int, int]]:
    """Spans of `text` holding an email address.

    Both shapes are harvested -- the strict one and the whitespace-tolerant
    one -- and where they disagree about the same address, the candidate that
    starts EARLIER wins, and at the same start the SHORTER one wins. That
    single ordering settles both of the ways they can disagree:

        "rahul . sharma@x.com"   strict finds "sharma@x.com" (starts later);
                                 the loose match starts earlier and takes it,
                                 recovering the local part the PDF broke up
        "rahul@x.com. Net sales" strict finds "rahul@x.com" (same start,
                                 shorter); the loose match has reached past a
                                 complete address into the next sentence, and
                                 loses

    Redacting the first as "sharma@x.com" would leave "rahul ." on the page;
    redacting the second as the loose match would blank a real word.
    """
    seen: set[tuple[int, int]] = set()
    for regex in (EMAIL_RE, _SCAN_EMAIL_RE):
        for m in regex.finditer(text):
            if normalize_email(m.group(0)):
                seen.add((m.start(), m.end()))

    out: list[tuple[int, int]] = []
    for start, end in sorted(seen, key=lambda se: (se[0], se[1] - se[0])):
        if not any(s < end and start < e for s, e in out):
            out.append((start, end))
    return out


def scan_residual(text: str) -> list[tuple[int, int, str]]:
    """Every (start, end, kind) in `text` the second pass wants redacted.

    Emails win any overlap: "9876543210@example.com" is one address, not an
    address next to a mobile number, and redacting it as two regions would
    report two hits for one value.
    """
    spans = [(s, e, EMAIL) for s, e in scan_emails(text)]
    for s, e in scan_phones(text):
        if not any(a < e and s < b for a, b, _ in spans):
            spans.append((s, e, PHONE))
    return sorted(spans)
