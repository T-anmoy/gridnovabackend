"""The OAuth `state` for the Google Calendar consent flow (BE-1 S7).

The browser returns from Google with no Clerk session, so the callback learns which
employee is connecting from `state` alone. It used to be a deterministic HMAC of the
employee id with no expiry (falling back to the literal key "dev-insecure"), so one
captured state worked forever. Now:

    state = base64url(json {e: employee_id, n: random nonce, t: issued-at}) "." signature

  * signed with a key derived for this purpose only from a configured secret
    (CLERK_SECRET_KEY, else FERNET_ENCRYPTION_KEY) — with neither set, the flow does
    not start (no fallback key);
  * valid for STATE_TTL_SECONDS;
  * single-use: the nonce's hash is stored when the flow starts and marked used by the
    first callback that presents it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.models import OAuthStateNonce

STATE_TTL_SECONDS = 600
_KEY_LABEL = b"gridnova-google-oauth-state-v1"


class OAuthStateNotConfiguredError(RuntimeError):
    """No secret is configured to sign the state with."""


def _key() -> bytes:
    secret = (settings.clerk_secret_key or settings.fernet_encryption_key or "").strip()
    if not secret:
        raise OAuthStateNotConfiguredError(
            "Neither CLERK_SECRET_KEY nor FERNET_ENCRYPTION_KEY is set — the calendar connection cannot be started.")
    return hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def issue(db: Session, employee_id: str, now: Optional[datetime] = None) -> str:
    """A new state for this employee; stores its nonce. Caller commits."""
    key = _key()
    now = now or datetime.now(timezone.utc)
    nonce = secrets.token_urlsafe(24)
    db.add(OAuthStateNonce(nonce_hash=_nonce_hash(nonce), employee_id=employee_id, issued_at=now))
    db.flush()
    payload = _b64(json.dumps({"e": employee_id, "n": nonce, "t": int(now.timestamp())},
                              separators=(",", ":")).encode())
    signature = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def consume(db: Session, state: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """The employee id a valid, unexpired, unused state was issued for — marking it
    used — or None. Never raises on bad input. Caller commits."""
    try:
        key = _key()
        payload, signature = (state or "").split(".", 1)
        expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        employee_id, nonce, issued = str(data["e"]), str(data["n"]), int(data["t"])
    except (OAuthStateNotConfiguredError, ValueError, KeyError, TypeError):
        return None
    now = now or datetime.now(timezone.utc)
    issued_at = datetime.fromtimestamp(issued, tz=timezone.utc)
    if not (issued_at - timedelta(seconds=60) <= now <= issued_at + timedelta(seconds=STATE_TTL_SECONDS)):
        return None
    # Single use, also under a race: only the UPDATE that flips used_at wins.
    result = db.execute(update(OAuthStateNonce)
                        .where(OAuthStateNonce.nonce_hash == _nonce_hash(nonce),
                               OAuthStateNonce.employee_id == employee_id,
                               OAuthStateNonce.used_at.is_(None))
                        .values(used_at=now))
    return employee_id if result.rowcount == 1 else None
