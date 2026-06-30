"""Thin Salesforce wrapper (simple-salesforce). ALL creds from ENV.

Auth: Username/Password/Security-Token OR Connected App (OAuth2).

Handles: resume fetch, watermark image fetch/upload, masked PDF upload.
"""
from __future__ import annotations

import base64
import os

from simple_salesforce import Salesforce


class MissingCredentialsError(RuntimeError):
    pass


class ResumeNotFoundError(RuntimeError):
    pass


class WatermarkNotFoundError(RuntimeError):
    pass


def _require(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingCredentialsError(
            "Salesforce credentials not configured. Missing: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


def connect() -> Salesforce:
    domain = os.environ.get("SF_DOMAIN", "login").strip() or "login"
    if os.environ.get("SF_CONSUMER_KEY"):
        c = _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
        return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                          consumer_key=c["SF_CONSUMER_KEY"], consumer_secret=c["SF_CONSUMER_SECRET"],
                          domain=domain)
    c = _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
    return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                      security_token=c["SF_SECURITY_TOKEN"], domain=domain)


def creds_configured() -> bool:
    try:
        connect_kwargs_present()
        return True
    except MissingCredentialsError:
        return False


def connect_kwargs_present() -> None:
    if os.environ.get("SF_CONSUMER_KEY"):
        _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
    else:
        _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")


# ── Resume PDF ──────────────────────────────────────────────────────────────

def fetch_resume_pdf(job_applicant_id: str, sf: Salesforce | None = None) -> bytes:
    """Fetch the latest resume PDF linked to a Job Applicant record."""
    sf = sf or connect()
    links = sf.query(
        "SELECT ContentDocumentId FROM ContentDocumentLink "
        f"WHERE LinkedEntityId = '{job_applicant_id}' ORDER BY SystemModstamp DESC")
    doc_ids = [r["ContentDocumentId"] for r in links.get("records", [])]
    if doc_ids:
        in_list = ",".join(f"'{d}'" for d in doc_ids)
        versions = sf.query(
            "SELECT Id, VersionData, FileExtension, Title, ContentDocumentId, CreatedDate "
            f"FROM ContentVersion WHERE ContentDocumentId IN ({in_list}) AND IsLatest = true "
            "ORDER BY CreatedDate DESC")
        records = versions.get("records", [])
        pdfs = [r for r in records if (r.get("FileExtension") or "").lower() == "pdf"]
        chosen = pdfs or records
        if chosen:
            return _download_version_data(sf, chosen[0]["VersionData"])
    raise ResumeNotFoundError(f"No resume found for Job Applicant '{job_applicant_id}'.")


# ── Watermark Image ─────────────────────────────────────────────────────────

_WATERMARK_TITLE = "ResumeWatermark"


def fetch_watermark_png(account_id: str | None = None,
                        sf: Salesforce | None = None) -> bytes | None:
    """Fetch the client's custom watermark image from Salesforce.

    Resolution:
      1. File titled 'ResumeWatermark' linked to the Account (per-client).
      2. Global file titled 'ResumeWatermark' (org-wide fallback).
      3. Returns None if neither found → falls back to text watermark in mask.py.

    Self-serve upload: sales/admin uploads a PNG via POST /watermark/upload
    or directly in Salesforce as a File on the Account record titled 'ResumeWatermark'.
    """
    sf = sf or connect()
    title = _WATERMARK_TITLE

    # 1) Per-client: look for watermark file on the Account
    if account_id:
        safe_id = account_id.replace("'", "")
        links = sf.query(
            "SELECT ContentDocumentId FROM ContentDocumentLink "
            f"WHERE LinkedEntityId = '{safe_id}'")
        doc_ids = [r["ContentDocumentId"] for r in links.get("records", [])]
        if doc_ids:
            in_list = ",".join(f"'{d}'" for d in doc_ids)
            res = sf.query(
                "SELECT Id, VersionData FROM ContentVersion "
                f"WHERE ContentDocumentId IN ({in_list}) AND Title = '{title}' "
                "AND IsLatest = true ORDER BY CreatedDate DESC LIMIT 1")
            recs = res.get("records", [])
            if recs:
                return _download_version_data(sf, recs[0]["VersionData"])

    # 2) Global fallback: any ContentVersion with this title
    res = sf.query(
        "SELECT Id, VersionData FROM ContentVersion "
        f"WHERE Title = '{title}' AND IsLatest = true ORDER BY CreatedDate DESC LIMIT 1")
    recs = res.get("records", [])
    if recs:
        return _download_version_data(sf, recs[0]["VersionData"])

    return None


def upload_watermark_image(account_id: str, image_bytes: bytes,
                           filename: str = "watermark.png",
                           sf: Salesforce | None = None) -> str:
    """Upload a custom watermark image as a Salesforce File on the Account record.

    The file is titled 'ResumeWatermark' so fetch_watermark_png() finds it.
    Upload a new version to swap the logo. Returns the new ContentVersion Id.

    Args:
        account_id: Salesforce Account Id (the watermark is per-client).
        image_bytes: PNG/JPEG bytes of the watermark image.
        filename: Display filename (default watermark.png).
        sf: Optional Salesforce connection.

    Returns:
        ContentVersion Id of the uploaded file.
    """
    sf = sf or connect()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    created = sf.ContentVersion.create({
        "Title": _WATERMARK_TITLE,
        "PathOnClient": filename,
        "VersionData": encoded,
        "FirstPublishLocationId": account_id,
    })
    return created["id"]


# ── Masked PDF Upload ───────────────────────────────────────────────────────

def upload_masked_pdf(job_applicant_id: str, pdf_bytes: bytes, filename: str,
                      sf: Salesforce | None = None) -> str:
    """Create a new ContentVersion with the masked PDF, linked to the record."""
    sf = sf or connect()
    title = filename[:-4] if filename.lower().endswith(".pdf") else filename
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    created = sf.ContentVersion.create({
        "Title": title,
        "PathOnClient": filename,
        "VersionData": encoded,
        "FirstPublishLocationId": job_applicant_id,
    })
    return created["id"]


# ── Internal ────────────────────────────────────────────────────────────────

def _download_version_data(sf: Salesforce, version_data_path: str) -> bytes:
    url = f"https://{sf.sf_instance}{version_data_path}"
    resp = sf.session.get(url, headers={"Authorization": f"Bearer {sf.session_id}"})
    resp.raise_for_status()
    return resp.content