"""Encrypt/decrypt the client_secret field before it ever touches Postgres.

CLIENT_SECRET_ENCRYPTION_KEY is read fresh from os.environ on every call (never
cached, never read at import time) so a deployment that never uses the DB-backed
client registry never has to set it -- validated lazily, only when an actual
encrypt/decrypt happens.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "CLIENT_SECRET_ENCRYPTION_KEY"


class EncryptionKeyError(RuntimeError):
    pass


def _get_fernet() -> Fernet:
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        raise EncryptionKeyError(
            f"{_ENV_VAR} is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as e:
        raise EncryptionKeyError(
            f"{_ENV_VAR} is not a valid Fernet key (must be 32 url-safe "
            f"base64-encoded bytes): {e}") from e


def encrypt_secret(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise EncryptionKeyError(
            f"Cannot decrypt client_secret -- {_ENV_VAR} may have changed "
            "since this row was written, or the row is corrupt.") from e
