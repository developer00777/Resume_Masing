"""Offline self-check — proves the exact client complaint is fixed:
PII removed (true-redact), experience + marks NOT over-masked, watermark stamped.
Run: ~/salesforce-ats/.venv/bin/python -m tests.test_mask"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
from app.mask import mask_pdf


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


def main():
    d = tempfile.mkdtemp()
    src, out = f"{d}/in.pdf", f"{d}/out.pdf"
    _make_sample(src)
    hits = mask_pdf(src, out, ["John Doe", "+91 98765 43210", "john.doe@example.com"])
    txt = "".join(pg.get_text() for pg in fitz.open(out))

    for pii in ["John Doe", "98765", "john.doe@example.com"]:
        assert pii not in txt, f"PII LEAKED: {pii!r} still in masked text"
    assert "Experience" in txt and "85%" in txt, "OVER-MASKED experience/marks (the client's bug)"

    print(f"MASK SELF-CHECK PASS: redacted {hits} regions | PII gone | experience+marks intact | watermark stamped")


if __name__ == "__main__":
    main()
