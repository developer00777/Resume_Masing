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


def _is_phone_like(s: str) -> bool:
    return pii.classify(s) == pii.PHONE


def _line_words(words: list) -> dict[tuple[int, int], list]:
    """Group page words by (block_no, line_no)."""
    lines: dict[tuple[int, int], list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)
    return lines


def _phone_rects(page: fitz.Page, target: str, words: list | None = None) -> list[fitz.Rect]:
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

    if words is None:
        words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)

    out: list[fitz.Rect] = []
    for line in _line_words(words).values():
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
                    ), (line[i][5], line[i][6]), words))
                    break
    return _dedupe_rects(out)


def _clip_literal(rect: fitz.Rect, words: list) -> fitz.Rect:
    """_clip_to_line for a search_for() hit, whose line has to be inferred
    from whichever line contributes most of the words the hit covers."""
    best, best_area = None, 0.0
    for w in words:
        overlap = fitz.Rect(w[:4]) & rect
        if overlap.is_empty:
            continue
        area = overlap.get_area()
        if area > best_area:
            best, best_area = (w[5], w[6]), area
    return rect if best is None else _clip_to_line(rect, best, words)


def _clip_to_line(rect: fitz.Rect, line_key: tuple[int, int], words: list) -> fitz.Rect:
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
    own = [w for w in words
           if (w[5], w[6]) == line_key and not (fitz.Rect(w[:4]) & rect).is_empty]
    if not own:
        return rect

    mid = (rect.y0 + rect.y1) / 2
    top, bottom = rect.y0, rect.y1
    for w in words:
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


def _bridge_separators(rects: list[fitz.Rect], words: list) -> list[fitz.Rect]:
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
            between = [w for w in words
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


def _covers_whole_words(rect: fitz.Rect, words: list) -> bool:
    """Does `rect` cover whole words, rather than clipping into one?

    search_for() has no word-boundary option, so a short name matches inside
    longer words. Comparing the hit's width against the width of each word it
    touches tells the two cases apart: a whole-word hit spans the word, a
    fragment hit covers only part of it.
    """
    for w in words:
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


#: Letters only — how a name token is compared, so "Sharma," and "SHARMA"
#: both reduce to "sharma".
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+")

#: How many unmatched words may sit between two matched name tokens. One
#: covers the common case of a middle name printed on the resume but absent
#: from the Contact record. More than that and we would start joining up
#: unrelated words that happen to share a surname.
_NAME_MAX_GAP = 1

#: A match must cover at least this many letters in total. Stops two short
#: coincidental tokens from being read as a name.
_NAME_MIN_CHARS = 6


def _name_token_list(name: str) -> list[str]:
    """Name tokens worth matching on, initials dropped.

    A single initial carries no evidence and matches far too much, so "Samar S
    Wadyalkar" is matched as ["samar", "wadyalkar"].
    """
    return [t.casefold() for t in _NAME_TOKEN_RE.findall(str(name)) if len(t) >= 3]


#: Shortest lone name token we will redact on its own. Below this a token is
#: too easily an acronym or an ordinary short word to act on without the
#: corroboration of a neighbouring token.
_NAME_LONE_TOKEN_MIN = 4


def _lone_token_rects(tokens: list[str], words: list) -> list[fitz.Rect]:
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
    for w in words:
        text = w[4].strip(" .,;:()[]-|/")
        if not text or "@" in text:
            continue                      # emails are matched as emails
        folded = text.casefold()
        if folded in joined:
            out.append(_clip_to_line(fitz.Rect(w[:4]), (w[5], w[6]), words))
            continue
        if folded not in wanted:
            continue
        if not (text.istitle() or text.isupper()):
            continue                      # lowercase in running prose
        out.append(_clip_to_line(fitz.Rect(w[:4]), (w[5], w[6]), words))
    return out


def _name_rects(page: fitz.Page, name: str, words: list) -> list[fitz.Rect]:
    """Locate a candidate's name even when the resume spells it differently.

    page.search_for() needs the whole string present verbatim, and on real
    records it very often is not -- the Contact holds a middle name the resume
    omits, or the resume prints one the Contact lacks:

        Contact "Sitendra Kumar Chakra"   resume "SITENDRA CHAKRA"
        Contact "Samar Wadyalkar"         resume "SAMAR SHIVAJI WADYALKAR"

    Confirmed on live data, where roughly a third of sampled records had a
    Contact name that appears nowhere verbatim in the resume -- so the name was
    not redacted at all, and sat in the page heading of the masked copy while
    phone and email were blacked out.

    Matches the tokens as an ordered subsequence within a single line, letting
    either side carry extra words, and redacts the whole span (a middle name
    on the resume is part of the name, so covering it is correct). Requires at
    least two tokens to match: one token alone would mask every occurrence of
    an ordinary word for a candidate named Will, Rose or Mark.
    """
    tokens = _name_token_list(name)
    if not tokens:
        return []

    out: list[fitz.Rect] = _lone_token_rects(tokens, words)
    if len(tokens) < 2:
        return out

    for line in _line_words(words).values():
        norm = ["".join(_NAME_TOKEN_RE.findall(w[4])).casefold() for w in line]
        n = len(line)
        for start in range(n):
            if not norm[start] or norm[start] not in tokens:
                continue
            next_token = 0
            matched = 0
            chars = 0
            last = start
            gaps = 0
            for j in range(start, n):
                if not norm[j]:
                    continue                       # punctuation-only word
                hit = next((k for k in range(next_token, len(tokens))
                            if tokens[k] == norm[j]), None)
                if hit is not None:
                    matched += 1
                    chars += len(norm[j])
                    next_token = hit + 1
                    last = j
                    gaps = 0
                    if next_token >= len(tokens):
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
                ), (line[start][5], line[start][6]), words))
    return out


#: The label sitting immediately before a contact value. Redacted along with
#: the value, because a bare "Contact:" or "Email:" followed by white space
#: still tells the reader exactly what was removed, and reads as a defect on
#: the page. Matched on the whole word so a sentence beginning "Contact the
#: site engineer" is untouched.
#:
#: A real label is written as several words -- "Alternate E-Mail ID :",
#: "Personal Mob No." -- and _absorb_label walks them right to left, so all
#: three kinds of word have to be listed or the walk stops early and strands
#: what it did not reach. The reported defect was exactly that: "Email ID:"
#: matched "Email" but not "ID", so absorption stopped at the colon and the
#: masked page kept an "Email ID:" sitting over white space.
_CONTACT_LABEL_RE = re.compile(
    # the contact channel
    r"^(?:e[-\s]?mail|email|mail|e|phone|mobile|mob|cell(?:ular)?|"
    r"tel(?:ephone)?|contact|whats?[\s-]?app|ph|landline|fax|skype|"
    # the qualifier in front of it
    r"alt(?:\.|ernat(?:e|ive))?|second(?:ary)?|primary|personal|official|"
    r"office|work|home|res(?:idence|idential)?|permanent|current|"
    r"name|candidate|applicant|"
    # the trailing noun
    r"id|ids|i\.?d\.?|address|addr|detail(?:s)?|info|no|nos|num(?:ber)?"
    r")[\s.:\-–—#|()]*$"
    # a separator stranded on its own between two label words ("E - Mail ID")
    r"|^[\s.:\-–—#|/]+$",
    re.I,
)


def _absorb_label(rect: fitz.Rect, words: list) -> fitz.Rect:
    """Grow `rect` leftwards over a contact label that precedes the value.

    On a real resume this is the difference between

        Contact:                       and        (nothing)
        Email:

    left standing over blank space, and a clean page. The label is only taken
    when it sits immediately to the left on the same line and the whole word is
    a label, so prose is never eaten.
    """
    grown = rect
    line_key = None
    # Labels come in runs: "Mob No.- 98765...", "Contact No :", "E-Mail ID:".
    # Taking only the nearest word left "Mob" standing on a real resume, so
    # this walks leftwards while each next word is still a label. Five steps,
    # because "Alternate E - Mail ID :" is five words and stopping short of
    # the start of a label run is what leaves half of it on the page.
    for _ in range(5):
        candidates = [w for w in words
                      if w[2] <= grown.x0 + 1.0
                      and w[3] > grown.y0 and w[1] < grown.y1]
        if not candidates:
            break
        nearest = max(candidates, key=lambda w: w[2])
        if grown.x0 - nearest[2] > 12.0:
            break                        # too far away to be this value's label
        if not _CONTACT_LABEL_RE.match(nearest[4].strip()):
            break
        grown = fitz.Rect(nearest[0], min(grown.y0, nearest[1]),
                          grown.x1, max(grown.y1, nearest[3]))
        line_key = (nearest[5], nearest[6])
    if line_key is None:
        return rect
    # Absorbing labels must not drag the rect onto another line.
    return _clip_to_line(grown, line_key, words)


def _rects_for(page: fitz.Page, s: str, words: list) -> list[fitz.Rect]:
    """Every region of `page` that should be redacted for the PII string `s`,
    using the matching strategy its kind calls for."""
    kind = pii.classify(s)
    if kind == pii.PHONE:
        return [_absorb_label(r, words) for r in _phone_rects(page, s, words)]
    if kind == pii.EMAIL:
        return [_absorb_label(_clip_literal(r, words), words)
                for r in page.search_for(s)]
    # Name (and any literal a caller passed explicitly): whole-word only.
    if len(s.strip()) < 3:
        return []  # too short to match safely — would hit half the page
    literal = [_clip_literal(r, words) for r in page.search_for(s)
               if _covers_whole_words(r, words)]
    if len(_name_token_list(s)) < 2:
        # A one-token name has to match case as written. search_for() is
        # case-insensitive, so a candidate actually named Will, Rose or Mark
        # otherwise has every ordinary occurrence of that word redacted out of
        # their own resume ("I will manage delivery" -> "I  manage delivery").
        # A heading still matches, as "Will" or as "WILL".
        literal = [r for r in literal if _matches_case(r, words, s)]
    # Union, not either/or: a resume can print the name verbatim in one place
    # and with an extra middle name in another, and both have to go. The
    # label goes too, for the same reason it does on a phone or an email: a
    # "Candidate Name:" left standing over white space is the defect, not the
    # fix.
    return [_absorb_label(r, words)
            for r in _dedupe_rects(literal + _name_rects(page, s, words))]


def _matches_case(rect: fitz.Rect, words: list, needle: str) -> bool:
    """Is the text under `rect` written the same way as `needle`?

    Accepts the needle as-is or fully upper-cased, which is how a name appears
    in a resume heading.
    """
    covered = []
    for w in words:
        wr = fitz.Rect(w[0], w[1], w[2], w[3])
        inter = wr & rect
        if inter.is_empty or inter.width <= 0:
            continue
        if inter.height < min(wr.height, rect.height) * 0.5:
            continue
        covered.append(w[4])
    text = " ".join(covered).strip(" .,;:()[]-")
    return text in (needle, needle.upper())


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

    for page in doc:
        # Read the word layout once per page — every mask string is matched
        # against it, and get_text() is the expensive part of this loop.
        words = page.get_text("words")

        page_rects: list[fitz.Rect] = []
        # expand() splits a Contact field holding several phone numbers into
        # one entry per number. Left whole, such a value is too long to
        # classify as a phone and gets searched as a single literal that
        # appears in no resume -- so the candidate's phone goes unmasked.
        for s in pii.expand([str(s) for s in mask_strings]):
            page_rects.extend(_rects_for(page, s, words))

        for rect in _bridge_separators(_dedupe_rects(page_rects), words):
            page.add_redact_annot(rect, fill=REDACT_FILL)
            hits += 1

        page.apply_redactions()

        # --- second pass --------------------------------------------------
        # Re-read the page: `words` above describes the page before
        # redaction, and every rect from here has to be measured against
        # what is actually left. Clipping and label absorption are the same
        # ones the first pass uses, so a residual hit is redacted exactly
        # like a Contact-record hit, label and all.
        if residual_sweep:
            left = page.get_text("words")
            found = [_absorb_label(_clip_to_line(rect, key, left), left)
                     for rect, key in residual.find_residual_rects(page, left)]
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
