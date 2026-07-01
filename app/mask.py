"""Resume masking core — TRUE-redact PII + centered watermark IMAGE overlay.

Watermark is a client-specific PNG image (logo/brand) stored in Salesforce,
fetched at mask-time, stamped center-aligned on every page of the resume PDF.
"""
from __future__ import annotations

import re

import fitz

_DIGITS_RE = re.compile(r"\d+")


def _digits(s: str) -> str:
    return "".join(_DIGITS_RE.findall(s))


def _is_phone_like(s: str) -> bool:
    return len(_digits(s)) >= 7


def _phone_rects(page: fitz.Page, target: str) -> list[fitz.Rect]:
    """Locate a phone number by its digits, not its literal formatting.

    page.search_for() is an exact substring match, so it misses a phone number
    whenever mask_strings' formatting differs from what the PDF actually renders
    (e.g. the on-prem parser stores it normalized as E.164 "+919876543210" while
    the resume shows "+91 98765 43210" — the original client complaint about
    phone digits leaking through). This walks each line's words and matches on
    concatenated digits instead, tolerant of a missing/extra country code or
    trunk-prefix digit.
    """
    target_digits = _digits(target)
    if len(target_digits) < 7:
        return []

    words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)
    lines: dict[tuple[int, int], list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    out: list[fitz.Rect] = []
    for line in lines.values():
        n = len(line)
        for i in range(n):
            if not _digits(line[i][4]):
                continue  # only start a window on a word that itself has digits —
                          # otherwise a match can grow backwards into a label like "Phone:"
            digits = ""
            for j in range(i, min(i + 6, n)):
                digits += _digits(line[j][4])
                if len(digits) > len(target_digits) + 4:
                    break
                close_enough = abs(len(digits) - len(target_digits)) <= 3
                if digits and close_enough and (target_digits in digits or digits in target_digits):
                    x0 = min(line[k][0] for k in range(i, j + 1))
                    y0 = min(line[k][1] for k in range(i, j + 1))
                    x1 = max(line[k][2] for k in range(i, j + 1))
                    y1 = max(line[k][3] for k in range(i, j + 1))
                    out.append(fitz.Rect(x0, y0, x1, y1))
                    break
    return out


def mask_pdf_bytes(pdf_bytes: bytes, mask_strings: list[str],
                   watermark_png: bytes | None = None,
                   watermark_text: str = "") -> tuple[bytes, int]:
    """True-redact PII strings + overlay centered watermark image.

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
        # Redact each PII string
        for s in mask_strings:
            if not s:
                continue
            rects = page.search_for(str(s))
            if not rects and _is_phone_like(str(s)):
                rects = _phone_rects(page, str(s))
            for rect in rects:
                page.add_redact_annot(rect, fill=(0, 0, 0))
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
    """Fallback: centered watermark text if no image provided."""
    rect = page.rect
    font_size = rect.width / max(len(text), 1) * 1.5
    font_size = min(max(font_size, 18), 72)

    page.insert_textbox(
        rect,
        text,
        fontsize=font_size,
        color=(0.4, 0.4, 0.4),
        overlay=True,
        align=fitz.TEXT_ALIGN_CENTER,
    )