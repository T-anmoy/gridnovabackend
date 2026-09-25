"""
Clerk JWT verification.

Production: verifies the JWT signature against Clerk's JWKS endpoint.
Development: accepts an X-Employee-Id / X-Employee-Role header pair for
             local testing — HARD-BLOCKED when ENVIRONMENT=production.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.db import get_db
from app.memory.models import EmployeeRole

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


class DeactivatedEmployeeError(Exception):
    """Raised when a deactivated employee's credentials are otherwise valid."""


def _provision_employee(db: Session, employee_id: str, role: EmployeeRole, name: str = "", email: str = "") -> None:
    """Get-or-create the local Employee mirror row for an authenticated identity.

    Called on every successful authentication (dev-header or real JWT) so the
    `employees` table actually reflects who has signed in — previously nothing
    ever wrote to it, which silently broke Sales-role lead/proposal scoping and
    the Google Calendar connection flow for any employee who hadn't been
    manually pre-seeded. Committed immediately so it survives independent of
    whatever the route handler does with its own session afterward.

    Raises DeactivatedEmployeeError for an existing employee whose access has
    been switched off — a deactivated employee must not be re-activated by the
    mere act of presenting a still-valid Clerk session.
    """
    from app.memory.memory_manager import MemoryManager

    memory = MemoryManager(db)
    emp = memory.get_or_create_employee(
        employee_id,
        name or employee_id,
        email or f"{employee_id}@no-email.local",
        role.value,
    )
    db.commit()
    if not emp.is_active:
        raise DeactivatedEmployeeError(employee_id)


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    if not settings.clerk_jwt_issuer:
        return {}
    jwks_url = f"{settings.clerk_jwt_issuer.rstrip('/')}/.well-known/jwks.json"
    response = httpx.get(jwks_url, timeout=10)
    response.raise_for_status()
    return response.json()


def _verify_clerk_jwt(token: str) -> dict:
    """Verify signature and return decoded claims. Raises on failure."""
    import jwt
    from jwt import PyJWKClient

    jwks_uri = f"{settings.clerk_jwt_issuer.rstrip('/')}/.well-known/jwks.json"
    jwks_client = PyJWKClient(jwks_uri)
    signing_key = jwks_client.get_signing_key_from_jwt(token)

    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"verify_exp": True},
        issuer=settings.clerk_jwt_issuer,
    )
    return payload


class AuthenticatedEmployee:
    def __init__(self, employee_id: str, role: EmployeeRole, email: str = "", name: str = ""):
        self.employee_id = employee_id
        self.role = role
        self.email = email
        self.name = name

    @property
    def is_admin(self) -> bool:
        return self.role == EmployeeRole.admin

    @property
    def is_sales(self) -> bool:
        return self.role == EmployeeRole.sales

    @property
    def is_management(self) -> bool:
        return self.role == EmployeeRole.management

    @property
    def is_engineer(self) -> bool:
        return self.role == EmployeeRole.engineer

    def can_approve_standard(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_approve_manager_signoff(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management)

    def can_approve_partner_signoff(self) -> bool:
        """The 3rd, highest approval tier (app/rules/approval_engine.py). There
        is no distinct 'Partner' EmployeeRole — Admin is the most senior role
        that actually exists in this system, so this tier maps to Admin only,
        the same way can_manage_employees() does. If a real Partner-vs-Admin
        distinction is ever wanted, that's a role-model change, not a
        permission-check change."""
        return self.role == EmployeeRole.admin

    def can_edit_lead(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_change_lead_stage(self) -> bool:
        """Opportunity-tracker moves. Sales is further limited to its own or
        unassigned leads by the route, as everywhere else on the Sales surface."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales)

    def can_create_lead(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales)

    def can_assign_lead(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management)

    def can_trigger_proposal_draft(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_send_proposal(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_view_audit_logs(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management)

    def can_manage_employees(self) -> bool:
        return self.role == EmployeeRole.admin

    def can_view_full_analytics(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management)

    def can_schedule_meeting(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_adjust_pricing(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_query_customer_memory(self) -> bool:
        """Engineers are deliberately excluded: customer memory is full of PII and
        commercial context, and the Engineer surface elsewhere is limited to
        technical fields for exactly that reason."""
        return self.role in (EmployeeRole.admin, EmployeeRole.sales, EmployeeRole.management)

    def can_manage_proposal_inputs(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_view_assigned_work(self) -> bool:
        """Intentionally False for every role, including Subcontractor.

        Not a broken check — there is genuinely nothing to view yet. Work
        Orders don't exist. This role starts maximally restricted and gets
        expanded deliberately once there's something real to grant access to,
        rather than starting broad and being tightened later.
        """
        return False


async def get_current_employee(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedEmployee:
    # ── Dev-only header bypass ─────────────────────────────────────────────────
    dev_id = request.headers.get("X-Employee-Id")
    dev_role = request.headers.get("X-Employee-Role")
    if dev_id and dev_role:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Dev auth headers are not permitted in production.",
            )
        try:
            role = EmployeeRole(dev_role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {dev_role}")
        try:
            _provision_employee(db, dev_id, role)
        except DeactivatedEmployeeError:
            raise HTTPException(status_code=403, detail="This account has been deactivated.")
        return AuthenticatedEmployee(employee_id=dev_id, role=role)

    # ── JWT verification ───────────────────────────────────────────────────────
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = _verify_clerk_jwt(credentials.credentials)
    except Exception as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    employee_id = payload.get("sub", "")
    metadata = payload.get("public_metadata", {})
    role_str = metadata.get("role", "")

    try:
        role = EmployeeRole(role_str)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"Unknown employee role: {role_str!r}")

    email = payload.get("email", "")
    name = payload.get("name", "")
    try:
        _provision_employee(db, employee_id, role, name=name, email=email)
    except DeactivatedEmployeeError:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")

    return AuthenticatedEmployee(
        employee_id=employee_id,
        role=role,
        email=email,
        name=name,
    )
