"""
Clerk JWT verification.

Staging/production: verifies the JWT signature against Clerk's JWKS endpoint.
Development/test:   also accepts an X-Employee-Id / X-Employee-Role header pair
                    (or X-Associate-Id) for local testing — refused whenever
                    ENVIRONMENT is anything other than exactly development or test.

The dependencies are plain functions, not coroutines: they commit to the database and
may fetch Clerk's JWKS over HTTPS, so FastAPI runs them in its threadpool instead of on
the event loop.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.db import get_db
from app.memory.models import EmployeeRole

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)

# `public_metadata.principal` values. Staff tokens may omit it (every token issued
# before associates existed does); associate tokens must carry ASSOCIATE_PRINCIPAL.
STAFF_PRINCIPAL = "staff"
ASSOCIATE_PRINCIPAL = "associate"


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
    if not emp.is_active:
        # Refused before anything else is touched: signing in never edits a
        # deactivated employee's row, and never reactivates it.
        db.commit()
        raise DeactivatedEmployeeError(employee_id)
    # The token's role is authoritative (Clerk metadata). Keep the local mirror in
    # step so checks that read the stored role — e.g. lead assignment targets —
    # never act on a role changed in Clerk since this employee's first sign-in.
    if EmployeeRole(emp.role) != role:
        memory.update_employee_role(employee_id=employee_id, role=role, performed_by="auth_sync")
    db.commit()


# One JWKS client per process (per issuer), so its key-set cache actually serves
# requests: building a PyJWKClient per call meant an HTTPS fetch on every request.
JWKS_CACHE_LIFESPAN_SECONDS = 300
JWKS_FETCH_TIMEOUT_SECONDS = 5
_jwks_lock = threading.Lock()
_jwks_client = None
_jwks_client_uri: Optional[str] = None


class MissingSessionClaimsError(Exception):
    """A verified token without public_metadata — Clerk's session token is not
    configured to carry it, so no role or principal can be read."""


def _jwks_uri() -> str:
    return f"{settings.clerk_jwt_issuer.rstrip('/')}/.well-known/jwks.json"


def get_jwks_client():
    """The process-wide PyJWKClient for the configured issuer (rebuilt only if the
    issuer setting changes)."""
    global _jwks_client, _jwks_client_uri
    from jwt import PyJWKClient

    uri = _jwks_uri()
    with _jwks_lock:
        if _jwks_client is None or _jwks_client_uri != uri:
            _jwks_client = PyJWKClient(uri, cache_jwk_set=True, lifespan=JWKS_CACHE_LIFESPAN_SECONDS,
                                       timeout=JWKS_FETCH_TIMEOUT_SECONDS)
            _jwks_client_uri = uri
        return _jwks_client


def _verify_clerk_jwt(token: str) -> dict:
    """Verify signature, expiry, issuer and — when present — the authorized party
    (`azp`, set by Clerk to the browser origin) against ALLOWED_ORIGINS. Returns the
    claims; raises on any failure."""
    import jwt

    signing_key = get_jwks_client().get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        # A token without a subject would provision an employee with an empty id, and
        # one without an expiry would never expire: both are refused, not defaulted.
        options={"verify_exp": True, "require": ["exp", "sub", "iss"]},
        issuer=settings.clerk_jwt_issuer,
    )
    if not isinstance(payload.get("sub"), str) or not payload["sub"].strip():
        raise jwt.InvalidTokenError("sub is empty")
    azp = payload.get("azp")
    if azp is not None and azp.rstrip("/") not in {o.rstrip("/") for o in settings.allowed_origins}:
        raise jwt.InvalidTokenError("azp is not an allowed origin")
    return payload


def _session_metadata(payload: dict) -> dict:
    metadata = payload.get("public_metadata")
    if not isinstance(metadata, dict):
        raise MissingSessionClaimsError()
    return metadata


def _missing_claims() -> HTTPException:
    logger.warning(
        "A verified Clerk token carries no public_metadata. Configure the Clerk session token "
        "to include public_metadata (Clerk dashboard → Sessions → Customize session token)."
    )
    return HTTPException(status_code=403, detail={
        "code": "session_token_missing_claims",
        "message": "Your sign-in token does not carry your Grid Nova role. Ask an administrator to check "
                   "the Clerk session token settings.",
    })


_DEV_HEADERS_REFUSED = "Dev auth headers are only accepted in development and test."


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

    def can_review_proposal_inputs(self) -> bool:
        """Reviewing a site assessment: Admin and Sales, and Engineer (technical
        review). Creating, uploading to and drafting from it stay Admin/Sales."""
        return self.role in (EmployeeRole.admin, EmployeeRole.sales, EmployeeRole.engineer)

    def can_record_project_stage(self) -> bool:
        """Hand-recorded progress stages (offline acceptance, PM Surya Ghar portal approval)."""
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_review_associate_applications(self) -> bool:
        """Review, approve and reject Associate / Preferred Associate applications,
        and suspend, reinstate or deactivate associates."""
        return self.role == EmployeeRole.admin

    def can_view_onboardings(self) -> bool:
        """Associate-sourced customers. Sales is further limited to its own or
        unassigned leads, as everywhere else on the Sales surface."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales)

    def can_view_surveys(self) -> bool:
        """The on-site survey of an associate-sourced customer (S1). Engineers read it
        (they review it) but never the indicative prices in its results. Sales is
        limited to its own or unassigned leads."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales, EmployeeRole.engineer)

    def can_review_surveys(self) -> bool:
        """The feasibility review of a site survey, applying or rejecting survey
        corrections, and generating the survey report (S1 Steps 5–7)."""
        return self.role in (EmployeeRole.admin, EmployeeRole.engineer)

    def can_clear_reverification(self) -> bool:
        """Clear a proposal flagged for re-verification after a survey change."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.engineer)

    def can_verify_onboarding(self) -> bool:
        """The staff verification call on an associate-sourced customer."""
        return self.role in (EmployeeRole.admin, EmployeeRole.sales)

    def can_resolve_attribution(self) -> bool:
        return self.role == EmployeeRole.admin

    def can_manage_territories(self) -> bool:
        return self.role == EmployeeRole.admin

    def can_manage_seats(self) -> bool:
        return self.role == EmployeeRole.admin

    # ── Payments (Phase 4A) — see app/auth/scoping.py require_payment_access ──

    def can_view_payments(self) -> bool:
        """Payment amounts and the ledger. Sales only for its own or unassigned
        leads (it records payments there too). Operations and Engineer: never."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.accounts,
                             EmployeeRole.sales)

    def can_record_payment(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.accounts, EmployeeRole.sales)

    def can_process_payments(self) -> bool:
        """Deposit, verify, dishonour, cancel any payment; edit schedules; receipts;
        exports. Management reads but does not act."""
        return self.role in (EmployeeRole.admin, EmployeeRole.accounts)

    # ── Commissions, TDS and payouts (BE-4) — app/api/routes/commissions_admin.py ──

    def can_view_commissions(self) -> bool:
        """Commission entries, rate schedules, payout batches and queries. Management
        reads; Sales, Engineer and Operations: never."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.accounts)

    def can_process_commissions(self) -> bool:
        """Approve entries; prepare, approve and mark paid payout batches; answer
        queries; the commission and TDS exports."""
        return self.role in (EmployeeRole.admin, EmployeeRole.accounts)

    def can_manage_commission_rates(self) -> bool:
        """Publish a rate schedule, re-rate pending entries, reverse an entry (4.7)."""
        return self.role == EmployeeRole.admin

    def can_export_payout_bank_file(self) -> bool:
        """The payout bank file carries full account numbers: Accounts only."""
        return self.role == EmployeeRole.accounts

    def can_view_network_summary(self) -> bool:
        return self.role in (EmployeeRole.admin, EmployeeRole.management)

    # ── Execution (Phase 4A) — see app/auth/scoping.py require_execution_access ──

    def can_view_execution(self) -> bool:
        """Execution status, submissions, photos and the serial register. Sales is
        limited to its own or unassigned leads; Operations to projects at or after
        agreement_signed; Engineer reads the technical record. Accounts: none."""
        return self.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales,
                             EmployeeRole.engineer, EmployeeRole.operations)

    def can_verify_execution(self) -> bool:
        """Verify or reject an associate's stage submission; verify the serial register."""
        return self.role in (EmployeeRole.admin, EmployeeRole.operations)

    def can_record_execution(self) -> bool:
        """Record a stage directly (grid_nova mode) and upload execution evidence. Also
        acts on the handover pack (ticks, customer link) — BE-5."""
        return self.role in (EmployeeRole.admin, EmployeeRole.operations)

    def can_handle_service_requests(self) -> bool:
        """Raise on a customer's behalf, assign, add notes and change the status of a
        warranty / O&M service request (BE-5). Management and Sales read (scoped as
        execution is); Accounts: none."""
        return self.role in (EmployeeRole.admin, EmployeeRole.operations, EmployeeRole.engineer)


def get_current_employee(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedEmployee:
    # ── Dev-only header bypass ─────────────────────────────────────────────────
    dev_id = request.headers.get("X-Employee-Id")
    dev_role = request.headers.get("X-Employee-Role")
    if dev_id and dev_role:
        if not settings.allows_dev_auth:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DEV_HEADERS_REFUSED)
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
    try:
        metadata = _session_metadata(payload)
    except MissingSessionClaimsError:
        raise _missing_claims()
    # A staff token has no `principal` claim (or "staff"). Anything else — above all
    # an associate's token — is refused BEFORE an Employee row could be provisioned:
    # associates are a separate kind of user and must never become employees.
    principal = metadata.get("principal")
    if principal not in (None, "", STAFF_PRINCIPAL):
        raise HTTPException(status_code=403, detail="This account cannot access staff endpoints.")
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


# ── Associates: a separate principal ──────────────────────────────────────────

class AuthenticatedAssociate:
    def __init__(self, associate_pk: int, associate_id: str, associate_type: str, clerk_user_id: Optional[str]):
        self.associate_pk = associate_pk
        self.associate_id = associate_id
        self.associate_type = associate_type
        self.clerk_user_id = clerk_user_id


_ASSOCIATE_ONLY = "This endpoint is for Grid Nova associates only."


def _resolve_associate(db: Session, associate_id: str, clerk_user_id: Optional[str]) -> AuthenticatedAssociate:
    """Load the associate a verified identity claims to be, and refuse unless it is
    active. The first Clerk sign-in binds that Clerk user to the associate; from then
    on a token for any other Clerk user naming the same associate_id is refused."""
    from sqlalchemy import select
    from app.memory.models import Associate, AssociateStatus, AuditLog

    associate = db.execute(select(Associate).where(Associate.associate_id == associate_id)).scalar_one_or_none()
    if associate is None:
        raise HTTPException(status_code=403, detail="Unknown associate.")
    if associate.status != AssociateStatus.active:
        raise HTTPException(status_code=403, detail=f"This associate account is {associate.status.value}.")
    if clerk_user_id is not None:
        if associate.clerk_user_id is None:
            associate.clerk_user_id = clerk_user_id
            db.add(AuditLog(entity_type="associate", entity_id=associate.associate_id,
                            action="clerk_user_bound", performed_by=clerk_user_id,
                            before_value={"clerk_user_id": None},
                            after_value={"clerk_user_id": clerk_user_id}))
            db.commit()
        elif associate.clerk_user_id != clerk_user_id:
            raise HTTPException(status_code=403, detail="This sign-in does not belong to that associate.")
    return AuthenticatedAssociate(
        associate_pk=associate.id, associate_id=associate.associate_id,
        associate_type=associate.associate_type.value, clerk_user_id=associate.clerk_user_id,
    )


def get_current_associate(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedAssociate:
    """Authenticate an Associate / Preferred Associate. Accepts ONLY a Clerk token
    whose public_metadata carries principal="associate" and an associate_id; a staff
    token (a role claim, no associate principal) is refused. Never reads the staff
    X-Employee-* headers and never provisions an Employee row."""
    # ── Dev-only header bypass (mirrors X-Employee-Id) ─────────────────────────
    dev_associate = request.headers.get("X-Associate-Id")
    if dev_associate:
        if not settings.allows_dev_auth:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DEV_HEADERS_REFUSED)
        return _resolve_associate(db, dev_associate, clerk_user_id=None)

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

    try:
        metadata = _session_metadata(payload)
    except MissingSessionClaimsError:
        raise _missing_claims()
    if metadata.get("principal") != ASSOCIATE_PRINCIPAL:
        raise HTTPException(status_code=403, detail=_ASSOCIATE_ONLY)
    associate_id = metadata.get("associate_id")
    sub = payload.get("sub")
    if not associate_id or not isinstance(associate_id, str) or not sub:
        raise HTTPException(status_code=403, detail=_ASSOCIATE_ONLY)
    return _resolve_associate(db, associate_id, clerk_user_id=sub)
