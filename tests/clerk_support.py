"""A recording stand-in for Clerk's Backend API (BE-21): sessions, revokes, bans, unbans,
metadata updates, invitation revokes and verified email addresses."""
from __future__ import annotations

from app.auth.clerk_client import ClerkSyncError


class ClerkMock:
    def __init__(self, sessions=None, failing=(), ban_fails=False, verified=None):
        self.sessions = {k: list(v) for k, v in (sessions or {}).items()}
        self.failing = set(failing)
        self.ban_fails = ban_fails
        self.verified = verified
        self.revoked: list[str] = []
        self.bans: list[str] = []
        self.unbans: list[str] = []
        self.metadata: list[tuple[str, dict]] = []
        self.invitations_revoked: list[str] = []

    def active_session_ids(self, user_id):
        return [s for s in self.sessions.get(user_id, []) if s not in self.revoked]

    def revoke_session(self, session_id):
        if session_id in self.failing:
            raise ClerkSyncError("Clerk session revoke failed (HTTP 500)")
        self.revoked.append(session_id)
        return True

    def ban_user(self, user_id):
        if self.ban_fails:
            raise ClerkSyncError("Clerk ban failed (HTTP 402)")
        self.bans.append(user_id)
        return True

    def unban_user(self, user_id):
        self.unbans.append(user_id)
        return True

    def update_public_metadata(self, user_id, public_metadata):
        self.metadata.append((user_id, public_metadata))
        return True

    def revoke_invitation(self, invitation_id):
        self.invitations_revoked.append(invitation_id)
        return True

    def verified_emails(self, user_id):
        # Lower-cased, as the real client returns them.
        return None if self.verified is None else [e.strip().lower() for e in self.verified.get(user_id, [])]


def clerk_mock(monkeypatch, **kwargs) -> ClerkMock:
    mock = ClerkMock(**kwargs)
    for name in ("active_session_ids", "revoke_session", "ban_user", "unban_user", "update_public_metadata",
                 "revoke_invitation", "verified_emails"):
        monkeypatch.setattr(f"app.auth.clerk_client.{name}", getattr(mock, name))
    return mock
