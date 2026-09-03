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

import fitz

from . import pii

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
                    out.append(fitz.Rect(
                        min(line[k][0] for k in range(i, j + 1)),
                        min(line[k][1] for k in range(i, j + 1)),
                        max(line[k][2] for k in range(i, j + 1)),
                        max(line[k][3] for k in range(i, j + 1)),
                    ))
                    break
    return _dedupe_rects(out)


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


def _rects_for(page: fitz.Page, s: str, words: list) -> list[fitz.Rect]:
    """Every region of `page` that should be redacted for the PII string `s`,
    using the matching strategy its kind calls for."""
    kind = pii.classify(s)
    if kind == pii.PHONE:
        return _phone_rects(page, s, words)
    if kind == pii.EMAIL:
        return page.search_for(s)
    # Name (and any literal a caller passed explicitly): whole-word only.
    if len(s.strip()) < 3:
        return []  # too short to match safely — would hit half the page
    return [r for r in page.search_for(s) if _covers_whole_words(r, words)]


def mask_pdf_bytes(pdf_bytes: bytes, mask_strings: list[str],
                   watermark_png: bytes | None = None,
                   watermark_text: str = "") -> tuple[bytes, int]:
    """True-redact PII strings, then overlay watermark.

    Args:
        pdf_bytes: Raw resume PDF bytes.
        mask_strings: Exact strings to redact (name, phone, email from parser).
        watermark_png: Client watermark image bytes (PNG/JPEG). Centered on every page.
        watermark_text: Fallback text watermark if no image provided.

    Returns:
        (masked_pdf_bytes, redacted_region_count)
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    hits = 0

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

        for rect in _dedupe_rects(page_rects):
            page.add_redact_annot(rect, fill=REDACT_FILL)
            hits += 1

        page.apply_redactions()

        # Apply watermark
        if watermark_png:
            _watermark_image(page, watermark_png)
        elif watermark_text:
            _watermark_text(page, watermark_text)

    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out, hits


def _watermark_image(page: fitz.Page, png_bytes: bytes) -> None:
    """Stamp a centered watermark image overlay on the page.

    Image is scaled to ~50% of page width, centered both axes.
    For semi-transparency, the PNG itself should have an alpha channel.
    """
    rect = page.rect
    target_w = rect.width * 0.50
    target_h = rect.height * 0.50

    page.insert_image(
        fitz.Rect(
            rect.width / 2 - target_w / 2,
            rect.height / 2 - target_h / 2,
            rect.width / 2 + target_w / 2,
            rect.height / 2 + target_h / 2,
        ),
        stream=png_bytes,
        overlay=True,
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

    page.insert_textbox(
        rect,
        text,
        fontsize=font_size,
        color=(0.4, 0.4, 0.4),
        fill_opacity=0.25,
        overlay=True,
        align=fitz.TEXT_ALIGN_CENTER,
    )
