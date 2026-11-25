"""Secrets at rest: AES-256-GCM for mailbox credentials, SHA-256 for tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE_LEN = 12


def new_token(nbytes: int = 32) -> str:
    """Random URL-safe token (256 bits by default)."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class SecretBox:
    """Encrypts small secrets bound to a context string (e.g. the user's e-mail).

    The context is used as associated data, so a ciphertext copied to another
    user's row will not decrypt.
    """

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("encryption key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return _VERSION + nonce + self._aead.encrypt(nonce, plaintext.encode(), context.encode())

    def decrypt(self, blob: bytes, context: str) -> str:
        if not blob or blob[:1] != _VERSION:
            raise ValueError("unknown ciphertext format")
        nonce, ct = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
        return self._aead.decrypt(nonce, ct, context.encode()).decode()


def load_or_create_key(path: Path) -> bytes:
    """Read the base64 key from `path`, creating it (mode 600) on first run."""
    if path.exists():
        key = base64.b64decode(path.read_text().strip())
        if len(key) != 32:
            raise ValueError(f"{path}: key must be 32 bytes (base64)")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(key).decode() + "\n")
    return key
