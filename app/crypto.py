# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Encryption of secrets at rest and password hashing.

Every secret (SSH private keys, key passphrases, sudo passwords, notification
tokens) is encrypted with AES-256-GCM before it touches the database. The key is
derived (HKDF-SHA256) from a master secret that lives OUTSIDE the database:
an environment variable, a Docker secret file, or - as a fallback - a key file
in the data volume created with 0600 permissions.

Each ciphertext is bound to its context (e.g. "server:3:private_key") via GCM
associated data, so encrypted values cannot be swapped between records.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("patchrack.crypto")

DATA_DIR = os.environ.get("PATCHRACK_DATA", "/data")
_PREFIX = "v1:"


class DecryptError(Exception):
    pass


def _load_master_secret() -> bytes:
    value = os.environ.get("PATCHRACK_SECRET", "").strip()
    secret_file = os.environ.get("PATCHRACK_SECRET_FILE", "").strip()
    if not value and secret_file:
        if not os.path.exists(secret_file):
            raise SystemExit(f"PATCHRACK_SECRET_FILE points to {secret_file}, which does not exist")
        with open(secret_file, encoding="utf-8") as fh:
            value = fh.read().strip()
    if not value:
        path = os.path.join(DATA_DIR, ".master.key")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            value = secrets.token_urlsafe(48)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value)
            log.warning(
                "No PATCHRACK_SECRET or PATCHRACK_SECRET_FILE set. Generated a master key at %s. "
                "For better protection, move it to a Docker secret (see README).", path)
    if len(value) < 32:
        raise SystemExit("The master secret must be at least 32 characters long")
    return value.encode("utf-8")


def _derive(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"patchrack-v1", info=info).derive(master)


_MASTER = _load_master_secret()
_FIELD_KEY = AESGCM(_derive(_MASTER, b"field-encryption"))
SESSION_KEY = _derive(_MASTER, b"session-signing").hex()


def encrypt(plaintext: str | None, context: str) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    nonce = os.urandom(12)
    ct = _FIELD_KEY.encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str | None, context: str) -> str | None:
    if not token:
        return None
    if not token.startswith(_PREFIX):
        raise DecryptError("Unknown ciphertext format")
    raw = base64.b64decode(token[len(_PREFIX):])
    try:
        return _FIELD_KEY.decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")
    except InvalidTag as exc:
        raise DecryptError(
            "Could not decrypt a stored secret. The master secret does not match this database.") from exc


# ---- password hashing (scrypt) -------------------------------------------------
_N, _R, _P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False
