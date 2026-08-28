"""DB-backed client registry — Postgres fully mocked at the app.db boundary.

Covers:
  * db.is_configured() False -> merged registry equals env-only (regression
    guard: a deployment that never provisions Postgres must behave exactly
    as it did before this module existed)
  * DB entry wins over a same-keyed env entry
  * differently-keyed env + DB entries both present in the merge
  * register_client() encrypts before persisting, invalidates the cache
    immediately (next _load_client_registry() call sees the write without
    waiting for the TTL)
  * register_client(allow_overwrite=False) raises ClientAlreadyRegisteredError
    on a second call for the same client_key
  * remove_client() on a nonexistent key returns False
  * crypto_util Fernet round-trip with a real generated key
  * crypto_util.EncryptionKeyError for unset/malformed/wrong-length keys

No real Postgres connection is ever made -- every touchpoint is monkeypatched
at the app.db function boundary, same pattern as mocking requests.post for
token fetches in test_sf_client_multitenant.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cryptography.fernet import Fernet

from app import crypto_util, db, sf_client

ACME_ENV_ENTRY = {
    "client_id": "acme-env-id",
    "client_secret": "acme-env-secret",
    "token_url": "https://acme.my.salesforce.com/services/oauth2/token",
    "instance_url": "https://acme.my.salesforce.com",
}

TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    sf_client._token_cache.clear()
    sf_client.invalidate_registry_cache()
    sf_client.invalidate_default_override_cache()
    monkeypatch.delenv("SF_CLIENTS_JSON", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLIENT_SECRET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("SF_USERNAME", raising=False)
    yield
    sf_client._token_cache.clear()
    sf_client.invalidate_registry_cache()
    sf_client.invalidate_default_override_cache()


class _FakeDb:
    """Minimal in-memory stand-in for app.db's module-level functions,
    installed via monkeypatch so sf_client._load_db_registry() /
    register_client() / remove_client() never touch a real connection."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.configured = True

    def is_configured(self) -> bool:
        return self.configured

    def list_entries(self) -> dict[str, dict]:
        return dict(self.rows)

    def get_entry(self, client_key: str) -> dict | None:
        return self.rows.get(client_key)

    def upsert_entry(self, client_key, client_id, encrypted_client_secret,
                      token_url, instance_url) -> None:
        self.rows[client_key] = {
            "client_id": client_id,
            "encrypted_client_secret": encrypted_client_secret,
            "token_url": token_url,
            "instance_url": instance_url,
        }

    def delete_entry(self, client_key: str) -> bool:
        return self.rows.pop(client_key, None) is not None

    def get_default_credentials(self) -> dict | None:
        return self.default_row

    def upsert_default_credentials(self, encrypted_password, encrypted_client_id,
                                    encrypted_client_secret, login_host) -> None:
        self.default_row = {
            "encrypted_password": encrypted_password,
            "encrypted_client_id": encrypted_client_id,
            "encrypted_client_secret": encrypted_client_secret,
            "login_host": login_host,
        }

    def delete_default_credentials(self) -> bool:
        existed = self.default_row is not None
        self.default_row = None
        return existed


def _install_fake_db(monkeypatch) -> _FakeDb:
    fake = _FakeDb()
    fake.default_row = None
    monkeypatch.setattr(sf_client.db, "is_configured", fake.is_configured)
    monkeypatch.setattr(sf_client.db, "list_entries", fake.list_entries)
    monkeypatch.setattr(sf_client.db, "get_entry", fake.get_entry)
    monkeypatch.setattr(sf_client.db, "upsert_entry", fake.upsert_entry)
    monkeypatch.setattr(sf_client.db, "delete_entry", fake.delete_entry)
    monkeypatch.setattr(sf_client.db, "get_default_credentials", fake.get_default_credentials)
    monkeypatch.setattr(sf_client.db, "upsert_default_credentials", fake.upsert_default_credentials)
    monkeypatch.setattr(sf_client.db, "delete_default_credentials", fake.delete_default_credentials)
    return fake


def test_db_unconfigured_registry_equals_env_only(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"00D000000000001AAA": ACME_ENV_ENTRY}))
    # db.is_configured() False by default (DATABASE_URL unset, no fake installed)
    registry = sf_client._load_client_registry()
    assert registry == {"00D000000000001AAA": ACME_ENV_ENTRY}


def test_db_entry_wins_over_same_keyed_env_entry(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"00D000000000001AAA": ACME_ENV_ENTRY}))
    fake = _install_fake_db(monkeypatch)
    fake.upsert_entry(
        "00D000000000001AAA", "db-client-id",
        crypto_util.encrypt_secret("db-client-secret"),
        "https://db-acme.my.salesforce.com/services/oauth2/token",
        "https://db-acme.my.salesforce.com",
    )

    registry = sf_client._load_client_registry()
    entry = registry["00D000000000001AAA"]
    assert entry["client_id"] == "db-client-id"
    assert entry["client_secret"] == "db-client-secret"
    assert entry["instance_url"] == "https://db-acme.my.salesforce.com"


def test_differently_keyed_env_and_db_entries_both_present(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"00D000000000001AAA": ACME_ENV_ENTRY}))
    fake = _install_fake_db(monkeypatch)
    fake.upsert_entry(
        "00D000000000002BBB", "beta-id", crypto_util.encrypt_secret("beta-secret"),
        "https://beta.my.salesforce.com/services/oauth2/token", "https://beta.my.salesforce.com",
    )

    registry = sf_client._load_client_registry()
    assert set(registry.keys()) == {"00D000000000001AAA", "00D000000000002BBB"}


def test_register_client_encrypts_and_invalidates_cache_immediately(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)

    # populate the cache with an empty registry first
    assert sf_client._load_client_registry() == {}

    sf_client.register_client(
        "00D000000000003CCC", "gamma-id", "gamma-plaintext-secret",
        "https://gamma.my.salesforce.com/services/oauth2/token",
        "https://gamma.my.salesforce.com",
    )

    # stored encrypted, not plaintext
    stored = fake.rows["00D000000000003CCC"]["encrypted_client_secret"]
    assert stored != "gamma-plaintext-secret"
    assert crypto_util.decrypt_secret(stored) == "gamma-plaintext-secret"

    # next read reflects the write immediately, no TTL wait
    registry = sf_client._load_client_registry()
    assert registry["00D000000000003CCC"]["client_secret"] == "gamma-plaintext-secret"


def test_register_client_create_only_rejects_duplicate(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)

    sf_client.register_client(
        "00D000000000004DDD", "id1", "secret1", "https://x/token", "https://x",
        allow_overwrite=False,
    )
    with pytest.raises(sf_client.ClientAlreadyRegisteredError):
        sf_client.register_client(
            "00D000000000004DDD", "id2", "secret2", "https://y/token", "https://y",
            allow_overwrite=False,
        )


def test_register_client_allow_overwrite_true_updates_existing(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)

    sf_client.register_client("00D000000000005EEE", "id1", "secret1", "https://x/token", "https://x")
    sf_client.register_client("00D000000000005EEE", "id2", "secret2", "https://y/token", "https://y")

    assert fake.rows["00D000000000005EEE"]["client_id"] == "id2"


def test_register_client_raises_when_postgres_not_configured(monkeypatch):
    with pytest.raises(sf_client.PostgresNotConfiguredError):
        sf_client.register_client(
            "00D000000000006FFF", "id", "secret", "https://x/token", "https://x")


def test_register_client_rejects_invalid_client_key(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    with pytest.raises(sf_client.InvalidIdError):
        sf_client.register_client("not-a-salesforce-id", "id", "secret", "https://x/token", "https://x")


def test_remove_client_on_nonexistent_key_returns_false(monkeypatch):
    _install_fake_db(monkeypatch)
    assert sf_client.remove_client("00D000000000007GGG") is False


def test_remove_client_raises_when_postgres_not_configured():
    with pytest.raises(sf_client.PostgresNotConfiguredError):
        sf_client.remove_client("00D000000000008HHH")


def test_list_registered_clients_never_includes_secret(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"00D000000000001AAA": ACME_ENV_ENTRY}))
    fake = _install_fake_db(monkeypatch)
    sf_client.register_client("00D000000000009III", "db-id", "db-secret", "https://x/token", "https://x")

    clients = sf_client.list_registered_clients()
    by_key = {c["client_key"]: c for c in clients}
    assert "client_secret" not in by_key["00D000000000001AAA"]
    assert "client_secret" not in by_key["00D000000000009III"]
    assert by_key["00D000000000001AAA"]["source"] == "env"
    assert by_key["00D000000000009III"]["source"] == "db"


def test_registry_backend_reflects_db_configuration(monkeypatch):
    assert sf_client.registry_backend() == "env-only"
    _install_fake_db(monkeypatch)
    assert sf_client.registry_backend() == "db+env"


# ── Default (no client_key) credential override ─────────────────────────────
# Backs /candidate/MaskProfileIndex's "User Settings" tab -- lets the
# password/security-token/Connected-App creds be rotated via Postgres without
# a Railway redeploy, while SF_USERNAME itself stays an env var.

class _FakeSalesforce:
    """Captures the kwargs connect() would pass to simple_salesforce.Salesforce,
    without ever attempting a real login."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeSalesforce.last = self


def test_register_default_credentials_requires_password_and_login_host(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    # No existing row -> nothing to "keep", password is still required
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.register_default_credentials("", None, None, "test")
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.register_default_credentials("pw", None, None, "")


def test_register_default_credentials_blank_password_keeps_existing_on_resave(monkeypatch):
    """The Settings tab's password field is write-only (never redisplayed) --
    resubmitting with it blank (e.g. to just fix the environment) must keep
    the previously-saved password, not fail or wipe it."""
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("original-password", None, None, "test")

    sf_client.register_default_credentials("", None, None, "login")  # only changing login_host

    assert crypto_util.decrypt_secret(fake.default_row["encrypted_password"]) == "original-password"
    assert fake.default_row["login_host"] == "login"


def test_register_default_credentials_blank_client_creds_keep_existing_on_resave(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw", "original-key", "original-secret", "test")

    sf_client.register_default_credentials("new-password", None, None, "test")  # only rotating password

    assert crypto_util.decrypt_secret(fake.default_row["encrypted_password"]) == "new-password"
    assert crypto_util.decrypt_secret(fake.default_row["encrypted_client_id"]) == "original-key"
    assert crypto_util.decrypt_secret(fake.default_row["encrypted_client_secret"]) == "original-secret"


def test_register_default_credentials_explicit_values_always_override(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw1", "key1", "secret1", "test")

    sf_client.register_default_credentials("pw2", "key2", "secret2", "login")

    assert crypto_util.decrypt_secret(fake.default_row["encrypted_password"]) == "pw2"
    assert crypto_util.decrypt_secret(fake.default_row["encrypted_client_id"]) == "key2"
    assert fake.default_row["login_host"] == "login"


def test_register_default_credentials_requires_client_key_and_secret_together(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.register_default_credentials("pw", "consumer-key-only", None, "test")
    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.register_default_credentials("pw", None, "consumer-secret-only", "test")


def test_register_default_credentials_raises_when_postgres_not_configured(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    with pytest.raises(sf_client.PostgresNotConfiguredError):
        sf_client.register_default_credentials("pw", None, None, "test")


def test_register_default_credentials_encrypts_and_invalidates_cache_immediately(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    fake = _install_fake_db(monkeypatch)

    sf_client.register_default_credentials("plain-password", None, None, "test")

    stored = fake.default_row["encrypted_password"]
    assert stored != "plain-password"
    assert crypto_util.decrypt_secret(stored) == "plain-password"
    assert fake.default_row["encrypted_client_id"] is None

    override = sf_client._load_default_override()
    assert override["password"] == "plain-password"
    assert override["login_host"] == "test"
    assert override["client_id"] is None


def test_connect_uses_default_override_plain_password_when_no_client_id(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "user@org.partialcpy")
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw+token", None, None, "test")
    monkeypatch.setattr(sf_client, "Salesforce", _FakeSalesforce)

    sf_client.connect()

    kwargs = _FakeSalesforce.last.kwargs
    assert kwargs["username"] == "user@org.partialcpy"
    assert kwargs["password"] == "pw+token"
    assert kwargs["security_token"] == ""
    assert kwargs["domain"] == "test"
    assert "consumer_key" not in kwargs


def test_connect_uses_default_override_connected_app_when_client_id_present(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "user@org.partialcpy")
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw+token", "consumer-key", "consumer-secret", "login")
    monkeypatch.setattr(sf_client, "Salesforce", _FakeSalesforce)

    sf_client.connect()

    kwargs = _FakeSalesforce.last.kwargs
    assert kwargs["consumer_key"] == "consumer-key"
    assert kwargs["consumer_secret"] == "consumer-secret"
    assert kwargs["domain"] == "login"


def test_connect_wraps_bad_env_var_credentials_as_salesforce_auth_error(monkeypatch):
    """Regression: a real SalesforceAuthenticationFailed (Salesforce
    rejecting a bad password/token) from the plain env-var login path used
    to propagate completely uncaught -- this is what turned a bad
    SF_PASSWORD/SF_SECURITY_TOKEN into an unhandled 500 on /mask instead of
    a clean error."""
    monkeypatch.setenv("SF_USERNAME", "user@org.com")
    monkeypatch.setenv("SF_PASSWORD", "wrong-password")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "wrong-token")
    _install_fake_db(monkeypatch)  # configured but no override row -- exercises the env-var branch

    from simple_salesforce.exceptions import SalesforceAuthenticationFailed

    def raising_salesforce(**kwargs):
        raise SalesforceAuthenticationFailed("INVALID_LOGIN", "Invalid username, password, security token")

    monkeypatch.setattr(sf_client, "Salesforce", raising_salesforce)

    with pytest.raises(sf_client.SalesforceAuthenticationError) as exc_info:
        sf_client.connect()
    assert "rejected" in str(exc_info.value).lower()


def test_connect_wraps_bad_override_credentials_as_salesforce_auth_error(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "user@org.partialcpy")
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("bad-password", None, None, "test")

    from simple_salesforce.exceptions import SalesforceAuthenticationFailed

    def raising_salesforce(**kwargs):
        raise SalesforceAuthenticationFailed("INVALID_LOGIN", "Invalid username, password, security token")

    monkeypatch.setattr(sf_client, "Salesforce", raising_salesforce)

    with pytest.raises(sf_client.SalesforceAuthenticationError) as exc_info:
        sf_client.connect()
    assert "settings-tab" in str(exc_info.value).lower()


def test_connect_wraps_unexpected_exception_types_too(monkeypatch):
    """Regression for a REAL production incident: a Settings-tab-saved
    Consumer Key/Secret pair caused Salesforce(...) to raise something
    other than SalesforceAuthenticationFailed (exact type never confirmed --
    Railway logs don't capture Python tracebacks), which propagated as an
    unhandled 500 on /mask because connect() only caught that one specific
    type. connect() must now wrap ANY exception from constructing the
    Salesforce session, not just the one type this module happened to
    anticipate."""
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "user@org.partialcpy")
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw", "consumer-key", "consumer-secret", "test")

    def raising_salesforce(**kwargs):
        raise KeyError("some totally unrelated library bug")  # NOT SalesforceAuthenticationFailed

    monkeypatch.setattr(sf_client, "Salesforce", raising_salesforce)

    with pytest.raises(sf_client.SalesforceAuthenticationError) as exc_info:
        sf_client.connect()
    assert "keyerror" in str(exc_info.value).lower()


def test_connect_with_client_credentials_wraps_unexpected_exceptions(monkeypatch):
    monkeypatch.setenv("SF_CLIENTS_JSON", json.dumps({"acme": ACME_ENV_ENTRY}))
    monkeypatch.setattr(
        sf_client, "get_client_credentials_token",
        lambda client_key, force_refresh=False: ("tok-abc", "https://acme.my.salesforce.com"))

    def raising_salesforce(**kwargs):
        raise ValueError("unexpected")

    monkeypatch.setattr(sf_client, "Salesforce", raising_salesforce)

    with pytest.raises(sf_client.SalesforceAuthenticationError):
        sf_client.connect(client_key="acme")


def test_connect_raises_when_override_present_but_sf_username_unset(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.delenv("SF_USERNAME", raising=False)
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw+token", None, None, "test")

    with pytest.raises(sf_client.MissingCredentialsError):
        sf_client.connect()


def test_connect_falls_back_to_env_vars_when_no_override_stored(monkeypatch):
    """No Postgres override row -- must behave exactly like before this
    feature existed (regression guard)."""
    monkeypatch.setenv("SF_USERNAME", "envuser@org.com")
    monkeypatch.setenv("SF_PASSWORD", "envpass")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "envtoken")
    monkeypatch.delenv("SF_CONSUMER_KEY", raising=False)
    _install_fake_db(monkeypatch)  # configured, but default_row stays None
    monkeypatch.setattr(sf_client, "Salesforce", _FakeSalesforce)

    sf_client.connect()

    kwargs = _FakeSalesforce.last.kwargs
    assert kwargs["username"] == "envuser@org.com"
    assert kwargs["security_token"] == "envtoken"


def test_default_credentials_status_unconfigured(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    status = sf_client.default_credentials_status()
    assert status == {"configured": False, "login_host": None, "has_client_credentials": False}


def test_default_credentials_status_configured_never_includes_password_or_secret(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("super-secret-password", "ck", "cs", "test")

    status = sf_client.default_credentials_status()
    assert status["configured"] is True
    assert status["login_host"] == "test"
    assert status["has_client_credentials"] is True
    assert "password" not in status
    assert "client_secret" not in status
    assert "super-secret-password" not in str(status)


def test_remove_default_credentials_returns_false_when_none_stored(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    _install_fake_db(monkeypatch)
    assert sf_client.remove_default_credentials() is False


def test_remove_default_credentials_clears_the_override(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "envuser@org.com")
    monkeypatch.setenv("SF_PASSWORD", "envpass")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "envtoken")
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("bad-password", "bad-key", "bad-secret", "test")
    assert sf_client._load_default_override() is not None

    assert sf_client.remove_default_credentials() is True
    assert sf_client._load_default_override() is None

    monkeypatch.setattr(sf_client, "Salesforce", _FakeSalesforce)
    sf_client.connect()  # must fall back to env vars now, not raise/use the cleared override
    kwargs = _FakeSalesforce.last.kwargs
    assert kwargs["username"] == "envuser@org.com"
    assert kwargs["security_token"] == "envtoken"


def test_remove_default_credentials_raises_when_postgres_not_configured():
    with pytest.raises(sf_client.PostgresNotConfiguredError):
        sf_client.remove_default_credentials()


def test_creds_configured_true_via_default_override_alone(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("SF_USERNAME", "user@org.partialcpy")
    monkeypatch.delenv("SF_PASSWORD", raising=False)
    monkeypatch.delenv("SF_SECURITY_TOKEN", raising=False)
    _install_fake_db(monkeypatch)
    sf_client.register_default_credentials("pw+token", None, None, "test")

    assert sf_client.creds_configured() is True


# ── crypto_util ───────────────────────────────────────────────────────────

def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    token = crypto_util.encrypt_secret("hello-world")
    assert token != "hello-world"
    assert crypto_util.decrypt_secret(token) == "hello-world"


def test_encrypt_raises_when_key_unset(monkeypatch):
    monkeypatch.delenv("CLIENT_SECRET_ENCRYPTION_KEY", raising=False)
    with pytest.raises(crypto_util.EncryptionKeyError):
        crypto_util.encrypt_secret("x")


def test_encrypt_raises_on_malformed_key(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(crypto_util.EncryptionKeyError):
        crypto_util.encrypt_secret("x")


def test_decrypt_raises_on_wrong_key(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", TEST_FERNET_KEY)
    token = crypto_util.encrypt_secret("secret-value")

    monkeypatch.setenv("CLIENT_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(crypto_util.EncryptionKeyError):
        crypto_util.decrypt_secret(token)
