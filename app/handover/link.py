"""The customer's handover link: 32 random bytes, only their SHA-256 stored, revocable,
optionally expiring (config handover.link_expiry_days, null = never). The plaintext token
exists once — in the issuing call — and goes to the customer by email or from staff's
response. Every failure to resolve a token (unknown, revoked, expired, not handed over) is
the same 404 at the route."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.handover import config
from app.memory.models import AuditLog, HandoverPack, ProjectStage, ProjectStageEvent

TOKEN_MIN, TOKEN_MAX = 16, 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(db: Session, pack: HandoverPack, actor: str) -> str:
    """A new token; any earlier one stops working. Returns the plaintext (never stored)."""
    token = secrets.token_urlsafe(32)
    days = config.handover()["link_expiry_days"]
    now = _now()
    before = {"issued_at": _aware(pack.link_issued_at).isoformat() if pack.link_issued_at else None,
              "revoked": pack.link_revoked_at is not None}
    pack.link_token_hash, pack.link_issued_at, pack.link_issued_by = hash_token(token), now, actor
    pack.link_expires_at = now + timedelta(days=days) if days else None
    pack.link_revoked_at = pack.link_revoked_by = None
    db.add(AuditLog(entity_type="handover_pack", entity_id=str(pack.id), action="link_issued", performed_by=actor,
                    before_value=before, after_value={"expires_at": pack.link_expires_at.isoformat()
                                                      if pack.link_expires_at else None}))
    db.flush()
    return token


def revoke(db: Session, pack: HandoverPack, actor: str) -> None:
    pack.link_revoked_at, pack.link_revoked_by = _now(), actor
    db.add(AuditLog(entity_type="handover_pack", entity_id=str(pack.id), action="link_revoked", performed_by=actor))
    db.flush()


def state(pack: HandoverPack) -> str:
    """none | active | revoked | expired — never the token."""
    if not pack.link_token_hash:
        return "none"
    if pack.link_revoked_at is not None:
        return "revoked"
    if pack.link_expires_at is not None and _aware(pack.link_expires_at) <= _now():
        return "expired"
    return "active"


def find(db: Session, token: str) -> Optional[HandoverPack]:
    """The pack behind a usable token, else None."""
    if not (TOKEN_MIN <= len(token) <= TOKEN_MAX):
        return None
    pack = db.execute(select(HandoverPack).where(HandoverPack.link_token_hash == hash_token(token))).scalar_one_or_none()
    if pack is None or state(pack) != "active":
        return None
    handed_over = db.execute(select(ProjectStageEvent.id).where(
        ProjectStageEvent.project_id == pack.project_id, ProjectStageEvent.stage == ProjectStage.handover_complete)
    ).first()
    return pack if handed_over else None


def public_pdf_url(token: str) -> Optional[str]:
    from app.config import settings
    base = (settings.public_api_base_url or "").rstrip("/")
    return f"{base}/api/v1/public/handover/{token}/pdf" if base else None
