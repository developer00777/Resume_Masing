"""Benchmark masking of image-only resumes, end to end.

    python tests/ocr_benchmark.py DIR [--baseline] [--out OUTDIR]

DIR holds NAME.pdf + NAME.json ({"pii": [the Contact's name, phone, email]}).
Each resume goes through what /mask does -- OCR (app/ocr.py), detect_pii, the
masking passes -- and the masked page is then read AGAIN from scratch, raw
Tesseract with no preprocessing and no knowledge of the values, to count what
is still legible. That second read is the number that matters: it is what a
recruiter's eye, or anyone else's OCR, can still get off the page.

--baseline swaps app/ocr.py's reader for a single raw Tesseract pass with no
image preprocessing and no snapping, for comparison.

Prints counts and shapes only -- never a PII value. Needs tesseract, so run it
in the service image:
    docker run --rm -v <data>:/data <image> python tests/ocr_benchmark.py /data
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402
import numpy as np  # noqa: E402

from app import mask, ocr  # noqa: E402
from app.server import detect_pii  # noqa: E402

MOBILE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)


def _items(values):
    """The phones and emails to look for. Names are no longer masked."""
    emails, phones = ocr._known(values)
    return [("email", e) for e in emails] + [("phone", p) for p in phones]


def _lines_text(words):
    return [" ".join(w[0] for w in ln) for ln in ocr._lines(words)] if words else []


def _legible(kind, target, lines, fuzzy):
    if kind == "email":
        for ln in lines:
            for tok in ln.lower().split():
                tok = tok.strip(".,;:()<>[]|")
                if tok == target or (fuzzy and "@" in tok and ocr._lev(tok, target) <= 2):
                    return True
            if fuzzy and ocr._lev(ln.lower().replace(" ", ""), target) <= 2:
                return True
        return False
    for ln in lines:                               # phone
        d = re.sub(r"\D", "", ln)
        for s in range(0, len(d) - 9):
            if sum(a != b for a, b in zip(d[s:s + 10], target)) <= (1 if fuzzy else 0):
                return True
    return False


def _raw_read(page):
    gray, _ = ocr._render(page)
    return ocr._merge([ocr._tesseract(gray, 3), ocr._tesseract(gray, 11)]), gray


def run(path, out_dir):
    meta = json.load(open(path[:-4] + ".json"))
    known = list(meta.get("pii") or [])
    items = _items(known)
    src = open(path, "rb").read()

    t = time.time()
    pdf, needed, done = ocr.ensure_text_layer(src, known)
    ocr_s = time.time() - t
    layer = [ln for p in fitz.open(stream=pdf, filetype="pdf") for ln in p.get_text().splitlines()]
    in_layer = sum(_legible(k, v, layer, False) for k, v in items)

    # What the page itself shows before masking: the known values, plus any
    # mobile/email a raw read can see that the Contact record does not hold.
    before = fitz.open(stream=src, filetype="pdf")
    pre_lines, pre_gray = [], []
    for page in before:
        words, gray = _raw_read(page)
        pre_lines += _lines_text(words)
        pre_gray.append(gray)
    extra = set()
    for ln in pre_lines:
        for m in MOBILE.finditer(re.sub(r"(?<=\d)[\s\-.]+(?=\d)", "", ln)):
            extra.add(("phone", m.group()))
        for m in EMAIL.finditer(ln):
            extra.add(("email", m.group().lower()))
    truth = items + [x for x in extra if x not in items]
    visible = [x for x in truth if _legible(x[0], x[1], pre_lines, True)]

    mask_strings = list(dict.fromkeys(known + detect_pii(pdf)))
    masked, hits = mask.mask_pdf_bytes(pdf, mask_strings, watermark_text="")
    status = "refused" if needed and done < needed else "ok"

    post = fitz.open(stream=masked, filetype="pdf")
    post_lines, blanked = [], []
    for page, g0 in zip(post, pre_gray):
        words, g1 = _raw_read(page)
        post_lines += _lines_text(words)
        if g1.shape == g0.shape:
            blanked.append(float(((g0.astype(int) - g1.astype(int)) < -60).mean()))
    leaked = [x for x in visible if _legible(x[0], x[1], post_lines, True)]
    kept = sum(1 for ln in pre_lines for _ in ln.split())
    left = sum(1 for ln in post_lines for _ in ln.split())

    if out_dir:
        open(os.path.join(out_dir, os.path.basename(path)), "wb").write(masked)
    return dict(ja=meta.get("ja", Path(path).stem), pages=len(before), needed=needed, done=done,
                items=len(items), in_layer=in_layer, visible=len(visible), leaked=len(leaked),
                leaked_kinds=[k for k, _ in leaked], hits=hits, status=status, ocr_s=ocr_s,
                words_kept=left / max(1, kept), blanked=100 * float(np.mean(blanked or [0])))


def main():
    args = sys.argv[1:]
    baseline = "--baseline" in args
    out_dir = args[args.index("--out") + 1] if "--out" in args else None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    folder = [a for a in args if not a.startswith("--") and a != out_dir][0]
    if baseline:
        ocr._read = lambda gray: ocr._tesseract(gray, 3)
        ocr._snap = lambda line, *a: line
    if not ocr.available():
        sys.exit("tesseract / opencv not available")

    rows = [run(p, out_dir) for p in sorted(glob.glob(os.path.join(folder, "*.pdf")))]
    print(f"{'resume':10s} {'pages':>5s} {'ocr':>5s} {'layer':>7s} {'visible':>7s} {'leaked':>6s} "
          f"{'regions':>7s} {'status':>8s} {'ocr s':>6s} {'text kept':>9s} {'blanked':>7s}")
    for r in rows:
        print(f"{r['ja']:10s} {r['pages']:5d} {r['done']:>2d}/{r['needed']:<2d} "
              f"{r['in_layer']:>3d}/{r['items']:<3d} {r['visible']:7d} {r['leaked']:6d} {r['hits']:7d} "
              f"{r['status']:>8s} {r['ocr_s']:6.1f} {100 * r['words_kept']:8.0f}% {r['blanked']:6.1f}%"
              + (f"  leaked: {','.join(r['leaked_kinds'])}" if r["leaked"] else ""))
    tot = lambda k: sum(r[k] for r in rows)
    print(f"\n{'baseline (raw tesseract)' if baseline else 'app/ocr.py'}: "
          f"known values in OCR layer {tot('in_layer')}/{tot('items')} | "
          f"PII visible before masking {tot('visible')}, still legible after {tot('leaked')} | "
          f"refused {sum(r['status'] == 'refused' for r in rows)}/{len(rows)} | "
          f"OCR {tot('ocr_s') / max(1, tot('pages')):.1f}s/page")


if __name__ == "__main__":
    main()
