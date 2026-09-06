"""Password hashing and secret encryption, byte-compatible with the retired
Node backend so existing users and stored provider tokens keep working.

- Passwords: scrypt (N=16384, r=8, p=1, dklen=64), stored as "salthex:hashhex",
  where the salt is a 32-char hex string used as UTF-8 bytes (Node behavior).
- Secrets: AES-256-GCM, key = SHA-256(CREDENTIAL_ENCRYPTION_KEY or JWT secret),
  stored as unpadded base64url "iv.tag.ciphertext" (Node buffer order).
"""

import base64
import hashlib
import hmac
import os
import secrets
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import get_settings

_SCRYPT = {"n": 16384, "r": 8, "p": 1, "dklen": 64, "maxmem": 64 * 1024 * 1024}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), **_SCRYPT)
    return f"{salt}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, expected_hex = (stored or "").partition(":")
    if not salt or not expected_hex:
        return False
    try:
        expected = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), **_SCRYPT)
    return hmac.compare_digest(actual, expected)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@lru_cache
def _key() -> bytes:
    settings = get_settings()
    return hashlib.sha256(
        (settings.credential_encryption_key or settings.auth_jwt_secret).encode()
    ).digest()


def encrypt_secret(value: str) -> str:
    iv = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(iv, value.encode(), None)
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join(_b64url_encode(part) for part in (iv, tag, ciphertext))


def decrypt_secret(value: str) -> str:
    iv, tag, ciphertext = (_b64url_decode(part) for part in value.split("."))
    return AESGCM(_key()).decrypt(iv, ciphertext + tag, None).decode()
