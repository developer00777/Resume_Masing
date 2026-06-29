"""Thin Salesforce wrapper (simple-salesforce). ALL creds come from ENV — never hardcoded.

Two auth modes, selected by which env vars are present:
  • Username/Password/Security-Token  (SF_USERNAME + SF_PASSWORD + SF_SECURITY_TOKEN)  — simplest.
  • Connected App (OAuth2)             (SF_CONSUMER_KEY + SF_CONSUMER_SECRET + SF_USERNAME + SF_PASSWORD)
SF_DOMAIN selects the login host: "login" (prod, default) or "test" (sandbox), or a My Domain host.

The real creds + integration user arrive in a later session — this module is fully env-driven and
degrades gracefully: connect() raises a clear MissingCredentialsError if anything required is unset, so
the service can answer the caller with a precise "creds not configured" instead of a stack trace.

SECURITY: the resume bytes are handled in memory only; nothing here logs PII or PDF content.
"""
from __future__ import annotations

import base64
import os

from simple_salesforce import Salesforce


class MissingCredentialsError(RuntimeError):
    """Raised when required Salesforce env vars are not configured."""


class ResumeNotFoundError(RuntimeError):
    """Raised when no resume ContentVersion can be located for a Job Applicant record."""


def _require(*names: str) -> dict[str, str]:
    """Return a dict of the named env vars, or raise MissingCredentialsError listing what's missing."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingCredentialsError(
            "Salesforce credentials not configured. Missing env var(s): "
            + ", ".join(missing)
            + ". See .env.example."
        )
    return {n: os.environ[n] for n in names}


def connect() -> Salesforce:
    """Build a simple-salesforce client from env. Connected App if SF_CONSUMER_KEY is set, else
    username/password/token. Raises MissingCredentialsError with a clear message if creds are absent."""
    domain = os.environ.get("SF_DOMAIN", "login").strip() or "login"

    if os.environ.get("SF_CONSUMER_KEY"):
        c = _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
        return Salesforce(
            username=c["SF_USERNAME"],
            password=c["SF_PASSWORD"],
            consumer_key=c["SF_CONSUMER_KEY"],
            consumer_secret=c["SF_CONSUMER_SECRET"],
            domain=domain,
        )

    c = _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
    return Salesforce(
        username=c["SF_USERNAME"],
        password=c["SF_PASSWORD"],
        security_token=c["SF_SECURITY_TOKEN"],
        domain=domain,
    )


def creds_configured() -> bool:
    """Cheap, side-effect-free check used by /health and pre-flight — does NOT hit the network."""
    try:
        connect_kwargs_present()
        return True
    except MissingCredentialsError:
        return False


def connect_kwargs_present() -> None:
    """Validate the env without constructing a client / making a network call. Raises if incomplete."""
    if os.environ.get("SF_CONSUMER_KEY"):
        _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
    else:
        _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")


def fetch_resume_pdf(job_applicant_id: str, sf: Salesforce | None = None) -> bytes:
    """SOQL the latest resume ContentVersion linked to the Job Applicant record and return its bytes.

    Resolution path (standard Salesforce Files model):
      Job Applicant record  --ContentDocumentLink-->  ContentDocument  --(latest)-->  ContentVersion(PDF)

    Returns the raw PDF bytes. Raises ResumeNotFoundError if the record has no PDF attachment.
    """
    sf = sf or connect()

    # 1) Files (ContentDocument) linked to the record, newest first.
    links = sf.query(
        "SELECT ContentDocumentId FROM ContentDocumentLink "
        f"WHERE LinkedEntityId = '{job_applicant_id}' "
        "ORDER BY SystemModstamp DESC"
    )
    doc_ids = [r["ContentDocumentId"] for r in links.get("records", [])]

    if doc_ids:
        in_list = ",".join(f"'{d}'" for d in doc_ids)
        versions = sf.query(
            "SELECT Id, VersionData, FileExtension, Title, ContentDocumentId, CreatedDate "
            f"FROM ContentVersion WHERE ContentDocumentId IN ({in_list}) AND IsLatest = true "
            "ORDER BY CreatedDate DESC"
        )
        records = versions.get("records", [])
        # Prefer a PDF; fall back to the most recent file if none is explicitly a PDF.
        pdfs = [r for r in records if (r.get("FileExtension") or "").lower() == "pdf"]
        chosen = (pdfs or records)
        if chosen:
            return _download_version_data(sf, chosen[0]["VersionData"])

    raise ResumeNotFoundError(
        f"No resume file (ContentVersion) found for Job Applicant '{job_applicant_id}'."
    )


def fetch_watermark_png(
    account_id: str | None = None,
    title: str | None = None,
    sf: Salesforce | None = None,
) -> bytes | None:
    """Fetch the client-specific watermark logo from Salesforce — no redeploy needed.

    Resolution order:
      1. File titled `ResumeWatermark` linked to `account_id` (per-client logo).
      2. Global File titled `ResumeWatermark` (org-wide fallback).
    Override title via WATERMARK_TITLE env. Returns PNG bytes, or None (falls back to faint text).

    Self-serve upload: sales/admin uploads the logo PNG as a Salesforce File on the Account record
    (or org-wide). Title must be exactly `ResumeWatermark`. Upload a new version to swap the logo.
    """
    title = title or os.environ.get("WATERMARK_TITLE", "ResumeWatermark")
    sf = sf or connect()
    safe_title = title.replace("'", "")

    # 1) Per-client: File linked to the Account record.
    if account_id:
        safe_id = account_id.replace("'", "")
        links = sf.query(
            "SELECT ContentDocumentId FROM ContentDocumentLink "
            f"WHERE LinkedEntityId = '{safe_id}'"
        )
        doc_ids = [r["ContentDocumentId"] for r in links.get("records", [])]
        if doc_ids:
            in_list = ",".join(f"'{d}'" for d in doc_ids)
            res = sf.query(
                "SELECT Id, VersionData FROM ContentVersion "
                f"WHERE ContentDocumentId IN ({in_list}) AND Title = '{safe_title}' "
                "AND IsLatest = true ORDER BY CreatedDate DESC LIMIT 1"
            )
            recs = res.get("records", [])
            if recs:
                return _download_version_data(sf, recs[0]["VersionData"])

    # 2) Global fallback: any ContentVersion with this title.
    res = sf.query(
        "SELECT Id, VersionData FROM ContentVersion "
        f"WHERE Title = '{safe_title}' AND IsLatest = true ORDER BY CreatedDate DESC LIMIT 1"
    )
    recs = res.get("records", [])
    if not recs:
        return None
    return _download_version_data(sf, recs[0]["VersionData"])


def _download_version_data(sf: Salesforce, version_data_path: str) -> bytes:
    """VersionData is a relative REST path (/services/data/vXX/sobjects/ContentVersion/<id>/VersionData).
    Stream it through the authenticated simple-salesforce session and return the raw bytes."""
    url = f"https://{sf.sf_instance}{version_data_path}"
    resp = sf.session.get(url, headers={"Authorization": f"Bearer {sf.session_id}"})
    resp.raise_for_status()
    return resp.content


def upload_masked_pdf(
    job_applicant_id: str,
    pdf_bytes: bytes,
    filename: str,
    sf: Salesforce | None = None,
) -> str:
    """Create a NEW ContentVersion holding the masked PDF and link it to the Job Applicant record.
    Returns the new ContentVersion Id. The original resume is left untouched (a new file is added)."""
    sf = sf or connect()

    title = filename[:-4] if filename.lower().endswith(".pdf") else filename
    encoded = base64.b64encode(pdf_bytes).decode("ascii")

    created = sf.ContentVersion.create(
        {
            "Title": title,
            "PathOnClient": filename,
            "VersionData": encoded,
            "FirstPublishLocationId": job_applicant_id,  # auto-links the file to the record
        }
    )
    return created["id"]
