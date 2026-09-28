"""OCR of image-only resumes (app/ocr.py) and how /mask treats them."""
import base64

import fitz
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import mask, ocr, server
from tests.test_server import _install_mocks

NAME, EMAIL, PHONE = "Rahul Sharma", "rahul.sharma@example.com", "+91 98765 43210"


def _text_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in (NAME, f"Email: {EMAIL}", f"Phone: {PHONE}", "",
                 "Site Engineer with 5 years of experience in solar installations.",
                 "Skills: AutoCAD, Site Supervision, Quality Control"):
        page.insert_text((72, y), line, fontsize=14 if line == NAME else 11)
        y += 22
    return doc.tobytes()


def _as_image_pdf(pdf: bytes, dpi: int = 150) -> bytes:
    """The same resume as a picture: one JPEG per page, no text layer."""
    src = fitz.open(stream=pdf, filetype="pdf")
    out = fitz.open()
    for page in src:
        jpg = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=80)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpg)
    return out.tobytes()


def test_needs_ocr_only_for_picture_pages():
    text = fitz.open(stream=_text_pdf(), filetype="pdf")[0]
    image = fitz.open(stream=_as_image_pdf(_text_pdf()), filetype="pdf")[0]
    assert not ocr.needs_ocr(text)
    assert ocr.needs_ocr(image)


def test_text_pdf_comes_back_untouched():
    pdf = _text_pdf()
    out, needed, done = ocr.ensure_text_layer(pdf, [NAME])
    assert (needed, done) == (0, 0) and out is pdf


def test_invisible_layer_sits_on_the_word_pixels():
    doc = fitz.open()
    page = doc.new_page()
    zoom = 300 / 72
    words = [("Rahul", 300, 400, 520, 460, 90.0), ("Sharma", 560, 402, 830, 462, 90.0)]
    ocr._write_layer(page, [words], zoom)
    got = page.get_text("words")
    assert [g[4] for g in got] == ["Rahul", "Sharma"]
    for (text, x0, y0, x1, y1, _), g in zip(words, got):
        assert abs(g[0] - x0 / zoom) < 1 and abs(g[2] - x1 / zoom) < 1
        assert g[1] <= y0 / zoom + 0.5 and g[3] >= y1 / zoom - 0.5   # covers the glyphs
    assert len({g[6] for g in got}) == 1                             # read as one line
    # and it is invisible
    assert page.get_pixmap().samples == doc.new_page().get_pixmap().samples


def test_snap_rewrites_near_misses_to_known_values():
    emails, phones, names = ocr._known([NAME, EMAIL, PHONE])
    line = [("rahul.sharma@exarnple.com", 0, 0, 100, 10, 60.0)]
    assert ocr._snap(line, emails, phones, names)[0][0] == EMAIL
    line = [("98765", 0, 0, 50, 10, 60.0), ("43218", 55, 0, 100, 10, 60.0)]   # one digit off
    snapped = ocr._snap(line, emails, phones, names)
    assert len(snapped) == 1 and snapped[0][0] == "9876543210"
    line = [("SHARNA", 0, 0, 50, 10, 60.0)]
    assert ocr._snap(line, emails, phones, names)[0][0] == "SHARMA"


def test_snap_leaves_unrelated_words_alone():
    emails, phones, names = ocr._known([NAME, EMAIL, PHONE])
    line = [("Experience", 0, 0, 50, 10, 90.0), ("2019-2024", 55, 0, 100, 10, 90.0)]
    assert ocr._snap(line, emails, phones, names) == line
    line = [("98765", 0, 0, 50, 10, 60.0), ("43718", 55, 0, 100, 10, 60.0)]   # two off: a different number
    assert ocr._snap(line, emails, phones, names) == line


def test_dark_panel_is_inverted():
    img = np.full((600, 800), 250, np.uint8)
    img[100:200, 50:750] = 30                      # navy header bar...
    img[140:160, 100:400] = 245                    # ...with light text on it
    out = ocr._invert_dark_panels(img)
    assert out[150, 200] < 60                      # text is now dark
    assert out[120, 600] > 200                     # bar is now light
    assert out[400, 400] == 250                    # page untouched


def test_mask_refuses_a_scan_it_cannot_read(monkeypatch):
    _install_mocks(monkeypatch)
    scan = _as_image_pdf(_text_pdf())
    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", lambda jaid, sf=None: (scan, "pdf"))
    monkeypatch.setattr(ocr, "available", lambda: False)
    body = TestClient(server.app).post("/mask", json={"job_applicant_id": "a0X000000000001",
                                                      "mask_strings": [NAME]}).json()
    assert body["status"] == "error" and "scanned image" in body["detail"]


def test_mask_refuses_when_ocr_finds_nothing_to_redact(monkeypatch):
    captured = _install_mocks(monkeypatch)
    scan = _as_image_pdf(_text_pdf())
    monkeypatch.setattr(server.sf_client, "fetch_resume_pdf", lambda jaid, sf=None: (scan, "pdf"))
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "ensure_text_layer", lambda pdf, known=(): (pdf, 1, 1))
    body = TestClient(server.app).post("/mask", json={"job_applicant_id": "a0X000000000001",
                                                      "mask_strings": [NAME]}).json()
    assert body["status"] == "error" and "manually" in body["detail"]
    assert "pdf_bytes" not in captured             # nothing uploaded


needs_tesseract = pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")


@needs_tesseract
def test_scan_is_masked_end_to_end():
    scan = _as_image_pdf(_text_pdf())
    pdf, needed, done = ocr.ensure_text_layer(scan, [NAME, EMAIL, PHONE])
    assert (needed, done) == (1, 1)
    text = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
    assert EMAIL in text and "Sharma" in text
    masked, hits = mask.mask_pdf_bytes(pdf, [NAME, EMAIL, PHONE], watermark_text="")
    assert hits >= 3
    # Read the masked PICTURE again from scratch: the pixels must be gone.
    page = fitz.open(stream=masked, filetype="pdf")[0]
    reread = " ".join(w[0] for w in ocr._read(ocr._render(page)[0]))
    assert "Sharma" not in reread and "example.com" not in reread and "43210" not in reread
    assert "Engineer" in reread                    # and the rest of the resume is still there


@needs_tesseract
def test_text_typed_over_a_scan_is_still_masked():
    """A recruiter's typed header over the picture: the real text must win,
    not be shadowed by an OCR copy of itself (JA-26412, JA-26296)."""
    doc = fitz.open(stream=_as_image_pdf(_text_pdf()), filetype="pdf")
    doc[0].insert_text((300, 40), EMAIL, fontsize=10)          # real text, over the image
    pdf, needed, done = ocr.ensure_text_layer(doc.tobytes(), [NAME, EMAIL, PHONE])
    assert (needed, done) == (1, 1)
    masked, _ = mask.mask_pdf_bytes(pdf, [NAME, EMAIL, PHONE], watermark_text="")
    out = fitz.open(stream=masked, filetype="pdf")[0]
    assert "example.com" not in out.get_text()
    reread = " ".join(w[0] for w in ocr._read(ocr._render(out)[0]))
    assert "example.com" not in reread and "Sharma" not in reread


@needs_tesseract
def test_inline_masks_a_scan():
    scan = _as_image_pdf(_text_pdf())
    body = TestClient(server.app).post("/mask/inline", json={
        "resume_base64": base64.b64encode(scan).decode(), "mask_strings": [NAME, EMAIL, PHONE]}).json()
    assert body["status"] == "ok" and body["redacted_regions"] >= 3
