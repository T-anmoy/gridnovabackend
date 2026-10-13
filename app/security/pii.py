"""The one way to read an associate's encrypted PAN or bank account number back (BE-4).

Both are stored Fernet-encrypted on the registration rows (app/security/encryption.py);
everything that is not the reason for decrypting them shows the last four characters
kept alongside. Every decryption writes an audit row naming the field, the record,
who asked and why — never the value. The caller commits (or the audit row goes with
its transaction).

    reveal(db, application, "pan", actor=..., purpose="application_review")
"""
from __future__ import annotations

from typing import Union

from sqlalchemy.orm import Session

from app.memory.models import AssociateApplication, AuditLog, PreferredAssociateApplication
from app.security.encryption import decrypt, encrypt

Application = Union[AssociateApplication, PreferredAssociateApplication]
FIELDS = {"pan": "pan_encrypted", "bank_account": "bank_account_encrypted"}
# Why a value may be decrypted. A new reason is a code change, reviewed like one.
PURPOSES = ("application_review", "tds_export", "payout_bank_export", "identity_claim_rehash")


def _entity(application: Application) -> str:
    kind = "preferred_associate" if isinstance(application, PreferredAssociateApplication) else "associate"
    return f"{kind}_application"


def reveal(db: Session, application: Application, field: str, *, actor: str, purpose: str) -> str:
    """The plaintext of `field` ("pan" | "bank_account"), audited."""
    if field not in FIELDS:
        raise ValueError(f"unknown encrypted field {field!r}")
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    value = decrypt(getattr(application, FIELDS[field]))
    db.add(AuditLog(entity_type=_entity(application), entity_id=str(application.id), action="pii_decrypted",
                    performed_by=actor, after_value={"field": field, "purpose": purpose}))
    return value


def protect_pan(pan: str) -> tuple[str, str]:
    """(pan_encrypted, pan_last4) for a normalised PAN."""
    return encrypt(pan), pan[-4:]
