"""Cutting a person off in Clerk after the database already has (BE-21).

The database is the off-switch: a deactivated employee or a non-active associate is
refused on their very next request (app/auth/dependencies.py), whatever Clerk does.
Clerk is told afterwards, in one of two modes:

  * revoke_sessions (default; CLERK_BAN_ENABLED false — Clerk's free plan has no ban):
    every active session of the Clerk user is revoked. They may sign in to Clerk again,
    and the API refuses them.
  * ban (CLERK_BAN_ENABLED true — Clerk Pro): the user is banned (Clerk revokes every
    session and refuses sign-in); reactivation unbans.

A Clerk failure never undoes the database change: it is logged and returned.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

MODE_BAN = "ban"
MODE_REVOKE = "revoke_sessions"


def mode() -> str:
    return MODE_BAN if settings.clerk_ban_enabled else MODE_REVOKE


def cut_off(user_id: str) -> dict:
    """{clerk_banned, sessions_revoked (int, or None when unknown/skipped), clerk_error,
    clerk_mode} for one Clerk user."""
    from app.auth import clerk_client

    current = mode()
    out = {"clerk_banned": False, "sessions_revoked": None, "clerk_error": None, "clerk_mode": current}
    try:
        if current == MODE_BAN:
            out["clerk_banned"] = clerk_client.ban_user(user_id)
        else:
            out["sessions_revoked"] = clerk_client.revoke_user_sessions(user_id)
    except clerk_client.SessionRevokeError as exc:
        out["sessions_revoked"], out["clerk_error"] = exc.revoked, str(exc)
    except clerk_client.ClerkSyncError as exc:
        out["clerk_error"] = str(exc)
    if out["clerk_error"]:
        logger.error("Clerk offboarding incomplete", extra={"user_id": user_id, "clerk_mode": current})
    return out


def restore(user_id: str) -> dict:
    """{clerk_unbanned, clerk_error, clerk_mode}. Only a ban has anything to undo: with
    sessions revoked, the database alone decides and nothing changes in Clerk."""
    from app.auth import clerk_client

    current = mode()
    if current != MODE_BAN:
        return {"clerk_unbanned": False, "clerk_error": None, "clerk_mode": current}
    try:
        return {"clerk_unbanned": clerk_client.unban_user(user_id), "clerk_error": None, "clerk_mode": current}
    except clerk_client.ClerkSyncError as exc:
        logger.error("Clerk unban failed", extra={"user_id": user_id})
        return {"clerk_unbanned": False, "clerk_error": str(exc), "clerk_mode": current}


def acted(outcome: dict) -> bool:
    """Clerk did something (or tried and failed) — worth an audit row."""
    return bool(outcome.get("clerk_banned") or outcome.get("clerk_unbanned") or outcome.get("invitation_revoked")
                or outcome.get("sessions_revoked") is not None or outcome.get("clerk_error"))
