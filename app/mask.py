"""Resume masking core — TRUE-redact PII + centered watermark IMAGE overlay.

Watermark is a client-specific PNG image (logo/brand) stored in Salesforce,
fetched at mask-time, stamped center-aligned on every page of the resume PDF.
"""
from __future__ import annotations

import fitz


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
            for rect in page.search_for(str(s)):
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