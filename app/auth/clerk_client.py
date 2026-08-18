"""
Thin Clerk Backend API client — used to keep an employee's role in sync with
Clerk's `public_metadata.role`, which is the source of truth that
`get_current_employee` reads out of the verified JWT.

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
