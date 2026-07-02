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
    monkeypatch.delenv("SF_CLIENTS_JSON", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLIENT_SECRET_ENCRYPTION_KEY", raising=False)
    yield
    sf_client._token_cache.clear()
    sf_client.invalidate_registry_cache()


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


def _install_fake_db(monkeypatch) -> _FakeDb:
    fake = _FakeDb()
    monkeypatch.setattr(sf_client.db, "is_configured", fake.is_configured)
    monkeypatch.setattr(sf_client.db, "list_entries", fake.list_entries)
    monkeypatch.setattr(sf_client.db, "get_entry", fake.get_entry)
    monkeypatch.setattr(sf_client.db, "upsert_entry", fake.upsert_entry)
    monkeypatch.setattr(sf_client.db, "delete_entry", fake.delete_entry)
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
