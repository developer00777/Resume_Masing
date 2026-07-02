"""Postgres-backed client-org registry storage. Fully optional: every function
in this module is a no-op or empty-result when DATABASE_URL isn't set, so a
deployment that never provisions Postgres behaves exactly as if this module
didn't exist.

No FastAPI/Pydantic imports -- keeps this mockable in isolation the same way
sf_client.requests.post is monkeypatched today. Callers (sf_client.py) own
encryption; this module only ever sees/stores the already-encrypted secret
string, never plaintext.

Each function opens and closes its own connection. Registry reads are cached
one layer up (sf_client._load_client_registry), and admin writes are rare, so
a connection pool would be unused complexity here.
"""
from __future__ import annotations

import os

import psycopg

_ENV_VAR = "DATABASE_URL"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client_orgs (
    client_key               TEXT PRIMARY KEY,
    client_id                TEXT NOT NULL,
    encrypted_client_secret  TEXT NOT NULL,
    token_url                TEXT NOT NULL,
    instance_url              TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def is_configured() -> bool:
    return bool(os.environ.get(_ENV_VAR, "").strip())


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ[_ENV_VAR])


def ensure_schema() -> None:
    """Idempotent. Called lazily before any read/write, never at import time."""
    with _connect() as conn:
        conn.execute(_CREATE_TABLE_SQL)


def list_entries() -> dict[str, dict]:
    ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT client_key, client_id, encrypted_client_secret, token_url, "
            "instance_url FROM client_orgs").fetchall()
    return {
        row[0]: {
            "client_id": row[1],
            "encrypted_client_secret": row[2],
            "token_url": row[3],
            "instance_url": row[4],
        }
        for row in rows
    }


def get_entry(client_key: str) -> dict | None:
    ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, encrypted_client_secret, token_url, instance_url "
            "FROM client_orgs WHERE client_key = %s", (client_key,)).fetchone()
    if row is None:
        return None
    return {
        "client_id": row[0],
        "encrypted_client_secret": row[1],
        "token_url": row[2],
        "instance_url": row[3],
    }


def upsert_entry(client_key: str, client_id: str, encrypted_client_secret: str,
                  token_url: str, instance_url: str) -> None:
    ensure_schema()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO client_orgs
                (client_key, client_id, encrypted_client_secret, token_url, instance_url, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (client_key) DO UPDATE SET
                client_id = EXCLUDED.client_id,
                encrypted_client_secret = EXCLUDED.encrypted_client_secret,
                token_url = EXCLUDED.token_url,
                instance_url = EXCLUDED.instance_url,
                updated_at = now()
            """,
            (client_key, client_id, encrypted_client_secret, token_url, instance_url),
        )


def delete_entry(client_key: str) -> bool:
    ensure_schema()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM client_orgs WHERE client_key = %s RETURNING client_key",
            (client_key,))
        deleted = cur.fetchone() is not None
    return deleted
