"""Staff access (BE-17): invitations, roles, offboarding and second approvals.

Who may do what is config/access_control.yaml (app/auth/access.py); the roles a person
holds are rows in employee_roles. Every change here is audit-logged.
"""
from __future__ import annotations

from typing import Optional


class StaffError(Exception):
    """A refused operation. `status_code` and `code` become the HTTP error."""

    def __init__(self, message: str, *, code: str, status_code: int = 409, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.extra = extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **(self.extra or {})}
