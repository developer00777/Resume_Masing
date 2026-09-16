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


def test_sf_domain_names_the_host_not_the_org():
    """SF_DOMAIN is a simple-salesforce host prefix, not a word for the org.

    simple-salesforce builds https://{domain}.salesforce.com out of it, so the
    deployed SF_DOMAIN="Live" resolved to nothing and failed every connection
    with a DNS error instead of an auth error. Production is "login" however
    it is spelled, and the same value arrives from the Settings tab as
    login_host.
    """
    for spelling in ("Live", "live", "PROD", "production", "login", "", None, "  "):
        assert sf_client._domain(spelling) == "login", spelling
    for spelling in ("test", "Sandbox"):
        assert sf_client._domain(spelling) == "test", spelling
    # A real My Domain host is passed through untouched.
    assert sf_client._domain("acme--uat.sandbox.my") == "acme--uat.sandbox.my"


# =========================================================================
# riding out a Salesforce blip, without riding out a real answer
# =========================================================================

TRANSIENT = [
    "Authentication failed (code: 503): We are down for maintenance",
    "Authentication failed (code: unknown_error): retry your request",
    "Authentication failed (code: 504): upstream request timeout",
    "Server is busy, try again later",
    "REQUEST_LIMIT_EXCEEDED: TotalRequests Limit exceeded",
]

FINAL = [
    "INVALID_LOGIN: Invalid username, password, security token; or user locked out",
    "invalid_grant: authentication failure",
    "Error Code 500. Response content: [{'message': 'invalid parameter value'}]",
    "MALFORMED_ID: Job Applicant ID: id value of incorrect type",
    "FIELD_CUSTOM_VALIDATION_EXCEPTION: masked file already attached",
]


@pytest.mark.parametrize("message", TRANSIENT)
def test_a_salesforce_blip_is_recognised_as_worth_retrying(message):
    """These three were seen in a week of live running, all at the login step
    and all with the org perfectly healthy either side."""
    assert sf_client.is_transient(RuntimeError(message)), message


@pytest.mark.parametrize("message", FINAL)
def test_a_real_answer_is_never_retried(message):
    """The half that matters more. A wrong password retried three times is
    still a wrong password, and a Job Applicant with no resume will not grow
    one -- retrying either only spends API calls to arrive back here."""
    assert not sf_client.is_transient(RuntimeError(message)), message


def test_a_missing_resume_is_final_whatever_it_says():
    """Matched on the exception type, not on its text, because the message is
    a filename and could contain anything at all."""
    assert not sf_client.is_transient(
        sf_client.ResumeNotFoundError("no resume: retry your request.pdf"))


def test_with_session_retries_a_blip_and_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(sf):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Authentication failed (code: 503): "
                               "We are down for maintenance")
        return "masked"

    monkeypatch.setattr(sf_client, "connect",
                        lambda client_key=None, force_refresh=False: object())
    monkeypatch.setattr(sf_client.time, "sleep", lambda s: None)
    assert sf_client.with_session(flaky) == "masked"
    assert calls["n"] == 3


def test_with_session_gives_up_after_the_configured_attempts(monkeypatch):
    calls = {"n": 0}

    def always_down(sf):
        calls["n"] += 1
        raise RuntimeError("Authentication failed (code: 504): "
                           "upstream request timeout")

    monkeypatch.setattr(sf_client, "connect",
                        lambda client_key=None, force_refresh=False: object())
    monkeypatch.setattr(sf_client.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        sf_client.with_session(always_down)
    assert calls["n"] == sf_client.RETRY_ATTEMPTS, \
        "a blip that never clears must still stop"


def test_with_session_does_not_retry_a_wrong_password(monkeypatch):
    calls = {"n": 0}

    def rejected(sf):
        calls["n"] += 1
        raise RuntimeError("INVALID_LOGIN: Invalid username, password, "
                           "security token; or user locked out")

    monkeypatch.setattr(sf_client, "connect",
                        lambda client_key=None, force_refresh=False: object())
    monkeypatch.setattr(sf_client.time, "sleep",
                        lambda s: pytest.fail("slept before a final failure"))
    with pytest.raises(RuntimeError):
        sf_client.with_session(rejected)
    assert calls["n"] == 1, "a rejected password was tried more than once"


def test_a_client_key_with_no_registry_uses_the_default_credentials(monkeypatch):
    """A client_key only means something once a registry exists.

    The Apex sends UserInfo.getOrganizationId() as the client_key on every
    call. On a single-tenant deployment -- no SF_CLIENTS_JSON, no rows in
    Postgres -- that used to be refused outright with "Unknown client_key ...
    Configured: none", so every batch failed while the org's own credentials
    sat working in the environment. Worse, the refusal came back as an empty
    results array rather than an error the caller recognised, so it read as
    "nothing to do".
    """
    monkeypatch.delenv("SF_CLIENTS_JSON", raising=False)
    monkeypatch.setenv("SF_USERNAME", "u@example.com")
    monkeypatch.setenv("SF_PASSWORD", "pw")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "tok")

    built = {}

    def fake_salesforce(**kwargs):
        built.update(kwargs)
        return object()

    monkeypatch.setattr(sf_client, "Salesforce", fake_salesforce)
    sf_client.connect(client_key="00D5j00000Di0AfEAJ")
    assert built.get("username") == "u@example.com", \
        "the default credentials were not used"

    # And it reports itself configured rather than raising.
    sf_client.connect_kwargs_present(client_key="00D5j00000Di0AfEAJ")
    assert sf_client.creds_configured(client_key="00D5j00000Di0AfEAJ") is True


def test_an_unknown_client_key_is_still_refused_when_a_registry_exists(monkeypatch):
    """The other half: once a registry is configured, a key that is not in it
    is a real mistake and must not silently fall back to some other org's
    credentials."""
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENTRY}))
    with pytest.raises(sf_client.UnknownClientError):
        sf_client.connect(client_key="not-a-registered-org")
    assert sf_client.creds_configured(client_key="not-a-registered-org") is False
