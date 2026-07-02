"""Thin Salesforce wrapper (simple-salesforce). ALL creds from ENV.

Auth, in priority order:
  A. Multi-client OAuth2 Client Credentials (SF_CLIENTS_JSON) — headless, per-client
     Connected App, no username/password. Select a client per-request via client_key.
  B. Username + Password + Consumer Key/Secret (simple-salesforce's OAuth2 username-password flow).
  C. Username + Password + Security Token (simplest, single client).

Handles: resume fetch, watermark image fetch/upload, masked PDF upload.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Callable, TypeVar

import requests
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceExpiredSession

T = TypeVar("T")

# Salesforce record Ids are always 15 (case-sensitive) or 18 (case-insensitive)
# alphanumeric chars. job_applicant_id/account_id arrive from the public /mask
# HTTP body and get f-string-interpolated straight into SOQL below, so this is
# the SOQL-injection guard (e.g. a value like "x' OR Id != '" would otherwise
# widen the WHERE clause) — validate at the boundary, not deep in the query.
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")


class MissingCredentialsError(RuntimeError):
    pass


class ResumeNotFoundError(RuntimeError):
    pass


class WatermarkNotFoundError(RuntimeError):
    pass


class InvalidIdError(RuntimeError):
    pass


class UnknownClientError(RuntimeError):
    pass


def _safe_id(value: str | None, field: str) -> str:
    if not value or not _SF_ID_RE.match(value):
        raise InvalidIdError(f"Invalid Salesforce Id for {field}: {value!r}")
    return value


def _require(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingCredentialsError(
            "Salesforce credentials not configured. Missing: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


# ── Multi-client Connected App registry (OAuth2 Client Credentials) ─────────
#
# SF_CLIENTS_JSON is a JSON object keyed by an arbitrary "client_key" your
# Salesforce button / caller passes in. Each entry needs:
#   client_id, client_secret, token_url, instance_url
#
# Example:
#   SF_CLIENTS_JSON={
#     "acme": {
#       "client_id": "3MVG9...",
#       "client_secret": "64585...",
#       "token_url": "https://acme.my.salesforce.com/services/oauth2/token",
#       "instance_url": "https://acme.my.salesforce.com"
#     },
#     "beta_corp": { ... }
#   }
_REQUIRED_CLIENT_FIELDS = ("client_id", "client_secret", "token_url", "instance_url")

# In-memory access-token cache: client_key -> {"access_token": ..., "expires_at": epoch_seconds}
_token_cache: dict[str, dict] = {}


def _load_client_registry() -> dict[str, dict]:
    raw = os.environ.get("SF_CLIENTS_JSON")
    if not raw:
        _token_cache.clear()
        return {}
    try:
        registry = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MissingCredentialsError(f"SF_CLIENTS_JSON is not valid JSON: {e}") from e
    if not isinstance(registry, dict):
        raise MissingCredentialsError("SF_CLIENTS_JSON must be a JSON object keyed by client_key.")
    for key, entry in registry.items():
        if not isinstance(entry, dict) or any(f not in entry for f in _REQUIRED_CLIENT_FIELDS):
            raise MissingCredentialsError(
                f"SF_CLIENTS_JSON['{key}'] must include: " + ", ".join(_REQUIRED_CLIENT_FIELDS))
    # Drop cached tokens for client_keys no longer in the registry (e.g. a client
    # was removed or renamed in SF_CLIENTS_JSON) so stale entries don't linger
    # in memory for the life of the process.
    for stale_key in _token_cache.keys() - registry.keys():
        del _token_cache[stale_key]
    return registry


def list_client_keys() -> list[str]:
    return sorted(_load_client_registry().keys())


def _get_client_entry(client_key: str) -> dict:
    registry = _load_client_registry()
    entry = registry.get(client_key)
    if entry is None:
        raise UnknownClientError(
            f"Unknown client_key '{client_key}'. Configured: {sorted(registry.keys()) or 'none'}")
    return entry


def _fetch_client_credentials_token(entry: dict) -> tuple[str, str]:
    """POST the OAuth2 Client Credentials grant. Returns (access_token, instance_url)."""
    try:
        resp = requests.post(
            entry["token_url"],
            data={
                "grant_type": "client_credentials",
                "client_id": entry["client_id"],
                "client_secret": entry["client_secret"],
            },
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise MissingCredentialsError(f"Salesforce token request failed: {e}") from e
    if resp.status_code != 200:
        raise MissingCredentialsError(
            f"Salesforce token request failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    access_token = payload["access_token"]
    # Salesforce echoes the org's instance_url in the token response; prefer that,
    # fall back to the configured one if absent.
    instance_url = payload.get("instance_url") or entry["instance_url"]
    return access_token, instance_url


def get_client_credentials_token(client_key: str, force_refresh: bool = False) -> tuple[str, str]:
    """Cached per-client access token. Refreshes ~60s before expiry (or if uncached).

    force_refresh=True skips the cache and fetches a new token unconditionally —
    used by the 401 retry path in with_session() when the cached token turned
    out to be dead (e.g. revoked, or the 15-minute conservative TTL undershot
    Salesforce's actual expiry).
    """
    entry = _get_client_entry(client_key)
    cached = _token_cache.get(client_key)
    now = time.time()
    if not force_refresh and cached and cached["expires_at"] > now:
        return cached["access_token"], cached["instance_url"]

    access_token, instance_url = _fetch_client_credentials_token(entry)
    # Client Credentials tokens don't carry a reliable expires_in from Salesforce;
    # cache conservatively and let a 401 downstream force a fresh fetch next call.
    _token_cache[client_key] = {
        "access_token": access_token,
        "instance_url": instance_url,
        "expires_at": now + 15 * 60,
    }
    return access_token, instance_url


def _connect_with_client_credentials(client_key: str, force_refresh: bool = False) -> Salesforce:
    access_token, instance_url = get_client_credentials_token(client_key, force_refresh=force_refresh)
    host = instance_url.replace("https://", "").replace("http://", "").rstrip("/")
    return Salesforce(instance_url=instance_url, session_id=access_token, domain=host)


def connect(client_key: str | None = None, force_refresh: bool = False) -> Salesforce:
    if client_key:
        return _connect_with_client_credentials(client_key, force_refresh=force_refresh)

    domain = os.environ.get("SF_DOMAIN", "login").strip() or "login"
    if os.environ.get("SF_CONSUMER_KEY"):
        c = _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
        return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                          consumer_key=c["SF_CONSUMER_KEY"], consumer_secret=c["SF_CONSUMER_SECRET"],
                          domain=domain)
    c = _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
    return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                      security_token=c["SF_SECURITY_TOKEN"], domain=domain)


class SessionExpiredError(RuntimeError):
    """Raised when a raw VersionData download (bypasses simple_salesforce's exception
    mapping) gets a 401. Caught by with_session() the same way as SalesforceExpiredSession."""


# Exceptions that mean "the session died, not that the request was invalid" --
# public so callers doing their own per-item error handling around with_session()
# (e.g. the /mask/batch loop) can distinguish "retry the whole operation" from
# "this one item legitimately failed."
SESSION_EXPIRED_ERRORS = (SalesforceExpiredSession, SessionExpiredError)


def with_session(fn: Callable[[Salesforce], T], client_key: str | None = None) -> T:
    """Run fn(sf) against a fresh/cached Salesforce session, retrying once on a
    dead session.

    The client-credentials token cache uses a conservative fixed TTL (Salesforce
    doesn't reliably return expires_in for that grant), so a cached token can go
    stale before its cache entry does. Rather than let that surface as an
    opaque failure on whatever call happened to hit it, this retries the whole
    operation once with a forced-fresh token. fn should be idempotent up to the
    point of failure -- true here since every sf_client operation either reads
    or does a single atomic Salesforce write.

    Raises whatever fn/connect raise; the retry only fires for a session that
    Salesforce itself has rejected as expired/invalid.
    """
    sf = connect(client_key=client_key)
    try:
        return fn(sf)
    except SESSION_EXPIRED_ERRORS:
        sf = connect(client_key=client_key, force_refresh=True)
        return fn(sf)


def creds_configured(client_key: str | None = None) -> bool:
    try:
        connect_kwargs_present(client_key=client_key)
        return True
    except (MissingCredentialsError, UnknownClientError):
        return False


def connect_kwargs_present(client_key: str | None = None) -> None:
    if client_key:
        _get_client_entry(client_key)  # raises UnknownClientError if absent
        return
    if _load_client_registry():
        return  # at least one multi-client entry configured, no client_key required to report "configured"
    if os.environ.get("SF_CONSUMER_KEY"):
        _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
    else:
        _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")


# ── Client (Account) resolution ─────────────────────────────────────────────

# RecruitChamp's managed package (SCSCHAMPS__) puts the client Account lookup
# directly on the Job Applicant join row (Job x Candidate) — the recruiter's
# "click Job Id -> joined requirements + eligible professionals" page already
# has this in view, so the mask button only needs to pass job_applicant_id and
# the service resolves the client itself for the per-client watermark.
_JOB_APPLICANT_OBJECT = "SCSCHAMPS__Job_Applicant__c"
_JOB_APPLICANT_ACCOUNT_FIELD = "SCSCHAMPS__Account__c"


def resolve_account_id(job_applicant_id: str, sf: Salesforce | None = None) -> str | None:
    """Look up the client Account Id from a Job Applicant record.

    Returns None (never raises) if the field is blank or the record can't be
    read — callers should fall back to the global/text watermark in that case,
    same as when no account_id is supplied at all.
    """
    job_applicant_id = _safe_id(job_applicant_id, "job_applicant_id")
    sf = sf or connect()
    try:
        res = sf.query(
            f"SELECT {_JOB_APPLICANT_ACCOUNT_FIELD} FROM {_JOB_APPLICANT_OBJECT} "
            f"WHERE Id = '{job_applicant_id}' LIMIT 1")
        recs = res.get("records", [])
        if not recs:
            return None
        return recs[0].get(_JOB_APPLICANT_ACCOUNT_FIELD) or None
    except Exception:
        return None


# ── Resume PDF ──────────────────────────────────────────────────────────────

def fetch_resume_pdf(job_applicant_id: str, sf: Salesforce | None = None) -> bytes:
    """Fetch the latest resume PDF linked to a Job Applicant record."""
    job_applicant_id = _safe_id(job_applicant_id, "job_applicant_id")
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
        safe_id = _safe_id(account_id, "account_id")
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
    # This is a raw requests call, not routed through simple_salesforce's REST
    # helpers, so a dead session surfaces as a plain 401 here instead of
    # SalesforceExpiredSession -- translate it so with_session()'s retry catches it.
    if resp.status_code == 401:
        raise SessionExpiredError(f"Session expired downloading {version_data_path}")
    resp.raise_for_status()
    return resp.content