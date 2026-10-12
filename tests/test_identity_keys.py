"""D6 (BE-3): identity claims have their own key (IDENTITY_HMAC_KEY), old claims can be
re-signed, and FERNET_ENCRYPTION_KEY can be rotated (MultiFernet) without breaking
either encrypted values or duplicate detection."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.associates import service
from app.config import settings
from app.memory.models import AssociateIdentityClaim, AssociateApplication
from app.security import encryption
from scripts import rehash_identity_claims, rotate_fernet
from tests.associate_support import (  # noqa: F401 — fixtures
    ASSOCIATE_APPLY, associate_form, kyc_ready, submit, territories,
)


def _register(client, n=1):
    r = submit(client, ASSOCIATE_APPLY, associate_form(n))
    assert r.status_code == 201, r.text
    return r


def test_the_claim_key_ignores_the_fernet_key_once_set(monkeypatch):
    monkeypatch.setattr(settings, "identity_hmac_key", "identity-secret")
    monkeypatch.setattr(settings, "fernet_encryption_key", Fernet.generate_key().decode())
    before = service.claim_hash("pan", "ABCPE1234F")
    monkeypatch.setattr(settings, "fernet_encryption_key", Fernet.generate_key().decode())
    assert service.claim_hash("pan", "ABCPE1234F") == before


def test_without_it_the_legacy_key_survives_prepending_a_fernet_key(monkeypatch):
    old = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "identity_hmac_key", "")
    monkeypatch.setattr(settings, "fernet_encryption_key", old)
    before = service.claim_hash("pan", "ABCPE1234F")
    monkeypatch.setattr(settings, "fernet_encryption_key", f"{Fernet.generate_key().decode()},{old}")
    assert service.claim_hash("pan", "ABCPE1234F") == before


def test_old_claims_are_re_signed_and_still_catch_a_duplicate(client, kyc_ready, db_session, monkeypatch):
    monkeypatch.setattr(settings, "identity_hmac_key", "")
    _register(client)                                    # claims signed with the legacy key
    old = {c.kind: c.value_hash for c in db_session.query(AssociateIdentityClaim)}

    monkeypatch.setattr(settings, "identity_hmac_key", "a-new-identity-secret")
    counts = rehash_identity_claims.rehash(db_session)
    db_session.commit()
    assert counts == {"claims": 4, "changed": 4, "missing_application": 0}
    assert all(c.value_hash != old[c.kind] for c in db_session.query(AssociateIdentityClaim))
    assert rehash_identity_claims.rehash(db_session)["changed"] == 0          # idempotent

    again = submit(client, ASSOCIATE_APPLY, associate_form(1))              # the same person
    assert again.status_code == 409, again.text


def test_the_script_refuses_without_the_new_key(monkeypatch, capsys):
    monkeypatch.setattr(settings, "identity_hmac_key", "")
    assert rehash_identity_claims.main("sqlite://", apply=False) == 2


def test_fernet_rotation_keeps_every_secret_readable(client, kyc_ready, db_session, monkeypatch):
    _register(client)
    old_key = settings.fernet_encryption_key
    app = db_session.query(AssociateApplication).one()
    plain = encryption.decrypt(app.bank_account_encrypted)

    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "fernet_encryption_key", f"{new_key},{old_key}")
    assert encryption.decrypt(app.bank_account_encrypted) == plain           # old ciphertext still reads
    assert rotate_fernet.rotate_all(db_session) == {"rotated": 1, "unreadable": 0}
    db_session.commit()

    monkeypatch.setattr(settings, "fernet_encryption_key", new_key)          # old key retired
    db_session.expire_all()
    assert encryption.decrypt(db_session.query(AssociateApplication).one().bank_account_encrypted) == plain
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(db_session.query(AssociateApplication).one().bank_account_encrypted.encode())
