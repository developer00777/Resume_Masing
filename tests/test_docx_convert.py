"""Tests for app/docx_convert.py's error handling. subprocess.run is
mocked throughout -- no real LibreOffice install required to run these
(the real conversion path is exercised separately, in Docker, against a
real .docx pulled from Salesforce -- see the manual verification notes in
the commit this file was introduced in)."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import docx_convert


def test_raises_clean_error_when_soffice_not_installed(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("soffice not found")

    monkeypatch.setattr(docx_convert.subprocess, "run", fake_run)
    with pytest.raises(docx_convert.DocxConversionError, match="not installed"):
        docx_convert.docx_bytes_to_pdf_bytes(b"fake docx bytes")


def test_raises_clean_error_on_timeout(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=60)

    monkeypatch.setattr(docx_convert.subprocess, "run", fake_run)
    with pytest.raises(docx_convert.DocxConversionError, match="timed out"):
        docx_convert.docx_bytes_to_pdf_bytes(b"fake docx bytes", timeout=60)


def test_raises_clean_error_on_nonzero_exit(monkeypatch):
    class FakeResult:
        returncode = 1
        stderr = b"soffice: unrecognized document format"

    monkeypatch.setattr(docx_convert.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(docx_convert.DocxConversionError, match="conversion failed"):
        docx_convert.docx_bytes_to_pdf_bytes(b"not actually a docx")


def test_returns_pdf_bytes_on_success(monkeypatch, tmp_path):
    """Simulate a successful soffice run by having the fake subprocess.run
    actually write the expected output file, matching what real soffice
    --convert-to pdf --outdir <dir> does."""
    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout, check):
        outdir = cmd[cmd.index("--outdir") + 1]
        (__import__("pathlib").Path(outdir) / "resume.pdf").write_bytes(b"%PDF-fake")
        return FakeResult()

    monkeypatch.setattr(docx_convert.subprocess, "run", fake_run)
    result = docx_convert.docx_bytes_to_pdf_bytes(b"fake docx bytes")
    assert result == b"%PDF-fake"


def test_each_call_gets_an_isolated_profile_dir(monkeypatch):
    """Regression guard: concurrent /mask/batch items each converting a
    docx must not share a LibreOffice profile dir (a well-known cause of
    'another instance is already running' failures under concurrency)."""
    seen_profile_args = []

    class FakeResult:
        returncode = 0
        stderr = b""

    def fake_run(cmd, capture_output, timeout, check):
        profile_arg = next(a for a in cmd if a.startswith("-env:UserInstallation="))
        seen_profile_args.append(profile_arg)
        outdir = cmd[cmd.index("--outdir") + 1]
        (__import__("pathlib").Path(outdir) / "resume.pdf").write_bytes(b"%PDF-fake")
        return FakeResult()

    monkeypatch.setattr(docx_convert.subprocess, "run", fake_run)
    docx_convert.docx_bytes_to_pdf_bytes(b"fake docx bytes 1")
    docx_convert.docx_bytes_to_pdf_bytes(b"fake docx bytes 2")
    assert seen_profile_args[0] != seen_profile_args[1]
