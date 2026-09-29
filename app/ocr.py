"""OCR for resumes that are pictures, so masking has text to work on.

Some resumes arrive as a PDF whose every page is a single image -- a Canva
export saved as JPEG, a phone photo, a scan. There is no text layer, so every
search the masking pipeline makes comes back empty: confirmed on JA-9635 and
JA-9703, and on about 1% of recent applicants, the masked copy was uploaded
with "ok" and the candidate's phone, email and address all still on it.

This gives such a page a text layer. Tesseract reads the page image, and each
word it finds is written back onto the page as INVISIBLE text sitting exactly
over the word's pixels. From there nothing downstream changes: search_for(),
get_text("words"), the residual sweep and the label walk all work on the
invisible layer as they do on a born-digital PDF, and apply_redactions()
(images=PDF_REDACT_IMAGE_PIXELS, PyMuPDF's default) blanks the image pixels
under every redaction, so the glyphs go from the picture itself.

Accuracy work, measured on the org's real image-only resumes (see
tests/ocr_benchmark.py):

  * Two passes, merged. Tesseract's layout analysis (psm 3) on the page as
    rendered, and its sparse-text mode (psm 11) on a cleaned-up copy. Sparse
    mode finds the contact line in a sidebar that layout analysis folds into
    a column and misreads; layout mode reads flowing text better. Masking
    needs recall more than anything, so every word either pass is sure of
    is kept, and where they overlap the more confident reading wins.

  * The cleaned-up copy (_clean):
      - dark panels are segmented and inverted -- white text on a navy header
        bar is invisible to a reader expecting dark text on light;
      - the background is flattened by dividing out a morphological closing
        of the page (a large-kernel close removes text and keeps the panels,
        tints and gradients behind it), which lifts grey text off tinted
        cards;
      - a light median filter takes out JPEG ringing before Tesseract
        binarises.

  * Snapping to known values. The Contact record says what the phone
    and email ARE; OCR only has to find where. A reading one or two
    characters off a known value ("gmaiI.com", one wrong digit) is rewritten
    to the value, so the exact matching in app/mask.py still finds it.

Only pages that need it are touched: a page with a real text layer is left
exactly as it was, and so is any page this cannot read (logged, not raised --
the caller decides what an unreadable scan means).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import fitz

try:
    import cv2
    import numpy as np
except ImportError:          # the service still runs, it just cannot OCR
    cv2 = np = None

logger = logging.getLogger(__name__)

TESSERACT = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
LANG = os.environ.get("OCR_LANG", "eng")

#: A page with fewer characters than this in its own text layer, and an image
#: covering at least IMAGE_PAGE_MIN_COVER of it, is a picture of a resume.
#: JA-9635 has a 39-character strip of real text over a full-page image.
IMAGE_PAGE_MAX_CHARS = 100
IMAGE_PAGE_MIN_COVER = 0.30

#: Render resolution. 300 dpi is what Tesseract is tuned for; the cap keeps a
#: scan saved on an oversized page (5098x6596 px at 300 dpi, seen live) from
#: costing four times the memory and time for no extra detail.
DPI = 300
MAX_SIDE_PX = 3300

#: Words below this confidence (0-100) are noise: specks, icon edges.
MIN_CONF = 20

#: Per-page, per-pass ceiling. A pathological image must not hang a request.
TIMEOUT_S = 90

#: Tesseract processes alive at once, across every request in this process.
#: A batch masks up to MASK_MAX_CONCURRENT resumes at a time and each scan
#: fans out over pages and passes; uncapped, that is dozens of ~150 MB
#: processes on one container.
_SLOTS = threading.BoundedSemaphore(int(os.environ.get("OCR_MAX_PROCS") or min(4, os.cpu_count() or 1)))


def available() -> bool:
    return bool(TESSERACT) and cv2 is not None


def needs_ocr(page: fitz.Page) -> bool:
    if len(page.get_text().strip()) >= IMAGE_PAGE_MAX_CHARS:
        return False
    area = page.rect.get_area()
    if not area:
        return False
    cover = 0.0
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            cover = max(cover, r.get_area())
    return cover / area >= IMAGE_PAGE_MIN_COVER


# ── image ───────────────────────────────────────────────────────────────────

def _render(page: fitz.Page) -> tuple["np.ndarray", float]:
    zoom = min(DPI / 72, MAX_SIDE_PX / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    return img.copy(), zoom


def _invert_dark_panels(gray: "np.ndarray") -> "np.ndarray":
    """Segment solid dark regions (header bars, sidebars) and invert them, so
    their light text reads as dark-on-light like the rest of the page."""
    h, w = gray.shape
    dark = (gray < 110).astype(np.uint8)
    # Close the text holes inside a panel, then open away thin dark strokes
    # (ordinary text), leaving only filled areas.
    k = max(9, int(min(h, w) * 0.006) | 1)
    solid = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3 * k, 3 * k)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=4)
    out = gray.copy()
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        # Panels are bars, cards and pills: big, and filling their own box.
        # The box, not the component, is what gets inverted -- the light text
        # inside is exactly the part the component leaves out as holes.
        if area < 0.002 * h * w or bw * bh > 0.9 * h * w or area < 0.6 * bw * bh:
            continue
        box = gray[y:y + bh, x:x + bw]
        # Only a panel if it is mostly dark and something light sits inside it.
        if (box < 110).mean() < 0.55 or (box > 170).mean() < 0.01:
            continue
        out[y:y + bh, x:x + bw] = 255 - box
    return out


def _clean(gray: "np.ndarray") -> "np.ndarray":
    img = _invert_dark_panels(gray)
    # Background estimate: a closing wider than any stroke removes the text
    # and keeps tints, cards and gradients. Dividing it out flattens them.
    k = max(15, int(min(img.shape) * 0.012) | 1)
    bg = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    flat = cv2.divide(img, bg, scale=255)
    flat = cv2.normalize(flat, None, 0, 255, cv2.NORM_MINMAX)
    return cv2.medianBlur(flat, 3)


# ── tesseract ───────────────────────────────────────────────────────────────

def _tesseract(img: "np.ndarray", psm: int) -> list[tuple]:
    """(text, x0, y0, x1, y1, conf) per word, in image pixels."""
    ok, png = cv2.imencode(".png", img)
    if not ok:
        return []
    env = dict(os.environ)
    # Pages and passes already run in parallel; Tesseract's own OpenMP threads
    # on top of that only oversubscribe the CPU.
    env.setdefault("OMP_THREAD_LIMIT", "1")
    with _SLOTS:
        res = subprocess.run(
            [TESSERACT, "stdin", "stdout", "--oem", "1", "--psm", str(psm), "--dpi", str(DPI),
             "-l", LANG, "tsv"],
            input=png.tobytes(), capture_output=True, timeout=TIMEOUT_S, env=env, check=True)
    words = []
    for line in res.stdout.decode("utf-8", "replace").splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or f[0] != "5":
            continue
        text, conf = f[11].strip(), float(f[10])
        if not text or conf < MIN_CONF:
            continue
        x, y, w, h = map(int, f[6:10])
        words.append((text, x, y, x + w, y + h, conf))
    return words


def _overlap(a, b) -> float:
    """Intersection over the smaller box."""
    ix = min(a[3], b[3]) - max(a[1], b[1])
    iy = min(a[4], b[4]) - max(a[2], b[2])
    if ix <= 0 or iy <= 0:
        return 0.0
    small = min((a[3] - a[1]) * (a[4] - a[2]), (b[3] - b[1]) * (b[4] - b[2])) or 1
    return ix * iy / small


def _merge(passes: list[list[tuple]]) -> list[tuple]:
    """Every word any pass found; where passes overlap, the surer reading."""
    words: list[tuple] = []
    for p in passes:
        for w in p:
            clash = [i for i, v in enumerate(words) if _overlap(v, w) > 0.5]
            if not clash:
                words.append(w)
            elif all(w[5] > words[i][5] for i in clash):
                for i in sorted(clash, reverse=True):
                    del words[i]
                words.append(w)
    return words


def _lines(words: list[tuple]) -> list[list[tuple]]:
    """Group words into lines, left to right; a wide gap starts a new line
    (two columns side by side are two lines, not one)."""
    rows: list[dict] = []
    for w in sorted(words, key=lambda w: (w[2] + w[4]) / 2):
        cy, h = (w[2] + w[4]) / 2, max(1, w[4] - w[2])
        for r in rows:
            if abs(r["cy"] - cy) < 0.5 * max(h, r["h"]):
                r["w"].append(w)
                break
        else:
            rows.append({"cy": cy, "h": h, "w": [w]})
    out = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[1])
        cur = [ws[0]]
        for a, b in zip(ws, ws[1:]):
            if b[1] - a[3] > 3 * r["h"]:
                out.append(cur)
                cur = []
            cur.append(b)
        out.append(cur)
    return out


# ── snapping OCR readings to the known values ──────────────────────────────

def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _known(values) -> tuple[list[str], list[str]]:
    """The emails and phones among the known values. Anything else (a name)
    is ignored -- the name is no longer masked."""
    emails, phones = [], []
    for v in values or ():
        v = str(v).strip()
        if "@" in v:
            emails.append(v.lower())
        elif len(re.sub(r"\D", "", v)) >= 10:
            for part in re.split(r"[,/;|]", v):
                d = re.sub(r"\D", "", part)
                if len(d) >= 10:
                    phones.append(d[-10:])
    return emails, phones


def _snap(line: list[tuple], emails, phones) -> list[tuple]:
    """Rewrite readings that are a character or two off a known value."""
    line = list(line)
    for e in emails:                               # 1-3 adjacent words -> one email
        for a in range(len(line)):
            for b in range(a, min(len(line), a + 3)):
                joined = "".join(w[0] for w in line[a:b + 1]).lower().strip(".,;:()<>[]")
                if joined != e and _lev(joined, e) <= 2 and len(joined) >= len(e) - 2:
                    box = line[a:b + 1]
                    line[a:b + 1] = [(e, min(w[1] for w in box), min(w[2] for w in box),
                                      max(w[3] for w in box), max(w[4] for w in box), 100.0)]
                    break
            else:
                continue
            break
    for p in phones:                               # digit stream, one digit off
        stream, owner = "", []
        for i, w in enumerate(line):
            d = re.sub(r"\D", "", w[0])
            stream += d
            owner += [i] * len(d)
        for s in range(0, len(stream) - 9):
            win = stream[s:s + 10]
            if win != p and sum(x != y for x, y in zip(win, p)) <= 1:
                idx = sorted(set(owner[s:s + 10]))
                box = [line[i] for i in idx]
                merged = (p, min(w[1] for w in box), min(w[2] for w in box),
                          max(w[3] for w in box), max(w[4] for w in box), 100.0)
                line = [w for i, w in enumerate(line) if i not in idx[1:]]
                line[idx[0]] = merged
                break
    return line


# ── writing the invisible layer ─────────────────────────────────────────────

def _font_metrics() -> tuple[float, float]:
    """How far above and below the baseline MuPDF reports a helv glyph's box,
    per point of font size -- measured, so the layer lines up exactly."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 200), "Hg", fontsize=100, fontname="helv")
    x0, y0, x1, y1 = page.get_text("words")[0][:4]
    doc.close()
    return (200 - y0) / 100, (y1 - 200) / 100


_ASC, _DESC = _font_metrics()


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def _drop_native(lines: list[list[tuple]], page: fitz.Page, zoom: float) -> list[list[tuple]]:
    """Drop OCR words that sit on text the page already has.

    An image-only page is not always text-free: recruiters type a line over
    the scan ("Name ... Phone ... Email :"), and that real text is what the
    masking passes already match, exactly. Writing a second, invisible copy of
    it on the same spot left two overlapping words there -- measured on
    JA-26412 and JA-26296, the matcher then took each hit for a fragment of
    the other word and redacted neither, so the header went out untouched.
    """
    native = [fitz.Rect(w[:4]) for w in page.get_text("words")]
    if not native:
        return lines
    out = []
    for line in lines:
        kept = []
        for w in line:
            r = fitz.Rect(w[1] / zoom, w[2] / zoom, w[3] / zoom, w[4] / zoom)
            area = r.get_area() or 1
            if not any((r & n).get_area() > 0.3 * min(area, n.get_area() or 1) for n in native):
                kept.append(w)
        if kept:
            out.append(kept)
    return out


def _write_layer(page: fitz.Page, lines: list[list[tuple]], zoom: float) -> int:
    count = 0
    for line in lines:
        # One size and baseline for the whole line keeps MuPDF reading it as
        # one line, which is what the label walk in app/mask.py relies on.
        y0 = min(w[2] for w in line) / zoom
        y1 = max(w[4] for w in line) / zoom
        pad = (y1 - y0) * 0.08                    # cover anti-aliased edges
        y0, y1 = y0 - pad, y1 + pad
        size = (y1 - y0) / (_ASC + _DESC)
        base = y0 + _ASC * size
        for w in line:
            text = _latin1(w[0])
            x0, x1 = w[1] / zoom, w[3] / zoom
            natural = fitz.get_text_length(text, fontname="helv", fontsize=size)
            if natural <= 0 or x1 <= x0:
                continue
            origin = fitz.Point(x0, base)
            page.insert_text(origin, text, fontsize=size, fontname="helv", render_mode=3,
                             morph=(origin, fitz.Matrix((x1 - x0) / natural, 1)))
            count += 1
    return count


# ── entry points ────────────────────────────────────────────────────────────

def _read(gray: "np.ndarray") -> list[tuple]:
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(_tesseract, gray, 3)
        b = pool.submit(lambda: _tesseract(_clean(gray), 11))
        return _merge([a.result(), b.result()])


def add_text_layer(doc: fitz.Document, known=()) -> tuple[int, int]:
    """OCR every image-only page of `doc` in place.

    Returns (pages_needing_ocr, pages_given_a_layer). They differ when OCR
    is unavailable or a page could not be read; callers must not treat such
    a document as masked.
    """
    targets = [p for p in doc if p.rotation == 0 and needs_ocr(p)]
    rotated = [p for p in doc if p.rotation != 0 and needs_ocr(p)]
    needed = len(targets) + len(rotated)
    if not needed:
        return 0, 0
    if not available():
        logger.warning("%d image-only page(s) but OCR is unavailable (tesseract=%r, cv2=%s)",
                       needed, TESSERACT, cv2 is not None)
        return needed, 0
    emails, phones = _known(known)
    renders = [_render(p) for p in targets]
    done = 0
    with ThreadPoolExecutor(min(4, max(1, len(renders)))) as pool:
        results = list(pool.map(lambda r: _safe_read(r[0]), renders))
    for page, (_, zoom), words in zip(targets, renders, results):
        if not words:
            continue
        lines = [_snap(ln, emails, phones) for ln in _lines(words)]
        # Read is read, even if every word it found was already real text.
        _write_layer(page, _drop_native(lines, page, zoom), zoom)
        done += 1
    return needed, done


def _safe_read(gray):
    try:
        return _read(gray)
    except Exception as e:                        # timeout, bad image, missing lang
        logger.warning("OCR failed on a page: %s: %s", type(e).__name__, e)
        return []


def ensure_text_layer(pdf_bytes: bytes, known=()) -> tuple[bytes, int, int]:
    """add_text_layer() for bytes. Returns (pdf_bytes, needed, done); the
    input bytes come back untouched when no page needed OCR."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        needed, done = add_text_layer(doc, known)
        if not done:
            return pdf_bytes, needed, done
        return doc.tobytes(garbage=3, deflate=True), needed, done
    finally:
        doc.close()
