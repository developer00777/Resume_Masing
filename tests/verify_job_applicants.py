"""READ-ONLY verification of masking against named Job Applicants.

The synthetic corpus in tests/test_residual_sweep.py scores the logic. It
cannot tell you whether the logic fixes JA-26753. Only the real resume can,
because the ways masking actually fails in production are ways a PDF is
*built* -- an address kerned into three word boxes, a contact block in a
table cell, a .docx converted by LibreOffice -- and none of those survive
being retyped into a fixture.

So this runs the real pipeline on the real file and reports what is left:

    fetch the resume  ->  mask it exactly as /mask does  ->  re-read the
    masked PDF  ->  scan the text, the link annotations and the metadata for
    anything that is still a phone number or an email address

Nothing is written back. The masked PDF is never uploaded, the Job Applicant
is never touched, and no DML of any kind is issued.

It prints SHAPES, never values: "9999999999" for a number, "aaaaa.aaaaa@aaaaa.aaa"
for an address. A verification run that pastes candidate PII into a terminal
transcript or a CI log has created a second leak to fix.

Run, against the org:
    python tests/verify_job_applicants.py JA-26753 JA-26708 JA-26631
    python tests/verify_job_applicants.py a0X...  a0X...        (record Ids)

Credentials come from the environment or a local .env (which .gitignore
already excludes) -- the same ones tests/diagnose_contact_pii.py uses. Do not
pass them on the command line; a shell history is not a secret store.

Or offline, against files, with no credentials at all:
    python tests/verify_job_applicants.py ./resumes/JA-26753.docx ...

Any argument that is a path to an existing file is masked from disk instead
of being looked up. That mode cannot use the Contact record -- there isn't
one -- so it masks from the resume's own text alone, which is the HARDER of
the two cases and the one the second pass exists for. A file that comes back
clean here is clean in production too; the reverse is not guaranteed, since
production also has the Contact's name to work from.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_dotenv() -> None:
    """Minimal .env reader -- avoids adding a dependency for one diagnostic."""
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def shape(value: str) -> str:
    """Collapse every character class, so nothing identifying survives."""
    out = re.sub(r"[A-Z]", "A", str(value))
    out = re.sub(r"[a-z]", "a", out)
    return re.sub(r"\d", "9", out)


def _resolve(sf, ref: str) -> str | None:
    """A record Id for `ref`, which may already be one or may be a Name."""
    from app import sf_client
    if re.fullmatch(r"[a-zA-Z0-9]{15,18}", ref):
        return ref
    safe = ref.replace("'", r"\'")
    try:
        rows = sf.query(
            f"SELECT Id FROM SCSCHAMPS__Job_Applicant__c WHERE Name = '{safe}' LIMIT 1"
        )["records"]
    except Exception as e:
        print(f"  ! could not resolve {ref}: {type(e).__name__}")
        return None
    return rows[0]["Id"] if rows else None


def _residual_report(masked: bytes) -> dict[str, list[str]]:
    """Everything still identifiable in the masked PDF, as shapes."""
    import fitz
    from app import pii

    doc = fitz.open(stream=masked, filetype="pdf")
    found: dict[str, list[str]] = {"text": [], "links": [], "metadata": []}
    for page in doc:
        text = page.get_text()
        for start, end, kind in pii.scan_residual(text):
            found["text"].append(f"{kind}: {shape(text[start:end])}")
        for link in page.get_links():
            uri = link.get("uri") or ""
            if uri.lower().startswith(("mailto:", "tel:")) or pii.scan_residual(uri):
                found["links"].append(shape(uri))
    meta = doc.metadata or {}
    for key in ("title", "author", "subject", "keywords"):
        if meta.get(key):
            found["metadata"].append(f"{key}: {shape(meta[key])}")
    doc.close()
    return found


def verify(ref: str, sf) -> bool:
    """Mask one Job Applicant's resume in memory and report what survived."""
    from app import docx_convert, mask, sf_client
    from app.server import detect_pii

    print(f"\n=== {ref} ===")
    ja_id = _resolve(sf, ref)
    if not ja_id:
        print("  ! no such Job Applicant")
        return False

    try:
        resume_bytes, ext = sf_client.fetch_resume_pdf(ja_id, sf=sf)
    except Exception as e:
        print(f"  ! no resume: {type(e).__name__}: {e}")
        return False
    print(f"  resume: {ext}, {len(resume_bytes)} bytes")

    if ext != "pdf":
        try:
            resume_bytes = docx_convert.docx_bytes_to_pdf_bytes(resume_bytes)
        except Exception as e:
            print(f"  ! {ext} -> pdf conversion failed: {e}")
            return False

    # mask_strings exactly as _mask_one builds them: Contact fields merged
    # with the resume's own text scan.
    contact_strings: list[str] = []
    contact_id = sf_client.resolve_contact_id(ja_id, sf=sf)
    if contact_id:
        contact_strings = sf_client.fetch_contact_pii_strings(contact_id, sf=sf)
    seen: set[str] = set()
    mask_strings: list[str] = []
    for s in contact_strings + detect_pii(resume_bytes):
        if s and s not in seen:
            seen.add(s)
            mask_strings.append(s)
    print(f"  mask_strings: {len(mask_strings)} "
          f"({len(contact_strings)} from the Contact record)")

    # Both passes, then the first pass alone, so the report says what the
    # second one is actually carrying on this specific resume.
    both, hits = mask.mask_pdf_bytes(resume_bytes, mask_strings, watermark_text="")
    first, first_hits = mask.mask_pdf_bytes(resume_bytes, mask_strings,
                                            watermark_text="", residual_sweep=False)
    print(f"  redacted regions: {first_hits} (first pass) -> {hits} (both)")

    before = _residual_report(first)
    after = _residual_report(both)
    for where in ("text", "links", "metadata"):
        closed = len(before[where]) - len(after[where])
        if after[where]:
            print(f"  LEAK in {where}: {after[where]}")
        elif closed:
            print(f"  {where}: {closed} closed by the second pass, none left")
        else:
            print(f"  {where}: clean")
    return not any(after.values())


def verify_file(path: Path) -> bool:
    """Mask a resume from disk and report what survived. No Salesforce."""
    from app import docx_convert, mask
    from app.server import detect_pii

    print(f"\n=== {path.name} ===")
    data = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    print(f"  resume: {ext}, {len(data)} bytes")
    if ext != "pdf":
        try:
            data = docx_convert.docx_bytes_to_pdf_bytes(data)
        except Exception as e:
            print(f"  ! {ext} -> pdf conversion failed (needs LibreOffice): {e}")
            return False

    # No Contact record off disk, so this is the resume-text-only path.
    mask_strings = detect_pii(data)
    print(f"  mask_strings: {len(mask_strings)} (resume text only, no Contact record)")

    both, hits = mask.mask_pdf_bytes(data, mask_strings, watermark_text="")
    first, first_hits = mask.mask_pdf_bytes(data, mask_strings, watermark_text="",
                                            residual_sweep=False)
    print(f"  redacted regions: {first_hits} (first pass) -> {hits} (both)")

    before, after = _residual_report(first), _residual_report(both)
    for where in ("text", "links", "metadata"):
        closed = len(before[where]) - len(after[where])
        if after[where]:
            print(f"  LEAK in {where}: {after[where]}")
        elif closed:
            print(f"  {where}: {closed} closed by the second pass, none left")
        else:
            print(f"  {where}: clean")
    return not any(after.values())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    load_dotenv()

    files = [Path(a) for a in argv if Path(a).is_file()]
    refs = [a for a in argv if not Path(a).is_file()]

    ok: list[bool] = [verify_file(p) for p in files]
    if refs:
        from app import sf_client
        if not sf_client.creds_configured():
            print("\nSalesforce credentials are not configured "
                  "(SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN).")
            print("SF_SECURITY_TOKEN is the 24-character token from "
                  "Setup > Reset My Security Token -- NOT the short "
                  "verification code Salesforce emails at UI login.")
            print("To check these without any credentials, download the "
                  "resumes and pass their paths instead.")
            return 2
        sf = sf_client.connect()
        ok += [verify(ref, sf) for ref in refs]

    print(f"\n{sum(ok)}/{len(ok)} clean")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
