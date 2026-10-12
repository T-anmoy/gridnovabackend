"""
Symmetric encryption for secrets stored at rest (employee Google refresh tokens,
associate bank account numbers). Uses Fernet (AES-128-CBC + HMAC) with a key from settings.

Rotation (deploy/RUNBOOK.md, "Rotating FERNET_ENCRYPTION_KEY"): FERNET_ENCRYPTION_KEY is
a comma-separated list, newest first. The first key encrypts; every key decrypts
(cryptography's MultiFernet). To rotate: put the new key in front of the old one,
deploy, run scripts/rotate_fernet.py (re-encrypts every stored secret with the new
key), then drop the old key. The identity-claim HMAC does NOT depend on this key once
IDENTITY_HMAC_KEY is set (app/associates/service.py).

The key is required only when a secret is actually handled — encrypt/decrypt raise a
clear RuntimeError if FERNET_ENCRYPTION_KEY is unset, so a config gap fails loudly at
the point of use rather than silently storing plaintext. Tests never store real tokens,
so they don't need a key configured.
"""
from __future__ import annotations

from app.config import settings


class EncryptionKeyMissingError(RuntimeError):
    """Raised when an encryption operation is attempted without a configured key."""


def generate_key() -> str:
    """Generate a new Fernet key (base64 str). Use this to populate FERNET_ENCRYPTION_KEY."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def keys() -> list[str]:
    """The configured Fernet keys, newest (the encrypting one) first."""
    return [k.strip() for k in settings.fernet_encryption_key.split(",") if k.strip()]


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    configured = keys()
    if not configured:
        raise EncryptionKeyMissingError(
            "FERNET_ENCRYPTION_KEY is not set — cannot encrypt/decrypt secrets. "
            "Generate one with app.security.encryption.generate_key()."
        )
    return MultiFernet([Fernet(k.encode()) for k in configured])


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning ciphertext as a str."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by encrypt() under any configured key."""
    return _fernet().decrypt(ciphertext.encode()).decode()


def rotate(ciphertext: str) -> str:
    """The same secret re-encrypted with the first (newest) key."""
    return _fernet().rotate(ciphertext.encode()).decode()
