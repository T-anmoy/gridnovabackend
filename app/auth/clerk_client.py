"""
Thin Clerk Backend API client. Staff roles live in the database (BE-17); Clerk's
`public_metadata.roles` / `.role` is kept as a copy so the frontend can render before
its first API call. Also: staff and associate invitations, the verified email
addresses checked at a first sign-in, and banning / unbanning a deactivated employee.

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
    Set `public_metadata.role` (and `roles`, to that one role) on a Clerk user. Returns True when the update was
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
            json={"public_metadata": {"role": role, "roles": [role]}},
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


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.clerk_secret_key}", "Content-Type": "application/json"}


def _call(method: str, path: str, what: str, **kwargs) -> httpx.Response:
    try:
        response = httpx.request(method, f"{CLERK_API_BASE}{path}", headers=_headers(), timeout=10, **kwargs)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Clerk call failed", extra={"what": what, "status": status_code})
        raise ClerkSyncError(f"Clerk {what} failed" + (f" (HTTP {status_code})" if status_code else "")) from exc
    return response


def update_user_roles(user_id: str, roles: list[str], primary: str) -> bool:
    """Copy a staff member's roles onto Clerk `public_metadata` (`roles`, `role` =
    primary). Clerk merges metadata, so other keys are kept. False when skipped (no key)."""
    if not settings.clerk_secret_key:
        logger.info("Clerk roles sync skipped (no secret key configured)", extra={"user_id": user_id})
        return False
    _call("PATCH", f"/users/{user_id}/metadata", "roles sync",
          json={"public_metadata": {"roles": list(roles), "role": primary}})
    return True


def create_staff_invitation(email: str, public_metadata: dict, redirect_url: str, expires_in_days: int,
                            ) -> tuple[bool, str | None]:
    """POST /v1/invitations for a staff member. Clerk emails it (`notify`), copies
    `public_metadata` onto the user it creates, and verifies the address on acceptance.
    (False, None) when skipped (no key); ClerkSyncError when Clerk refuses."""
    if not settings.clerk_secret_key:
        logger.info("Clerk staff invitation skipped (no secret key configured)")
        return False, None
    body: dict = {"email_address": email, "public_metadata": public_metadata, "notify": True,
                  "expires_in_days": expires_in_days}
    if redirect_url:
        body["redirect_url"] = redirect_url
    response = _call("POST", "/invitations", "staff invitation", json=body)
    return True, (response.json() or {}).get("id")


def revoke_invitation(invitation_id: str) -> bool:
    """POST /v1/invitations/{id}/revoke — the emailed link stops working."""
    if not settings.clerk_secret_key:
        return False
    _call("POST", f"/invitations/{invitation_id}/revoke", "invitation revoke")
    return True


def verified_emails(user_id: str) -> list[str] | None:
    """GET /v1/users/{id}: the user's email addresses whose verification status is
    "verified", lower-cased. None when skipped (no key)."""
    if not settings.clerk_secret_key:
        return None
    user = _call("GET", f"/users/{user_id}", "user lookup").json() or {}
    out = []
    for address in user.get("email_addresses") or []:
        if ((address.get("verification") or {}).get("status") == "verified") and address.get("email_address"):
            out.append(address["email_address"].strip().lower())
    return out


def ban_user(user_id: str) -> bool:
    """POST /v1/users/{id}/ban: Clerk revokes all the user's sessions and refuses any
    further sign-in until unbanned. (Clerk's lock expires on its own, so offboarding
    uses ban.)"""
    if not settings.clerk_secret_key:
        logger.info("Clerk ban skipped (no secret key configured)", extra={"user_id": user_id})
        return False
    _call("POST", f"/users/{user_id}/ban", "ban")
    return True


def unban_user(user_id: str) -> bool:
    """POST /v1/users/{id}/unban: the user may sign in again."""
    if not settings.clerk_secret_key:
        logger.info("Clerk unban skipped (no secret key configured)", extra={"user_id": user_id})
        return False
    _call("POST", f"/users/{user_id}/unban", "unban")
    return True
