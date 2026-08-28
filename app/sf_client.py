"""Thin Salesforce wrapper (simple-salesforce). ALL creds from ENV or Postgres.

Auth, in priority order:
  A. Multi-client OAuth2 Client Credentials — headless, per-client Connected
     App, no username/password. Select a client per-request via client_key,
     which by convention is the org's own Salesforce Organization Id (Apex:
     UserInfo.getOrganizationId()) so callers never have to invent/coordinate
     a label. Entries come from two merged sources:
       - SF_CLIENTS_JSON (env var, static, requires a redeploy to change)
       - Postgres client_orgs table (dynamic, via register_client()/DATABASE_URL) —
         wins on client_key collision. Fully optional: behaves exactly like
         env-only mode when DATABASE_URL isn't set.
  B. Username + Password + Consumer Key/Secret (simple-salesforce's OAuth2 username-password flow).
  C. Username + Password + Security Token (simplest, single client).

Handles: resume fetch, watermark image fetch/upload, masked PDF upload,
dynamic client-org registration (register_client/remove_client).
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
from simple_salesforce.exceptions import SalesforceExpiredSession, SalesforceAuthenticationFailed

from app import crypto_util, db

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


class SalesforceAuthenticationError(RuntimeError):
    """Salesforce itself rejected the credentials (bad password/token/
    consumer key-secret) -- distinct from MissingCredentialsError (nothing
    configured at all). Wraps simple_salesforce's SalesforceAuthenticationFailed
    so every caller uses this module's own exception vocabulary instead of
    reaching into simple_salesforce's. Previously uncaught anywhere, which
    turned a bad password (env var OR a bad Settings-tab-saved override)
    into an unhandled 500 on /mask, /mask/batch, and /watermark/upload
    instead of a clean, actionable error message."""


class PostgresNotConfiguredError(RuntimeError):
    pass


class ClientAlreadyRegisteredError(RuntimeError):
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
# Two merged sources, keyed by "client_key" (by convention, the org's own
# Salesforce Organization Id). Each entry needs: client_id, client_secret,
# token_url, instance_url.
#
#   1. SF_CLIENTS_JSON (env var, static) -- example:
#      SF_CLIENTS_JSON={
#        "00D5f000000ABCDEAU": {
#          "client_id": "3MVG9...",
#          "client_secret": "64585...",
#          "token_url": "https://acme.my.salesforce.com/services/oauth2/token",
#          "instance_url": "https://acme.my.salesforce.com"
#        }
#      }
#   2. Postgres client_orgs table (dynamic, via register_client()) -- wins on
#      client_key collision. Absent entirely if DATABASE_URL isn't set.
_REQUIRED_CLIENT_FIELDS = ("client_id", "client_secret", "token_url", "instance_url")

# In-memory access-token cache: client_key -> {"access_token": ..., "expires_at": epoch_seconds}
_token_cache: dict[str, dict] = {}

# In-memory merged-registry cache. Not a pure TTL cache -- writes made through
# register_client()/remove_client() call invalidate_registry_cache() so the
# writer's own process sees the change on its very next read; the TTL is only
# a backstop for other processes/replicas to converge without needing pub/sub.
_registry_cache: dict[str, dict] | None = None
_registry_cache_at: float = 0.0
_REGISTRY_CACHE_TTL = 30  # seconds

# In-memory cache for the default (no client_key) connection's Postgres
# override -- same TTL/invalidate-on-write pattern as _registry_cache above.
# _default_override_loaded distinguishes "not loaded yet" from "loaded, and
# there's genuinely no override row" (both would otherwise look like None).
_default_override_cache: dict | None = None
_default_override_loaded: bool = False
_default_override_cache_at: float = 0.0
_DEFAULT_OVERRIDE_CACHE_TTL = 30  # seconds


def _load_env_registry() -> dict[str, dict]:
    raw = os.environ.get("SF_CLIENTS_JSON")
    if not raw:
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
    return registry


def _load_db_registry() -> dict[str, dict]:
    """Never raises -- a Postgres hiccup degrades to "env entries still work",
    not a full outage of every /mask call across every client org."""
    if not db.is_configured():
        return {}
    try:
        rows = db.list_entries()
        return {
            key: {
                "client_id": row["client_id"],
                "client_secret": crypto_util.decrypt_secret(row["encrypted_client_secret"]),
                "token_url": row["token_url"],
                "instance_url": row["instance_url"],
            }
            for key, row in rows.items()
        }
    except Exception:
        return {}


def _load_client_registry(force: bool = False) -> dict[str, dict]:
    global _registry_cache, _registry_cache_at
    now = time.time()
    if not force and _registry_cache is not None and (now - _registry_cache_at) < _REGISTRY_CACHE_TTL:
        return _registry_cache

    env_entries = _load_env_registry()  # raises MissingCredentialsError on malformed SF_CLIENTS_JSON
    db_entries = _load_db_registry()    # never raises; {} on any failure
    merged = {**env_entries, **db_entries}  # DB wins on key collision

    # Drop cached tokens for client_keys no longer in the merged registry (e.g.
    # a client was removed from SF_CLIENTS_JSON or deleted from Postgres) so
    # stale entries don't linger in memory for the life of the process.
    for stale_key in _token_cache.keys() - merged.keys():
        del _token_cache[stale_key]

    _registry_cache = merged
    _registry_cache_at = now
    return merged


def invalidate_registry_cache() -> None:
    """Called after every admin/self-service registry write so the next read
    in this process reflects it immediately, without waiting out the TTL."""
    global _registry_cache, _registry_cache_at
    _registry_cache = None
    _registry_cache_at = 0.0


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


def _load_default_override(force: bool = False) -> dict | None:
    """Postgres-backed override for the default (no client_key) connection --
    lets /candidate/MaskProfileIndex's Settings tab rotate the
    password/security-token/Connected-App creds without a Railway redeploy.

    Returns None if unset, or if Postgres is unreachable/unconfigured (never
    raises -- a DB hiccup degrades to 'fall back to the static env vars',
    not an outage of every no-client_key /mask call). SF_USERNAME is
    deliberately NOT part of this override; it stays an env var."""
    global _default_override_cache, _default_override_loaded, _default_override_cache_at
    now = time.time()
    if not force and _default_override_loaded and (now - _default_override_cache_at) < _DEFAULT_OVERRIDE_CACHE_TTL:
        return _default_override_cache

    override = None
    if db.is_configured():
        try:
            row = db.get_default_credentials()
            if row is not None:
                override = {
                    "password": crypto_util.decrypt_secret(row["encrypted_password"]),
                    "client_id": (crypto_util.decrypt_secret(row["encrypted_client_id"])
                                  if row["encrypted_client_id"] else None),
                    "client_secret": (crypto_util.decrypt_secret(row["encrypted_client_secret"])
                                       if row["encrypted_client_secret"] else None),
                    "login_host": row["login_host"],
                }
        except Exception:
            override = None

    _default_override_cache = override
    _default_override_loaded = True
    _default_override_cache_at = now
    return override


def invalidate_default_override_cache() -> None:
    """Called after POST /candidate/settings writes so the next connect() in
    this process reflects it immediately, without waiting out the TTL."""
    global _default_override_cache, _default_override_loaded, _default_override_cache_at
    _default_override_cache = None
    _default_override_loaded = False
    _default_override_cache_at = 0.0


def connect(client_key: str | None = None, force_refresh: bool = False) -> Salesforce:
    if client_key:
        return _connect_with_client_credentials(client_key, force_refresh=force_refresh)

    override = _load_default_override(force=force_refresh)
    try:
        if override is not None:
            username = os.environ.get("SF_USERNAME", "").strip()
            if not username:
                raise MissingCredentialsError(
                    "Salesforce credentials not configured. Missing: SF_USERNAME "
                    "(the Settings-tab-stored password/creds need SF_USERNAME set "
                    "in the environment alongside them).")
            domain = (override["login_host"] or "").strip() or "login"
            if override["client_id"] and override["client_secret"]:
                return Salesforce(username=username, password=override["password"],
                                  consumer_key=override["client_id"], consumer_secret=override["client_secret"],
                                  domain=domain)
            # No Connected App creds stored -- plain username/password login. The
            # Settings-tab form tells the caller to concatenate the security token
            # into the password themselves, so security_token="" here (simple-
            # salesforce just concatenates password + security_token internally;
            # "" is a no-op when the token is already part of the password).
            return Salesforce(username=username, password=override["password"],
                              security_token="", domain=domain)

        domain = os.environ.get("SF_DOMAIN", "login").strip() or "login"
        if os.environ.get("SF_CONSUMER_KEY"):
            c = _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
            return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                              consumer_key=c["SF_CONSUMER_KEY"], consumer_secret=c["SF_CONSUMER_SECRET"],
                              domain=domain)
        c = _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
        return Salesforce(username=c["SF_USERNAME"], password=c["SF_PASSWORD"],
                          security_token=c["SF_SECURITY_TOKEN"], domain=domain)
    except SalesforceAuthenticationFailed as e:
        source = "the Settings-tab-stored default connection" if override is not None else "the configured (env var) credentials"
        raise SalesforceAuthenticationError(
            f"Salesforce rejected {source}: {e}. If this was just changed via "
            "the Settings tab, clear it with DELETE /candidate/settings to "
            "fall back to the working env-var credentials.") from e


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
    if _load_default_override() is not None and os.environ.get("SF_USERNAME", "").strip():
        return  # Settings-tab override present, alongside the required SF_USERNAME
    if os.environ.get("SF_CONSUMER_KEY"):
        _require("SF_USERNAME", "SF_PASSWORD", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET")
    else:
        _require("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")


# ── Dynamic client-org registration (Postgres-backed) ───────────────────────

def registry_backend() -> str:
    """"db+env" if Postgres is provisioned, else "env-only" -- surfaced on
    /health so operators can confirm the DB is wired without hitting /admin."""
    return "db+env" if db.is_configured() else "env-only"


def register_client(client_key: str, client_id: str, client_secret: str,
                    token_url: str, instance_url: str,
                    allow_overwrite: bool = True) -> None:
    """Persist a client org's Connected App credentials to Postgres.

    allow_overwrite=False makes this create-only -- used by the self-service
    registration route (gated by MASK_API_KEY, reachable by the client
    themselves) so a resubmission can't clobber another org's already-working
    credentials, only fail loudly with ClientAlreadyRegisteredError. The
    admin route (gated by the separate, higher-privilege ADMIN_API_KEY) calls
    this with allow_overwrite=True to support credential rotation.
    """
    client_key = _safe_id(client_key, "client_key")
    missing = [n for n, v in (
        ("client_id", client_id), ("client_secret", client_secret),
        ("token_url", token_url), ("instance_url", instance_url),
    ) if not v]
    if missing:
        raise MissingCredentialsError("Missing required field(s): " + ", ".join(missing))
    if not db.is_configured():
        raise PostgresNotConfiguredError(
            "Postgres isn't provisioned on this deployment (DATABASE_URL unset) "
            "-- dynamic client registration is unavailable. Use SF_CLIENTS_JSON instead.")

    if not allow_overwrite and db.get_entry(client_key) is not None:
        raise ClientAlreadyRegisteredError(
            f"client_key '{client_key}' is already registered.")

    encrypted = crypto_util.encrypt_secret(client_secret)
    db.upsert_entry(client_key, client_id, encrypted, token_url, instance_url)
    invalidate_registry_cache()


def register_default_credentials(password: str, client_id: str | None, client_secret: str | None,
                                 login_host: str) -> None:
    """Persist the default (no client_key) connection's password/security-token
    (caller concatenates the token into password themselves) and optional
    Connected App Consumer Key/Secret to Postgres, encrypted.

    Used by POST /candidate/settings so the /candidate/MaskProfileIndex
    "User Settings" tab can rotate these without a Railway redeploy.
    SF_USERNAME is NOT stored here -- it stays an env var; this only
    overrides password/token/consumer-key/secret/domain. Always overwrites
    (single sentinel row) -- there's nothing to "already be registered"
    the way there is for register_client()'s per-org rows.
    """
    if not password or not login_host:
        raise MissingCredentialsError("Missing required field(s): password, login_host")
    if bool(client_id) != bool(client_secret):
        raise MissingCredentialsError(
            "client_key and client_secret must be provided together, or both left blank.")
    if not db.is_configured():
        raise PostgresNotConfiguredError(
            "Postgres isn't provisioned on this deployment (DATABASE_URL unset) "
            "-- dynamic credential rotation is unavailable. Set SF_PASSWORD / "
            "SF_SECURITY_TOKEN (and SF_CONSUMER_KEY / SF_CONSUMER_SECRET if "
            "applicable) via Railway env instead.")

    encrypted_password = crypto_util.encrypt_secret(password)
    encrypted_client_id = crypto_util.encrypt_secret(client_id) if client_id else None
    encrypted_client_secret = crypto_util.encrypt_secret(client_secret) if client_secret else None
    db.upsert_default_credentials(encrypted_password, encrypted_client_id, encrypted_client_secret, login_host)
    invalidate_default_override_cache()


def remove_client(client_key: str) -> bool:
    """Delete a Postgres-backed client org entry. Returns whether one existed.
    Does not touch SF_CLIENTS_JSON entries -- those aren't DB-managed."""
    if not db.is_configured():
        raise PostgresNotConfiguredError(
            "Postgres isn't provisioned on this deployment (DATABASE_URL unset).")
    deleted = db.delete_entry(client_key)
    invalidate_registry_cache()
    return deleted


def default_credentials_status() -> dict:
    """Non-destructive status check for the default-connection override --
    NEVER includes the password or Consumer Secret. Backs GET
    /candidate/settings, added because there was previously no way to
    confirm whether a Settings-tab save actually persisted short of the
    destructive DELETE (which reports "existed" only by removing it)."""
    override = _load_default_override(force=True)
    if override is None:
        return {"configured": False, "login_host": None, "has_client_credentials": False}
    return {
        "configured": True,
        "login_host": override["login_host"],
        "has_client_credentials": bool(override["client_id"] and override["client_secret"]),
    }


def remove_default_credentials() -> bool:
    """Clear the default-connection override (POST /candidate/settings)
    so connect() falls back to the static SF_* env vars again. Returns
    whether an override existed. The DB override always wins over env vars
    while present, so this is the only way back to env-var-only mode short
    of hand-deleting the row -- needed the moment a bad Settings-tab save
    (wrong password, garbage Consumer Key/Secret) breaks the live
    connection until it's cleared."""
    if not db.is_configured():
        raise PostgresNotConfiguredError(
            "Postgres isn't provisioned on this deployment (DATABASE_URL unset).")
    deleted = db.delete_default_credentials()
    invalidate_default_override_cache()
    return deleted


def list_registered_clients() -> list[dict]:
    """Admin-listing view of the merged registry, secrets never included.
    [{client_key, client_id, token_url, instance_url, source: "db"|"env"}, ...]
    Additive -- does not change list_client_keys()'s existing list[str] shape."""
    env_entries = _load_env_registry()
    db_entries = {}
    if db.is_configured():
        try:
            db_entries = db.list_entries()
        except Exception:
            db_entries = {}

    out: list[dict] = []
    for key in sorted(set(env_entries) | set(db_entries)):
        source = "db" if key in db_entries else "env"
        entry = db_entries.get(key) or env_entries[key]
        out.append({
            "client_key": key,
            "client_id": entry["client_id"],
            "token_url": entry["token_url"],
            "instance_url": entry["instance_url"],
            "source": source,
        })
    return out


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