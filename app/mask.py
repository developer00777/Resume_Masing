"""Resume masking core — TRUE-redact PII text, plus centered watermark
IMAGE overlay.

Only three things are ever redacted: candidate name, phone number(s), email
address(es). Which strings those are is decided upstream (server.detect_pii +
the Salesforce Contact record); this module's job is to find each one on the
page *without* catching anything else, which needs a per-kind matching
strategy rather than one blind substring search:

    email  exact substring   — the shape is unique enough to trust as-is
    phone  digit-equivalence — formatting differs between Salesforce and the
                               resume, so match on digits, anchored at the end
    name   whole-word literal — a bare search_for() hit can be a fragment
                               inside a longer word ("Ana" inside "Analysis")

Removing the value is only half the job. What the reader sees is the *row* it
sat in, and a row that still says

    Phone number:                    (Mobile)
    Email ID:

over white space has told them exactly what was taken out, which is the defect
clients keep reporting. So every hit is grown outwards along its row over the
label that introduces it and the annotation that trails it — see
_absorb_labels, which is the single place that decision is made, for both
passes and all three kinds.

Redaction is a true redaction — `apply_redactions()` deletes the glyphs, so
the text is gone from the PDF, not merely covered. The fill is white
(REDACT_FILL) so the masked copy reads as clean whitespace rather than a page
of black bars.

Watermark is a client-specific PNG image (logo/brand) stored in Salesforce,
fetched at mask-time, stamped center-aligned on every page of the resume PDF.
"""
from __future__ import annotations

import re

import fitz

from . import pii, residual

#: Redaction fill. White, not black — the redacted region should read as blank
#: space on the page rather than a censor bar.
REDACT_FILL = (1.0, 1.0, 1.0)

#: A search_for() hit is treated as a fragment (and dropped) if it covers less
#: than this fraction of the width of a word it overlaps. Set high enough that
#: "Li" inside "Lin" (0.67) is rejected, low enough that a name followed by
#: punctuation glued into the same word — "Sharma," (~0.93) — is kept.
_WORD_COVERAGE = 0.85


def _digits(s: str) -> str:
    return pii.digits(s)


# --- page layout ----------------------------------------------------------

class _Layout:
    """The word layout of one page, indexed the three ways masking needs it.

    Built once per page and passed down, rather than each helper re-deriving
    it: matching a dozen mask strings used to regroup every word into lines a
    dozen times over, and each label absorption then scanned the whole word
    list again, five times per hit. Every lookup below is against a
    precomputed index instead.

      * `lines`  — the PDF's own text lines, in reading order. What a phone
                   candidate may span, and what a name is matched within: a
                   run must never form across two stacked lines of a
                   two-column layout.
      * `rows`   — words grouped by where they sit VERTICALLY, which is not
                   the same thing. A label and its value routinely land in
                   different text lines while printing as one row ("Name" and
                   ":" on line 0, the value on line 1, on JA-26631), so label
                   absorption has to work off geometry, not off line numbers.
    """

    __slots__ = ("words", "lines", "rows", "_row_bounds")

    def __init__(self, words: list):
        self.words = words

        lines: dict[tuple[int, int], list] = {}
        for w in words:
            lines.setdefault((w[5], w[6]), []).append(w)
        for group in lines.values():
            group.sort(key=lambda w: w[0])
        self.lines = lines

        # Rows, by vertical overlap. Sorted by top edge, so a word only ever
        # needs to be offered to the few rows opened most recently.
        rows: list[list] = []
        bounds: list[list[float]] = []
        for w in sorted(words, key=lambda w: (w[1], w[0])):
            cy = (w[1] + w[3]) / 2
            for i in range(len(rows) - 1, max(-1, len(rows) - 4), -1):
                y0, y1 = bounds[i]
                if y0 < cy < y1 or w[1] < (y0 + y1) / 2 < w[3]:
                    rows[i].append(w)
                    bounds[i] = [min(y0, w[1]), max(y1, w[3])]
                    break
            else:
                rows.append([w])
                bounds.append([w[1], w[3]])
        for row in rows:
            row.sort(key=lambda w: w[0])
        self.rows = rows
        # Kept from the pass above rather than re-derived: row_for() runs once
        # per hit, and measuring every row's extent each time walks the whole
        # page to answer a question already answered here.
        self._row_bounds = [tuple(b) for b in bounds]

    def row_for(self, rect: fitz.Rect) -> list:
        """The row `rect` sits in — the one its vertical span overlaps most."""
        best, best_overlap = [], 0.0
        for row, (y0, y1) in zip(self.rows, self._row_bounds):
            overlap = min(rect.y1, y1) - max(rect.y0, y0)
            if overlap > best_overlap:
                best, best_overlap = row, overlap
        return best

    def near(self, rect: fitz.Rect) -> list:
        """Words that could possibly intersect `rect`, by vertical span.

        Callers that only care about intersections (clipping, coverage) get a
        row's worth of words to test instead of the page's.
        """
        return [w for w in self.words if w[3] > rect.y0 and w[1] < rect.y1]

    def block_for(self, rect: fitz.Rect) -> int | None:
        """The text block `rect` mostly sits in, or None if it touches none."""
        best, best_area = None, 0.0
        for w in self.near(rect):
            area = (fitz.Rect(w[:4]) & rect).get_area()
            if area > best_area:
                best, best_area = w[5], area
        return best


# --- phone ----------------------------------------------------------------

def _phone_rects(target: str, layout: _Layout) -> list[fitz.Rect]:
    """Locate a phone number by its digits, not its literal formatting.

    page.search_for() is an exact substring match, so it misses a phone number
    whenever mask_strings' formatting differs from what the PDF actually renders
    (e.g. the on-prem parser stores it normalized as E.164 "+919876543210" while
    the resume shows "+91 98765 43210" — the original client complaint about
    phone digits leaking through). This walks each line's words and matches on
    concatenated digits instead, tolerant of a missing/extra country code or
    trunk-prefix digit.

    Equivalence is decided by pii.phone_digits_equivalent(), which anchors on
    the END of the number. The looser "substring either way, +/-3 digits" rule
    this used to apply also matched digit runs that merely contained a piece of
    the phone number — an employee id or an order number sitting inside a
    longer run — which is exactly the over-masking this module must not do.
    """
    target_digits = _digits(target)
    if len(target_digits) < pii.MIN_PHONE_DIGITS:
        return []

    out: list[fitz.Rect] = []
    for key, line in layout.lines.items():
        n = len(line)
        for i in range(n):
            if not _digits(line[i][4]):
                continue  # only start a window on a word that itself has digits —
                          # otherwise a match can grow backwards into a label like "Phone:"
            if pii.is_id_context(" ".join(w[4] for w in line[:i])):
                continue  # "Order Reference 4319876543210" ends in the candidate's
                          # own ten digits; the label is the only thing that says
                          # it is an order reference and not a +43 number
            run = ""
            for j in range(i, min(i + 6, n)):
                run += _digits(line[j][4])
                if len(run) > len(target_digits) + 4:
                    break
                if pii.phone_digits_equivalent(run, target_digits):
                    out.append(_clip_to_line(fitz.Rect(
                        min(line[k][0] for k in range(i, j + 1)),
                        min(line[k][1] for k in range(i, j + 1)),
                        max(line[k][2] for k in range(i, j + 1)),
                        max(line[k][3] for k in range(i, j + 1)),
                    ), key, layout))
                    break
    return _dedupe_rects(out)


# --- geometry -------------------------------------------------------------

def _clip_literal(rect: fitz.Rect, layout: _Layout) -> fitz.Rect:
    """_clip_to_line for a search_for() hit, whose line has to be inferred
    from whichever line contributes most of the words the hit covers."""
    best, best_area = None, 0.0
    for w in layout.near(rect):
        area = (fitz.Rect(w[:4]) & rect).get_area()
        if area > best_area:
            best, best_area = (w[5], w[6]), area
    return rect if best is None else _clip_to_line(rect, best, layout)


def _clip_to_line(rect: fitz.Rect, line_key: tuple[int, int],
                  layout: _Layout) -> fitz.Rect:
    """Shrink `rect` vertically so it cannot reach text on neighbouring lines.

    A word box for a large heading font is far taller than its glyphs -- a
    candidate name on one live resume measured 37pt tall for two words -- and
    apply_redactions() deletes every character whose box merely INTERSECTS the
    annotation. So the tall heading rect silently wiped the tagline underneath
    it, leaving "Elec" and "nce" stranded either side of a white gap, which is
    the "white box covering info for no reason" that got reported.

    Trimming back is safe: the name's own characters still intersect the
    smaller rect, so they are still deleted. If the two genuinely overlap and
    no separating rect exists, the original is kept -- removing the PII wins
    over preserving the line.
    """
    near = layout.near(rect)
    own = [w for w in near
           if (w[5], w[6]) == line_key and not (fitz.Rect(w[:4]) & rect).is_empty]
    if not own:
        return rect

    mid = (rect.y0 + rect.y1) / 2
    top, bottom = rect.y0, rect.y1
    for w in near:
        if (w[5], w[6]) == line_key:
            continue
        wr = fitz.Rect(w[:4])
        overlap = wr & rect
        if wr.is_empty or overlap.is_empty or overlap.width <= 0:
            continue
        if (wr.y0 + wr.y1) / 2 >= mid:
            bottom = min(bottom, wr.y0)
        else:
            top = max(top, wr.y1)

    clipped = fitz.Rect(rect.x0, top, rect.x1, bottom)
    if clipped.height < 1:
        return rect
    # The clipped rect must still touch every word it was meant to remove.
    if any((fitz.Rect(w[:4]) & clipped).is_empty for w in own):
        return rect
    return clipped


#: Widest horizontal gap that a stranded separator may span, in points.
#: Comfortably wider than a slash or comma plus its spaces, narrower than the
#: gutter between two columns.
_BRIDGE_MAX_GAP = 24.0


def _bridge_separators(rects: list[fitz.Rect], layout: _Layout) -> list[fitz.Rect]:
    """Absorb a separator left stranded between two redactions.

    A Contact field holding "9876543210 / 9123456789" redacts both numbers and
    leaves the slash floating in the white gap between them; the same happens
    to the comma between two email addresses. Reported as "some slash and , is
    not covered".

    Two rects are merged only when they sit on the same line, are close
    together, and every word strictly between them is punctuation -- so this
    can never join two redactions across real content.
    """
    if len(rects) < 2:
        return rects

    merged = sorted(rects, key=lambda r: (round(r.y0, 1), r.x0))
    changed = True
    while changed:
        changed = False
        for i in range(len(merged) - 1):
            a, b = merged[i], merged[i + 1]
            if a.y1 <= b.y0 or b.y1 <= a.y0:
                continue                       # not on the same line
            gap = b.x0 - a.x1
            if gap < 0 or gap > _BRIDGE_MAX_GAP:
                continue
            between = [w for w in layout.words
                       if w[0] >= a.x1 - 0.5 and w[2] <= b.x0 + 0.5
                       and w[3] > min(a.y0, b.y0) and w[1] < max(a.y1, b.y1)]
            if any(any(c.isalnum() for c in w[4]) for w in between):
                continue                       # real content in the gap
            merged[i:i + 2] = [fitz.Rect(a.x0, min(a.y0, b.y0),
                                         b.x1, max(a.y1, b.y1))]
            changed = True
            break
    return merged


def _dedupe_rects(rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """Drop rects fully contained in another one.

    The digit-window scan starts a window on every digit-bearing word, so a
    number written "+91 98765 43210" is found three times over (from "+91",
    from "98765", ...) as nested rects. Redacting all of them is harmless but
    inflates the redacted_regions count we report back to Salesforce, which is
    the only signal a caller has for "did this actually mask anything".
    """
    kept: list[fitz.Rect] = []
    for r in sorted(rects, key=lambda r: -r.get_area()):
        if not any(r in k for k in kept):
            kept.append(r)
    return kept


def _covers_whole_words(rect: fitz.Rect, layout: _Layout) -> bool:
    """Does `rect` cover whole words, rather than clipping into one?

    search_for() has no word-boundary option, so a short name matches inside
    longer words. Comparing the hit's width against the width of each word it
    touches tells the two cases apart: a whole-word hit spans the word, a
    fragment hit covers only part of it.
    """
    for w in layout.near(rect):
        wr = fitz.Rect(w[0], w[1], w[2], w[3])
        if wr.is_empty or wr.width <= 0:
            continue
        inter = wr & rect
        if inter.is_empty or inter.width <= 0:
            continue
        # Ignore words that merely brush the rect from the line above/below.
        if inter.height < min(wr.height, rect.height) * 0.5:
            continue
        if inter.width < wr.width * _WORD_COVERAGE:
            return False
    return True


# --- name -----------------------------------------------------------------

#: Letters only — how a name token is compared, so "Sharma," and "SHARMA"
#: both reduce to "sharma". Applied per letter-run rather than to the word as
#: a whole, which is what lets a name be recognised through the label glued to
#: the front of it: "Name-Anup" is one word box on JA-26708, and matching the
#: word whole ("nameanup") left the candidate's first name on the page.
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+")

#: How many unmatched words may sit between two matched name tokens. One
#: covers the common case of a middle name printed on the resume but absent
#: from the Contact record. More than that and we would start joining up
#: unrelated words that happen to share a surname.
_NAME_MAX_GAP = 1

#: A match must cover at least this many letters in total. Stops two short
#: coincidental tokens from being read as a name.
_NAME_MIN_CHARS = 6

#: Shortest lone name token we will redact on its own. Below this a token is
#: too easily an acronym or an ordinary short word to act on without the
#: corroboration of a neighbouring token.
_NAME_LONE_TOKEN_MIN = 4


def _name_sequence(name: str) -> list[tuple[str, bool]]:
    """The Contact name as ordered (part, is_initial) entries.

    Initials used to be dropped outright, because a single letter matches far
    too much on its own. Dropping them also threw away the only evidence that
    the name continues: the Contact holds "Karthik V", the resume heading says
    "Karthik Velayuthan", and with the initial gone there was one usable token
    left, no two-token match, and the candidate's surname stayed in 24pt at
    the top of the masked copy (JA-26753).

    Kept as a weak entry instead — see _name_rects, where an initial only ever
    matches a capitalised word sitting immediately after a token that already
    matched.
    """
    return [(t.casefold(), len(t) < 3) for t in _NAME_TOKEN_RE.findall(str(name))]


def _name_token_list(name: str) -> list[str]:
    """The full-length tokens of `name`, which are what may match on their own."""
    return [t for t, initial in _name_sequence(name) if not initial]


def _word_groups(text: str) -> list[str]:
    """The letter runs in one word box ("Name-Anup" -> ["Name", "Anup"])."""
    return _NAME_TOKEN_RE.findall(text)


def _initial_matches(letter: str, text: str) -> bool:
    """Could word `text` be the name part the Contact abbreviated to `letter`?

    Capitalisation is the guard that makes this safe: a name is written
    "Velayuthan" or "VELAYUTHAN", never "velayuthan", so ordinary prose cannot
    satisfy it even when it starts with the right letter.
    """
    groups = _word_groups(text)
    return bool(groups) and groups[0][:1].casefold() == letter \
        and (groups[0].istitle() or groups[0].isupper())


def _lone_token_rects(tokens: list[str], layout: _Layout) -> list[fitz.Rect]:
    """Redact a single name token standing on its own.

    A surname alone in a page footer, or a first name above a signature, is
    still the candidate's name -- 22 of these survived across 40 live resumes
    once the full-name match was working, so they are the bulk of what is left
    leaking.

    Guarded two ways so ordinary prose is untouched: the token must be at
    least _NAME_LONE_TOKEN_MIN characters, and it must be capitalised the way
    a name is. "Kumar" and "KUMAR" match; the "will" in "I will manage
    delivery" does not, which is what makes this safe for a candidate whose
    name really is Will.
    """
    wanted = {t for t in tokens if len(t) >= _NAME_LONE_TOKEN_MIN}
    # The tokens run together as one word, which is how a resume heading or a
    # file-derived title often writes it: "ANILKUMAR" for Contact "Anil
    # Kumar". Matched only on full equality, never as a substring, so it
    # cannot behave like the "Ana" inside "Analysis" case.
    joined = {"".join(tokens[i:j])
              for i in range(len(tokens))
              for j in range(i + 2, len(tokens) + 1)}
    if not wanted and not joined:
        return []
    out: list[fitz.Rect] = []
    for w in layout.words:
        text = w[4].strip(" .,;:()[]-|/")
        if not text or "@" in text:
            continue                      # emails are matched as emails
        groups = _word_groups(text)
        if "".join(groups).casefold() in joined:
            out.append(_clip_to_line(fitz.Rect(w[:4]), (w[5], w[6]), layout))
            continue
        # Per letter-run, so a label glued to the name ("Name-Anup") is
        # recognised — and taken with it, since the whole word box goes.
        if any(g.casefold() in wanted and (g.istitle() or g.isupper())
               for g in groups):
            out.append(_clip_to_line(fitz.Rect(w[:4]), (w[5], w[6]), layout))
    return out


#: A word that is a link or a handle rather than prose. A name inside one of
#: these is still the candidate's name: JA-26753 was masked down to a blank
#: contact block that still carried
#: "https://www.linkedin.com/in/karthikvelayuthan/", which names the candidate
#: as plainly as the heading did.
_URLISH_RE = re.compile(
    r"https?://|www\.|\b[a-z0-9\-]+\.(?:com|in|org|net|io|me|co|dev|info|us|uk)\b",
    re.I,
)


def _url_name_rects(tokens: list[str], layout: _Layout) -> list[fitz.Rect]:
    """Redact a URL or handle that spells out the candidate's name.

    Matched as a substring of the link's letters, which is the only way to
    find it: a profile slug runs the name together and drops the separators
    ("karthikvelayuthan"). Confined to links, so the substring rule cannot
    behave like the "Ana" inside "Analysis" case — that only happens in prose,
    and prose is not a URL.
    """
    wanted = [t for t in tokens if len(t) >= _NAME_LONE_TOKEN_MIN]
    if not wanted:
        return []
    out: list[fitz.Rect] = []
    for w in layout.words:
        if not _URLISH_RE.search(w[4]):
            continue
        letters = "".join(_word_groups(w[4])).casefold()
        if any(t in letters for t in wanted):
            out.append(_clip_to_line(fitz.Rect(w[:4]), (w[5], w[6]), layout))
    return out


def _name_rects(name: str, layout: _Layout) -> list[fitz.Rect]:
    """Locate a candidate's name even when the resume spells it differently.

    page.search_for() needs the whole string present verbatim, and on real
    records it very often is not -- the Contact holds a middle name the resume
    omits, or the resume prints one the Contact lacks:

        Contact "Sitendra Kumar Chakra"   resume "SITENDRA CHAKRA"
        Contact "Samar Wadyalkar"         resume "SAMAR SHIVAJI WADYALKAR"
        Contact "Karthik V"               resume "Karthik Velayuthan"

    Confirmed on live data, where roughly a third of sampled records had a
    Contact name that appears nowhere verbatim in the resume -- so the name was
    not redacted at all, and sat in the page heading of the masked copy while
    phone and email were blacked out.

    Matches the entries as an ordered subsequence within a single line, letting
    either side carry extra words, and redacts the whole span (a middle name
    on the resume is part of the name, so covering it is correct). Requires at
    least two entries to match: one alone would mask every occurrence of an
    ordinary word for a candidate named Will, Rose or Mark.
    """
    seq = _name_sequence(name)
    strong = [t for t, initial in seq if not initial]
    if not strong:
        return []

    out: list[fitz.Rect] = _lone_token_rects(strong, layout) \
        + _url_name_rects(strong, layout)
    if len(seq) < 2:
        return out

    for line in layout.lines.values():
        groups = [[g.casefold() for g in _word_groups(w[4])] for w in line]
        n = len(line)
        for start in range(n):
            if not any(g in strong for g in groups[start]):
                continue
            nxt = matched = chars = gaps = 0
            last = start
            for j in range(start, n):
                if not groups[j]:
                    continue                       # punctuation-only word
                hit = None
                for k in range(nxt, len(seq)):
                    token, initial = seq[k]
                    if not initial and token in groups[j]:
                        hit = k
                        break
                    # An initial is evidence only where it stands: directly
                    # after a part that already matched, never on its own and
                    # never across a gap.
                    if initial and matched and not gaps \
                            and _initial_matches(token, line[j][4]):
                        hit = k
                        break
                if hit is not None:
                    matched += 1
                    chars += sum(len(g) for g in groups[j])
                    nxt = hit + 1
                    last = j
                    gaps = 0
                    if nxt >= len(seq):
                        break
                elif matched and gaps < _NAME_MAX_GAP:
                    gaps += 1
                elif matched:
                    break
            if matched >= 2 and chars >= _NAME_MIN_CHARS:
                out.append(_clip_to_line(fitz.Rect(
                    min(line[k][0] for k in range(start, last + 1)),
                    min(line[k][1] for k in range(start, last + 1)),
                    max(line[k][2] for k in range(start, last + 1)),
                    max(line[k][3] for k in range(start, last + 1)),
                ), (line[start][5], line[start][6]), layout))
    return out


def _matches_case(rect: fitz.Rect, layout: _Layout, needle: str) -> bool:
    """Is the text under `rect` written the same way as `needle`?

    Accepts the needle as-is or fully upper-cased, which is how a name appears
    in a resume heading.
    """
    covered = []
    for w in layout.near(rect):
        wr = fitz.Rect(w[0], w[1], w[2], w[3])
        inter = wr & rect
        if inter.is_empty or inter.width <= 0:
            continue
        if inter.height < min(wr.height, rect.height) * 0.5:
            continue
        covered.append(w[4])
    text = " ".join(covered).strip(" .,;:()[]-")
    return text in (needle, needle.upper())


# --- labels ---------------------------------------------------------------
# A redaction that removes only the value leaves the row saying what it was.
# Clients report that as a defect in its own right ("the emailid tag is also
# not removed", "it still has the mobile number tag"), and they are right to:
# a blank space behind "Alternate Mobile No. :" is not anonymised, it is
# annotated.
#
# So a hit grows outwards along its row for as long as what it meets is only a
# label. That is one rule in one place, applied to both sides, because the
# label is not reliably on the left:
#
#     Phone number: (+91) 98765 43210 (Mobile)      <- trails the value
#     Name  :        Rahul Sharma                   <- leads it, tab-aligned
#     Email id:-rahul@example.com                   <- glued into the same word
#     + 91-9876543210                               <- a sign left by itself

#: The separators a label is written with, and which on their own are all that
#: is left of one.
_LABEL_SEPS = r"\s.:\-–—#|/+()\[\]"

#: The words a contact label is built from. A real one is several of them
#: ("Alternate E-Mail ID :", "Personal Mob No."), and the walk takes them one
#: at a time, so every kind has to be listed or it stops early and strands
#: what it did not reach.
_LABEL_WORDS = (
    # the contact channel
    r"e[-\s]?mail|email|mail|e|phone|mobile|mob|cell(?:ular)?|"
    r"tel(?:ephone)?|contact|whats?[\s-]?app|ph|landline|fax|skype|"
    r"linked[\s-]?in|"
    # the qualifier in front of it
    r"alt(?:\.|ernat(?:e|ive))?|second(?:ary)?|primary|personal|official|"
    r"office|work|home|res(?:idence|idential)?|permanent|current|"
    r"name|candidate|applicant|"
    # the trailing noun
    r"id|ids|i\.?d\.?|address|addr|detail(?:s)?|info|no|nos|num(?:ber)?"
)

_CONTACT_LABEL_RE = re.compile(
    rf"^[{_LABEL_SEPS}]*(?:{_LABEL_WORDS})[{_LABEL_SEPS}]*$"
    # A separator stranded on its own, either between two label words
    # ("E - Mail ID") or left behind by the value itself: a "+" typed as its
    # own word survived redaction of "+ 91-98765 43210" on JA-26631 and sat
    # alone on the masked page.
    rf"|^[{_LABEL_SEPS}]+$"
    # The bracketed annotation a contact block puts AFTER the number to say
    # which line it is: "(Mobile)", "(R)", "(O)". JA-26753 shipped with
    # "(Mobile)" alone on the row, which is the reported "mobile number tag".
    rf"|^\s*[(\[]\s*(?:{_LABEL_WORDS}|[mrowhp])\s*[)\]][{_LABEL_SEPS}]*$",
    re.I,
)

#: Words that hold a label together without being one ("NAME OF THE
#: CANDIDATE"). Absorbed only in the middle of a run -- a label never starts
#: or ends on one, and trailing ones are trimmed back off before committing.
_LABEL_CONNECTOR_RE = re.compile(
    rf"^[{_LABEL_SEPS}]*(?:of|the|for|and|to|my|our|&)[{_LABEL_SEPS}]*$", re.I)

#: A label printed inside the SAME word box as the value, which is how
#: "Email id:-someone@example.com" extracts on JA-26708 -- one word, with the
#: label glued to the front. search_for() matches only the address, so the
#: rect starts partway through the word and "id:-" stays on the masked page.
_GLUED_LABEL_RE = re.compile(
    r"^(?:e[-\s]?mail|email|mail|mob(?:ile)?|ph(?:one)?|tel|contact|whats?app|"
    r"name|id|no|alt|res)?"
    r"[\s.:\-–—#|()+]+",
    re.I,
)

#: Widest gap, in points, between a contact label and the value it labels.
#: Generous, because the guards that matter here are structural rather than
#: metric: the label has to be the NEAREST word on that side (so everything
#: between it and the value is whitespace) and it has to sit in the same text
#: block (so a two-column layout cannot donate its left column to the right).
#: The old 12pt assumed a label typed up against its value; on real resumes
#: the contact block is tab-aligned, and both reported labels sat outside it
#: -- "Email" 20.5pt from its address on JA-26708, "Name :" 38.8pt from its
#: value on JA-26631 -- so both stayed on the masked page.
_LABEL_MAX_GAP = 150.0

#: How many words a label may run to. "Alternate E - Mail ID :" is five, and
#: stopping short of the start of a label run is what leaves half of it on the
#: page.
_LABEL_MAX_WORDS = 6


def _absorb_glued_label(rect: fitz.Rect, layout: _Layout) -> fitz.Rect:
    """Extend `rect` left over a label printed inside the value's own word."""
    for w in layout.near(rect):
        if not (w[0] < rect.x0 - 0.5 < w[2] - 0.5):
            continue                      # rect does not start inside this word
        if _GLUED_LABEL_RE.match(w[4]):
            return fitz.Rect(w[0], rect.y0, rect.x1, rect.y1)
    return rect


def _walk_labels(rect: fitz.Rect, row: list, block: int | None,
                 forward: bool) -> float | None:
    """How far `rect` may grow along `row` before it stops meeting labels.

    Returns the x to grow to, or None to grow no further. The walk stops at
    the first word that is not part of a label, and then has to decide whether
    what it absorbed really was one. The tell is what stopped it: a field
    label is bounded by the edge of its line, by another field's value, or by
    a gap -- never by lowercase prose. That single test is what keeps

        In case of any problem, please contact at: help@example.org

    intact (stopped by "please") while still taking the whole of

        Nationality: Indian   Gender: Male   Phone number: (+91) 98765 43210

    (stopped by "Male", which is a value, not prose).
    """
    edge = rect.x1 if forward else rect.x0
    absorbed: list[tuple[list, bool]] = []
    for _ in range(_LABEL_MAX_WORDS):
        nearest = None
        for w in row:
            if block is not None and w[5] != block:
                continue
            if forward and w[0] >= edge - 1.0:
                if nearest is None or w[0] < nearest[0]:
                    nearest = w
            elif not forward and w[2] <= edge + 1.0:
                if nearest is None or w[2] > nearest[2]:
                    nearest = w
        if nearest is None:
            break                          # the edge of the row: nothing to stop us
        gap = nearest[0] - edge if forward else edge - nearest[2]
        if gap > _LABEL_MAX_GAP:
            break                          # too far away to be this value's label
        text = nearest[4].strip()
        if _CONTACT_LABEL_RE.match(text):
            absorbed.append((nearest, False))
        elif absorbed and _LABEL_CONNECTOR_RE.match(text):
            absorbed.append((nearest, True))
        elif text[:1].islower():
            return None                    # running prose, not a label
        else:
            break
        edge = nearest[2] if forward else nearest[0]

    while absorbed and absorbed[-1][1]:
        absorbed.pop()                     # a label never ends on a connector
    if not absorbed:
        return None
    outermost = absorbed[-1][0]
    return outermost[2] if forward else outermost[0]


def _absorb_labels(rect: fitz.Rect, layout: _Layout) -> fitz.Rect:
    """Grow `rect` over the label that introduces the value and the annotation
    that trails it, so the masked row says nothing about what was removed.

    Sideways only. Taking a label's vertical extent as well is how a tall
    label box reached the line underneath, and it buys nothing:
    apply_redactions() deletes every glyph whose box merely INTERSECTS the
    annotation, so covering the label's row is enough to remove it.
    """
    grown = _absorb_glued_label(rect, layout)
    row = layout.row_for(grown)
    if not row:
        return grown
    block = layout.block_for(grown)
    x0 = _walk_labels(grown, row, block, forward=False)
    x1 = _walk_labels(grown, row, block, forward=True)
    if x0 is None and x1 is None:
        return grown
    return fitz.Rect(grown.x0 if x0 is None else x0, grown.y0,
                     grown.x1 if x1 is None else x1, grown.y1)


def _rects_for(page: fitz.Page, s: str, layout: _Layout) -> list[fitz.Rect]:
    """Every region of `page` that should be redacted for the PII string `s`,
    using the matching strategy its kind calls for."""
    kind = pii.classify(s)
    if kind == pii.PHONE:
        found = _phone_rects(s, layout)
    elif kind == pii.EMAIL:
        found = [_clip_literal(r, layout) for r in page.search_for(s)]
    else:
        # Name (and any literal a caller passed explicitly): whole-word only.
        if len(s.strip()) < 3:
            return []  # too short to match safely — would hit half the page
        literal = [_clip_literal(r, layout) for r in page.search_for(s)
                   if _covers_whole_words(r, layout)]
        if len(_name_token_list(s)) < 2:
            # A one-token name has to match case as written. search_for() is
            # case-insensitive, so a candidate actually named Will, Rose or Mark
            # otherwise has every ordinary occurrence of that word redacted out of
            # their own resume ("I will manage delivery" -> "I  manage delivery").
            # A heading still matches, as "Will" or as "WILL".
            literal = [r for r in literal if _matches_case(r, layout, s)]
        # Union, not either/or: a resume can print the name verbatim in one
        # place and with an extra middle name in another, and both have to go.
        found = _dedupe_rects(literal + _name_rects(s, layout))
    # The label goes with the value, whatever kind it was: a "Candidate Name:"
    # left standing over white space is the defect, not the fix.
    return [_absorb_labels(r, layout) for r in found]


def mask_pdf_bytes(pdf_bytes: bytes, mask_strings: list[str],
                   watermark_png: bytes | None = None,
                   watermark_text: str = "",
                   residual_sweep: bool = True) -> tuple[bytes, int]:
    """True-redact PII strings, then overlay watermark.

    Two passes, in this order and for a reason. The first removes the values
    it was handed -- the Contact record's name/phone/email. The second
    (app/residual.py) then reads the page as it now stands and removes the
    phone numbers and addresses still on it: the alternate mobile that only
    ever existed in the resume body, and the address whose PDF word boxes
    search_for() could not match. Running it second rather than merging the
    two means it scores only what genuinely survived, so it cannot re-redact
    a region the first pass already cleared.

    Args:
        pdf_bytes: Raw resume PDF bytes.
        mask_strings: Exact strings to redact (name, phone, email from parser).
        watermark_png: Client watermark image bytes (PNG/JPEG). Centered on every page.
        watermark_text: Fallback text watermark if no image provided.
        residual_sweep: Run the second pass. Off only for measuring what the
            first pass does on its own.

    Returns:
        (masked_pdf_bytes, redacted_region_count)
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    hits = 0
    watermark = prepare_watermark(watermark_png) if watermark_png else None
    # expand() splits a Contact field holding several phone numbers into one
    # entry per number. Left whole, such a value is too long to classify as a
    # phone and gets searched as a single literal that appears in no resume --
    # so the candidate's phone goes unmasked. Done once, not per page.
    wanted = pii.expand([str(s) for s in mask_strings])

    for page in doc:
        # Index the word layout once per page — every mask string is matched
        # against it, and get_text() is the expensive part of this loop.
        layout = _Layout(page.get_text("words"))

        page_rects: list[fitz.Rect] = []
        for s in wanted:
            page_rects.extend(_rects_for(page, s, layout))

        for rect in _bridge_separators(_dedupe_rects(page_rects), layout):
            page.add_redact_annot(rect, fill=REDACT_FILL)
            hits += 1

        page.apply_redactions()

        # --- second pass --------------------------------------------------
        # Re-read the page: the layout above describes the page before
        # redaction, and every rect from here has to be measured against
        # what is actually left. Clipping and label absorption are the same
        # ones the first pass uses, so a residual hit is redacted exactly
        # like a Contact-record hit, label and all.
        if residual_sweep:
            left = _Layout(page.get_text("words"))
            found = [_absorb_labels(_clip_to_line(rect, key, left), left)
                     for rect, key in residual.find_residual_rects(page, left.words)]
            if found:
                for rect in _bridge_separators(_dedupe_rects(found), left):
                    page.add_redact_annot(rect, fill=REDACT_FILL)
                    hits += 1
                page.apply_redactions()
            # The glyphs under a mailto: link are gone by now; the link's own
            # URI still holds the address until this runs.
            residual.scrub_links(page)

        # Watermark only when the client actually has one. No stand-in text:
        # a "CONFIDENTIAL" default was being stamped on every masked resume
        # for clients who had never configured a watermark at all.
        if watermark is not None:
            _watermark_image(page, watermark)
        elif watermark_text:
            _watermark_text(page, watermark_text)

    # Word writes the candidate's name into /Title and /Author from the
    # original filename ("Rahul Sharma CV 2024.docx"), and it survives
    # redaction untouched -- it is not on any page.
    if residual_sweep:
        residual.scrub_metadata(doc)

    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out, hits

#: Watermark width as a fraction of the page width. The old value effectively
#: filled a 50%-by-50% box, which on A4 is a ~300pt block across the middle of
#: the resume.
WATERMARK_WIDTH_RATIO = 0.28

#: How much of the watermark's own opacity survives. A logo has to read as a
#: stamp behind the content, not as a panel over it.
WATERMARK_OPACITY = 0.16

#: A pixel at least this bright in every channel counts as background and is
#: knocked out completely -- but only for a source with no alpha channel of its
#: own. The client logo on the live org is a 554x554 RGB PNG with alpha=0: a
#: solid white square with the logo painted on it. Fading that uniformly would
#: leave a pale grey box over the text; the white has to become transparent.
WATERMARK_WHITE_CUTOFF = 240


def prepare_watermark(image_bytes: bytes) -> fitz.Pixmap | None:
    """Turn an uploaded logo into something safe to stamp over a resume.

    Two corrections, both driven by what clients actually upload:

      * an opaque background is made transparent, so the logo does not arrive
        as a filled rectangle sitting on top of the text;
      * what remains is faded to WATERMARK_OPACITY, so the text underneath
        stays readable.

    A source that already carries alpha is trusted about which pixels are
    background -- its transparency is scaled, not second-guessed.

    Done once per document rather than per page: it walks every pixel, and a
    554x554 logo is ~300k of them.
    """
    if not image_bytes:
        return None
    try:
        pix = fitz.Pixmap(image_bytes)
        if pix.n - pix.alpha < 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)      # greyscale/CMYK -> RGB
        had_alpha = bool(pix.alpha)
        if not had_alpha:
            pix = fitz.Pixmap(pix, 1)               # add a fully opaque alpha
        data = bytearray(pix.samples)
        if len(data) != pix.width * pix.height * 4:
            return pix                              # unexpected layout; leave it alone
        cutoff = WATERMARK_WHITE_CUTOFF
        opacity = WATERMARK_OPACITY
        for i in range(0, len(data), 4):
            if not had_alpha and (data[i] >= cutoff and data[i + 1] >= cutoff
                                  and data[i + 2] >= cutoff):
                data[i] = data[i + 1] = data[i + 2] = data[i + 3] = 0
                continue                            # background -> transparent
            if not data[i + 3]:
                continue                            # already transparent
            # PyMuPDF pixmap alpha is PREMULTIPLIED, so the colour channels
            # have to be scaled by the same factor as alpha. Writing straight
            # RGBA here turned the client's blue logo into a green smear:
            # (20,74,138,40) rendered as (218,247,216) instead of the correct
            # (218,227,237). Scaling all four channels keeps premultiplied
            # samples premultiplied, which is also right for a source that
            # already carried alpha.
            data[i] = int(data[i] * opacity)
            data[i + 1] = int(data[i + 1] * opacity)
            data[i + 2] = int(data[i + 2] * opacity)
            data[i + 3] = max(1, int(data[i + 3] * opacity))
        return fitz.Pixmap(fitz.csRGB, pix.width, pix.height, bytes(data), True)
    except Exception:
        return None


def _watermark_image(page: fitz.Page, pixmap: fitz.Pixmap) -> None:
    """Stamp the prepared watermark, centred on both axes.

    Sized from WATERMARK_WIDTH_RATIO with the aspect ratio preserved, so a
    wide logo does not get blown up to fill a square box the way the previous
    50%-by-50% target did.
    """
    page_rect = page.rect
    width = page_rect.width * WATERMARK_WIDTH_RATIO
    height = width * (pixmap.height / pixmap.width) if pixmap.width else width
    # Never taller than a third of the page, however tall the source is.
    max_height = page_rect.height / 3
    if height > max_height:
        width *= max_height / height
        height = max_height

    cx, cy = page_rect.width / 2, page_rect.height / 2
    page.insert_image(
        fitz.Rect(cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2),
        pixmap=pixmap,
        # BEHIND the text, not over it: overlay=False puts the image at the
        # start of the content stream so every glyph is painted on top of it.
        # Faded and underneath is the only combination that reads as a
        # watermark rather than as something spilled on the page.
        overlay=False,
        keep_proportion=True,
    )


def _watermark_text(page: fitz.Page, text: str) -> None:
    """Fallback: diagonal, semi-transparent watermark text if no image provided.

    Sized to a modest fraction of the page width (measured via real font
    metrics, not a length-based guess) so it reads as a watermark stamp
    rather than a banner that drowns out the resume content underneath.
    """
    rect = page.rect
    font = fitz.Font("helv")
    max_width = rect.width * 0.6

    font_size = 36.0
    while font_size > 8 and font.text_length(text, fontsize=font_size) > max_width:
        font_size -= 2

    # insert_textbox aligns horizontally but starts at the TOP of the box, so
    # passing the whole page put the "centered" watermark across the header.
    # A box one line tall, centred vertically, actually centres it.
    band = font_size * 1.6
    box = fitz.Rect(rect.x0, rect.height / 2 - band / 2,
                    rect.x1, rect.height / 2 + band / 2)

    page.insert_textbox(
        box,
        text,
        fontsize=font_size,
        color=(0.4, 0.4, 0.4),
        fill_opacity=0.25,
        overlay=True,
        align=fitz.TEXT_ALIGN_CENTER,
    )
