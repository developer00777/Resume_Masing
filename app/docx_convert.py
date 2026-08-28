"""DOCX -> PDF conversion via headless LibreOffice, so the existing
PyMuPDF-based masking pipeline (app/mask.py) can be reused unchanged for
Word-document resumes.

Real candidate resumes on this org are legacy .docx Attachments on the
related Contact (confirmed against live Salesforce data, not a
hypothetical format to support) -- this module exists specifically for
that, not speculative future-proofing.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path


class DocxConversionError(RuntimeError):
    pass


def docx_bytes_to_pdf_bytes(docx_bytes: bytes, timeout: int = 60) -> bytes:
    """Convert a .docx file (as bytes) to PDF bytes via `soffice --headless`.

    Each call gets its own LibreOffice user-profile directory
    (-env:UserInstallation) -- headless soffice instances that share a
    profile can lock each other out ("another instance is already
    running") when two conversions run concurrently, which /mask/batch
    processing multiple docx resumes in one request would otherwise hit.

    Raises DocxConversionError on any failure: soffice not installed,
    corrupt/unsupported document, or a timeout.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "resume.docx"
        src.write_bytes(docx_bytes)
        profile_dir = tmp_path / "lo_profile"

        try:
            result = subprocess.run(
                [
                    "soffice", "--headless", "--norestore",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to", "pdf", "--outdir", str(tmp_path), str(src),
                ],
                capture_output=True, timeout=timeout, check=False,
            )
        except FileNotFoundError as e:
            raise DocxConversionError(
                "LibreOffice (soffice) is not installed in this environment -- "
                "docx-to-pdf conversion is unavailable.") from e
        except subprocess.TimeoutExpired as e:
            raise DocxConversionError(
                f"docx-to-pdf conversion timed out after {timeout}s") from e

        out = tmp_path / "resume.pdf"
        if result.returncode != 0 or not out.exists():
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            raise DocxConversionError(
                f"docx-to-pdf conversion failed (exit {result.returncode}): {stderr}")
        return out.read_bytes()
