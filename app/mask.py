"""Resume masking core — TRUE-redact specific PII strings + centered watermark. Offline, no Salesforce.

Fixes the freelancer service's two complaints:
  • TRUE redaction (PyMuPDF apply_redactions) DELETES the glyphs, so masked text can't be copied out
    (not a black box drawn over selectable text).
  • Masks ONLY the exact strings you pass (name/phone/email from the on-prem parser) — so it never
    over-masks experience or academic marks (the client's exact bug).
"""
from __future__ import annotations
import fitz  # PyMuPDF


def mask_pdf(in_path: str, out_path: str, mask_strings, watermark_png: bytes | None = None,
             watermark_text: str = "CONFIDENTIAL") -> int:
    """Redact every occurrence of each string in `mask_strings`, stamp a centered watermark on every
    page, save to out_path. Returns the number of redacted regions."""
    doc = fitz.open(in_path)
    hits = 0
    for page in doc:
        for s in mask_strings:
            if not s:
                continue
            for rect in page.search_for(str(s)):
                page.add_redact_annot(rect, fill=(0, 0, 0))
                hits += 1
        page.apply_redactions()                 # <-- deletes the underlying text, not a cosmetic box
        _watermark(page, watermark_png, watermark_text)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    return hits


def mask_pdf_bytes(pdf_bytes: bytes, mask_strings, watermark_png: bytes | None = None,
                   watermark_text: str = "CONFIDENTIAL") -> tuple[bytes, int]:
    """In-memory variant of mask_pdf for the service: takes the raw PDF as bytes (fetched from
    Salesforce), redacts every occurrence of each string in `mask_strings`, stamps a centered
    watermark on every page, returns (masked_pdf_bytes, redacted_region_count).

    Nothing touches disk — the resume never leaves memory (security requirement)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    hits = 0
    for page in doc:
        for s in mask_strings:
            if not s:
                continue
            for rect in page.search_for(str(s)):
                page.add_redact_annot(rect, fill=(0, 0, 0))
                hits += 1
        page.apply_redactions()                  # <-- deletes the underlying text, not a cosmetic box
        _watermark(page, watermark_png, watermark_text)
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out, hits


def _watermark(page, png: bytes | None, text: str):
    r = page.rect
    cx, cy = r.width / 2, r.height / 2
    if png:                                      # real run: client logo PNG bytes fetched from SF
        w = r.width * 0.45
        page.insert_image(fitz.Rect(cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2),
                          stream=png, overlay=True, keep_proportion=True)
    else:                                        # fallback: faint centered text watermark
        tw = fitz.TextWriter(r)
        fs = 36
        tw.append(fitz.Point(cx - len(text) * fs * 0.27, cy), text, fontsize=fs)
        tw.write_text(page, opacity=0.18, color=(0.5, 0.5, 0.5))
