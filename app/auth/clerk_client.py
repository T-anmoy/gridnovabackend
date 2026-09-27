"""
Thin Clerk Backend API client — used to keep an employee's role in sync with
Clerk's `public_metadata.role`, which is the source of truth that
`get_current_employee` reads out of the verified JWT — and to invite approved
associates, whose invitation carries the associate claims instead.

When `CLERK_SECRET_KEY` is unset (local dev / tests) this is a no-op so the
role update still succeeds against the local mirror without a network call.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

CLERK_API_BASE = "https://api.clerk.com/v1"


class ClerkSyncError(RuntimeError):
    """Raised when Clerk rejects a metadata update we asked it to make."""


def update_user_role(user_id: str, role: str) -> bool:
    """
    Set `public_metadata.role` on a Clerk user. Returns True when the update was
    applied against Clerk, False when skipped because no secret key is configured
    (local dev). Raises ClerkSyncError on an actual Clerk API failure.
    """
    if not settings.clerk_secret_key:
        logger.info(
            "Clerk sync skipped (no secret key configured); local role mirror only",
            extra={"user_id": user_id, "role": role},
        )
        return False

    url = f"{CLERK_API_BASE}/users/{user_id}/metadata"
    try:
        response = httpx.patch(
            url,
            headers={
                "Authorization": f"Bearer {settings.clerk_secret_key}",
                "Content-Type": "application/json",
            },
            json={"public_metadata": {"role": role}},
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Clerk role sync failed", extra={"user_id": user_id, "role": role})
        raise ClerkSyncError(f"Clerk metadata update failed for {user_id}") from exc

    logger.info("Clerk role synced", extra={"user_id": user_id, "role": role})
    return True


def create_associate_invitation(
    email: str, public_metadata: dict, redirect_url: str = "",
) -> tuple[bool, str | None]:
    """Invite an approved associate to sign up, via Clerk's Backend API
    (POST /v1/invitations). The invitation's `public_metadata` is copied onto the
    Clerk user Clerk creates when the invitation is accepted — that is how the
    associate claims (app/auth/dependencies.py get_current_associate) reach their
    session token. Clerk emails the invitation itself (`notify`).

    Returns (True, invitation_id) when Clerk created it, (False, None) when skipped
    because no secret key is configured (local dev). Raises ClerkSyncError when Clerk
    rejects it — e.g. the address already has a pending invitation or an account.
    """
    if not settings.clerk_secret_key:
        logger.info("Clerk invitation skipped (no secret key configured)",
                    extra={"associate_id": public_metadata.get("associate_id")})
        return False, None

    body: dict = {"email_address": email, "public_metadata": public_metadata, "notify": True}
    if redirect_url:
        body["redirect_url"] = redirect_url
    try:
        response = httpx.post(
            f"{CLERK_API_BASE}/invitations",
            headers={
                "Authorization": f"Bearer {settings.clerk_secret_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Clerk invitation failed",
                     extra={"associate_id": public_metadata.get("associate_id"), "status": status_code})
        raise ClerkSyncError(
            f"Clerk invitation failed for {public_metadata.get('associate_id')}"
            + (f" (HTTP {status_code})" if status_code else "")
        ) from exc

    invitation_id = (response.json() or {}).get("id")
    logger.info("Clerk invitation created", extra={"associate_id": public_metadata.get("associate_id")})
    return True, invitation_id
