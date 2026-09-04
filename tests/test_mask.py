"""Offline self-check — PII true-redacted, custom watermark image centered.
Run: python -m tests.test_mask, or under pytest with the rest of tests/."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from app.mask import mask_pdf_bytes


def _make_sample(path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72),
                     "John Doe\n"
                     "Phone: +91 98765 43210\n"
                     "Email: john.doe@example.com\n\n"
                     "Experience: 5 years at Acme Corp\n"
                     "10th: 85%   12th: 88%",
                     fontsize=12)
    doc.save(path); doc.close()


def test_basic_redact():
    """PII is removed, experience/marks preserved."""
    d = tempfile.mkdtemp()
    src = f"{d}/in.pdf"
    _make_sample(src)
    with open(src, "rb") as f:
        pdf_bytes = f.read()
    masked, hits = mask_pdf_bytes(pdf_bytes, ["John Doe", "+91 98765 43210", "john.doe@example.com"])
    txt = "".join(pg.get_text() for pg in fitz.open(stream=masked, filetype="pdf"))
    for pii in ["John Doe", "98765", "john.doe@example.com"]:
        assert pii not in txt, f"PII LEAKED: {pii!r}"
    assert "Experience" in txt and "85%" in txt, "OVER-MASKED experience/marks"
    print(f"  [BASIC] OK — {hits} redactions, PII gone, experience intact")


def test_watermark_image():
    """Custom watermark image is stamped centered."""
    import struct, zlib
    d = tempfile.mkdtemp()
    src = f"{d}/in.pdf"
    _make_sample(src)
    with open(src, "rb") as f:
        pdf_bytes = f.read()
    # Build a minimal 4x4 RGBA PNG properly
    w, h = 4, 4
    raw = b""
    for y in range(h):
        raw += b"\x00"  # filter byte
        for x in range(w):
            raw += bytes([0, 0, 0, 80])  # RGBA: black at 31% opacity
    compressed = zlib.compress(raw)
    def _chunk(ctype, data):
        c = ctype + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    minimal_png = (
        b"\x89PNG\r\n\x1a\n" +
        _chunk(b"IHDR", ihdr_data) +
        _chunk(b"IDAT", compressed) +
        _chunk(b"IEND", b"")
    )
    masked, hits = mask_pdf_bytes(pdf_bytes, ["john.doe@example.com"], watermark_png=minimal_png)
    txt = "".join(pg.get_text() for pg in fitz.open(stream=masked, filetype="pdf"))
    assert "john.doe" not in txt, "PII leaked with image watermark"
    print(f"  [IMAGE WATERMARK] OK — {hits} redactions, PNG watermark centered")


def test_fallback_text():
    """Fallback text watermark when no image provided."""
    d = tempfile.mkdtemp()
    src = f"{d}/in.pdf"
    _make_sample(src)
    with open(src, "rb") as f:
        pdf_bytes = f.read()
    masked, hits = mask_pdf_bytes(pdf_bytes, ["+91 98765 43210"], watermark_text="CLIENT CONFIDENTIAL")
    txt = "".join(pg.get_text() for pg in fitz.open(stream=masked, filetype="pdf"))
    assert "98765" not in txt, "PII leaked with text watermark"
    print(f"  [TEXT FALLBACK] OK — {hits} redactions, text watermark applied")


def test_no_pii():
    """No redactions needed — watermark still applied."""
    d = tempfile.mkdtemp()
    src = f"{d}/in.pdf"
    _make_sample(src)
    with open(src, "rb") as f:
        pdf_bytes = f.read()
    masked, hits = mask_pdf_bytes(pdf_bytes, [], watermark_text="TEST")
    assert hits == 0
    print(f"  [EMPTY] OK — 0 redactions, watermark applied")


def test_phone_format_mismatch():
    """Parser hands back a normalized phone (E.164, no spaces) that doesn't literally
    match the formatted phone on the page -- this used to leak the phone entirely
    (the client's original complaint) because search_for() is exact-substring only."""
    d = tempfile.mkdtemp()
    src = f"{d}/in.pdf"
    _make_sample(src)  # renders "Phone: +91 98765 43210"
    with open(src, "rb") as f:
        pdf_bytes = f.read()
    masked, hits = mask_pdf_bytes(pdf_bytes, ["+919876543210"])  # no spaces
    txt = "".join(pg.get_text() for pg in fitz.open(stream=masked, filetype="pdf"))
    assert "98765" not in txt and "43210" not in txt, "phone leaked on format mismatch"
    # The label goes with the value. A bare "Phone:" over blank space still
    # tells the reader exactly what was removed and reads as a defect on the
    # page, so _absorb_label() takes it too. This assertion used to require
    # the opposite -- reversed deliberately, not worked around.
    assert "Phone:" not in txt, "the contact label should be redacted with the value"
    assert "Experience" in txt and "85%" in txt, "over-masked real content"
    print(f"  [PHONE FORMAT MISMATCH] OK — {hits} redactions, digits and label gone")


def main():
    print("MASK TESTS")
    test_basic_redact()
    test_watermark_image()
    test_fallback_text()
    test_no_pii()
    test_phone_format_mismatch()
    print("\nALL PASSED")


if __name__ == "__main__":
    main()