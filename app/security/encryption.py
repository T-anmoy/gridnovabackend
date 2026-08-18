"""
Symmetric encryption for secrets stored at rest (currently employee Google refresh
tokens). Uses Fernet (AES-128-CBC + HMAC) with a key from settings.

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


def _fernet():
    from cryptography.fernet import Fernet

    if not settings.fernet_encryption_key:
        raise EncryptionKeyMissingError(
            "FERNET_ENCRYPTION_KEY is not set — cannot encrypt/decrypt secrets. "
            "Generate one with app.security.encryption.generate_key()."
        )
    return Fernet(settings.fernet_encryption_key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning ciphertext as a str."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by encrypt(), returning the original str."""
    return _fernet().decrypt(ciphertext.encode()).decode()
