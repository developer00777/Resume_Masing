"""Multi-client OAuth2 Client Credentials registry — network AND Postgres fully mocked.

Covers:
  * SF_CLIENTS_JSON parsing (missing, malformed, missing required fields)
  * token exchange (client_credentials grant) + in-memory caching per client_key
  * unknown client_key -> UnknownClientError
  * connect(client_key=...) builds a Salesforce session from the token, not from
    username/password
  * stale token_cache eviction when a client_key is removed from SF_CLIENTS_JSON
  * force_refresh bypasses the cache
  * with_session() retries once on a dead session, not on other errors

DB-backed registry behavior (register_client/remove_client/merge-with-DB) is
covered separately in tests/test_client_registry_db.py -- this file asserts
the env-only path is byte-for-byte unchanged now that Postgres is a possible
second source (DATABASE_URL is explicitly unset here, so db.is_configured()
is always False and _load_db_registry() always returns {}).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import sf_client

ACME_ENTRY = {
    "client_id": "acme-id",
    "client_secret": "acme-secret",
    "token_url": "https://acme.my.salesforce.com/services/oauth2/token",
    "instance_url": "https://acme.my.salesforce.com",
}


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    sf_client._token_cache.clear()
    sf_client.invalidate_registry_cache()
    monkeypatch.delenv("SF_CLIENTS_JSON", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield
    sf_client._token_cache.clear()
    sf_client.invalidate_registry_cache()


def test_list_client_keys_empty_without_env():
    assert sf_client.list_client_keys() == []


def test_registry_rejects_malformed_json(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", "{not-json")
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client._load_client_registry()


def test_registry_rejects_missing_fields(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": {"client_id": "x"}}))
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client._load_client_registry()


def test_unknown_client_key_raises(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    with pytest.raises(sf_client.UnknownClientError):
        sf_client._get_client_entry("nonexistent")


def test_get_client_credentials_token_fetches_and_caches(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "tok-123", "instance_url": "https://acme.my.salesforce.com"}

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        return FakeResponse()

    monkeypatch.setattr(sf_client.requests, "post", fake_post)

    token, instance_url = sf_client.get_client_credentials_token("acme")
    assert token == "tok-123"
    assert instance_url == "https://acme.my.salesforce.com"
    assert len(calls) == 1
    assert calls[0][1]["grant_type"] == "client_credentials"
    assert calls[0][1]["client_id"] == "acme-id"
    assert calls[0][1]["client_secret"] == "acme-secret"

    # Second call within the cache window must NOT hit the network again.
    token2, _ = sf_client.get_client_credentials_token("acme")
    assert token2 == "tok-123"
    assert len(calls) == 1


def test_get_client_credentials_token_raises_on_error_response(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))

    class FakeErrorResponse:
        status_code = 400
        text = "invalid_client_id"

        def json(self):
            return {}

    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: FakeErrorResponse())

    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.get_client_credentials_token("acme")


def test_get_client_credentials_token_raises_clean_error_on_connection_failure(monkeypatch):
    """A DNS/connection failure talking to token_url must surface as MissingCredentialsError,
    not an unhandled requests.exceptions.ConnectionError (which would 500 the /mask endpoint)."""
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))

    def fake_post(*a, **k):
        raise sf_client.requests.exceptions.ConnectionError("Failed to resolve host")

    monkeypatch.setattr(sf_client.requests, "post", fake_post)

    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.get_client_credentials_token("acme")


def test_connect_with_client_key_uses_token_not_password(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    monkeypatch.setattr(
        sf_client, "get_client_credentials_token",
        lambda client_key, force_refresh=False: ("tok-abc", "https://acme.my.salesforce.com"))

    captured = {}

    class FakeSalesforce:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sf_client, "Salesforce", FakeSalesforce)

    sf_client.connect(client_key="acme")
    assert captured["session_id"] == "tok-abc"
    assert captured["instance_url"] == "https://acme.my.salesforce.com"
    assert "username" not in captured
    assert "password" not in captured


def test_creds_configured_true_with_registry_and_no_client_key(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    assert sf_client.creds_configured() is True


def test_creds_configured_false_for_unknown_client_key(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    assert sf_client.creds_configured(client_key="nonexistent") is False


BETA_ENTRY = {
    "client_id": "beta-id",
    "client_secret": "beta-secret",
    "token_url": "https://beta.my.salesforce.com/services/oauth2/token",
    "instance_url": "https://beta.my.salesforce.com",
}


def test_token_cache_evicts_client_keys_removed_from_registry(monkeypatch):
    sf_client._token_cache["acme"] = {"access_token": "t", "instance_url": "x", "expires_at": 9e18}
    sf_client._token_cache["beta"] = {"access_token": "t", "instance_url": "x", "expires_at": 9e18}

    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    sf_client._load_client_registry()

    assert "acme" in sf_client._token_cache
    assert "beta" not in sf_client._token_cache, "beta was dropped from SF_CLIENTS_JSON, its cached token should be evicted"


def test_token_cache_cleared_when_sf_clients_json_unset(monkeypatch):
    sf_client._token_cache["acme"] = {"access_token": "t", "instance_url": "x", "expires_at": 9e18}
    monkeypatch.delenv("SF_CLIENTS_JSON", raising=False)

    sf_client._load_client_registry()

    assert sf_client._token_cache == {}


def test_get_client_credentials_token_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    sf_client._token_cache["acme"] = {
        "access_token": "stale-token", "instance_url": "https://acme.my.salesforce.com",
        "expires_at": time.time() + 999,  # not expired by our cache's own clock
    }

    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "fresh-token", "instance_url": "https://acme.my.salesforce.com"}

    def fake_post(url, data=None, timeout=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(sf_client.requests, "post", fake_post)

    token, _ = sf_client.get_client_credentials_token("acme", force_refresh=True)
    assert token == "fresh-token"
    assert len(calls) == 1, "force_refresh must skip the cache and hit the network"


def test_with_session_retries_once_on_expired_session(monkeypatch):
    from simple_salesforce.exceptions import SalesforceExpiredSession

    connect_calls = []

    def fake_connect(client_key=None, force_refresh=False):
        connect_calls.append(force_refresh)
        return f"sf-session-{'fresh' if force_refresh else 'cached'}"

    monkeypatch.setattr(sf_client, "connect", fake_connect)

    attempts = []

    def fn(sf):
        attempts.append(sf)
        if len(attempts) == 1:
            raise SalesforceExpiredSession(url="x", status=401, resource_name="ContentVersion", content=b"")
        return "ok:" + sf

    result = sf_client.with_session(fn, client_key="acme")
    assert result == "ok:sf-session-fresh"
    assert connect_calls == [False, True]


def test_with_session_does_not_retry_on_other_errors(monkeypatch):
    monkeypatch.setattr(sf_client, "connect", lambda client_key=None, force_refresh=False: object())

    calls = []

    def fn(sf):
        calls.append(1)
        raise sf_client.ResumeNotFoundError("nope")

    with pytest.raises(sf_client.ResumeNotFoundError):
        sf_client.with_session(fn, client_key="acme")
    assert len(calls) == 1, "non-session errors must not trigger a retry"
