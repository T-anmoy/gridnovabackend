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


def _provision_employee(db: Session, employee_id: str, role: EmployeeRole, name: str = "", email: str = "",
                        roles=None) -> list[str]:
    """Dev/test headers only: get-or-create the Employee row and make its roles the ones
    the headers name (X-Employee-Role, or X-Employee-Roles for several), so a test can
    sign in as any role. Returns the roles held. The JWT path never does this — there the
    database decides (get_current_employee).

    Raises DeactivatedEmployeeError for an existing employee whose access has
    been switched off — a deactivated employee must not be re-activated by the
    mere act of presenting credentials.
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
    wanted = sorted({r.value for r in (roles or [role])})
    if EmployeeRole(emp.role) != role:
        memory.update_employee_role(employee_id=employee_id, role=role, performed_by="auth_sync")
    if sorted(memory.employee_roles(employee_id)) != wanted:
        memory.replace_employee_roles(employee_id, wanted, performed_by="auth_sync", audit=False)
    db.commit()
    return wanted


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


TWO_FACTOR_REQUIRED = {
    "code": "two_factor_required",
    "message": "This action needs two-step verification. Turn it on in your account settings, then sign in again.",
}


class AuthenticatedEmployee:
    """The signed-in staff member and what they may do.

    `roles` are the roles held (from employee_roles — the database decides, not the
    token); `permissions` their union (config/access_control.yaml). `role` is the
    primary role, kept for responses and code that only needs one.

    Every `can_*` gate is `has(<permission>)`. has() raises 403 two_factor_required
    when the permission is sensitive, every held role granting it requires two-step
    verification and this session did not complete a second factor; holds() is the
    same test as a plain yes/no, for narrowing a view rather than guarding an action.
    """

    def __init__(self, employee_id: str, role: EmployeeRole, email: str = "", name: str = "",
                 roles=None, two_factor_ok: bool = True, regions=None):
        from app.auth import access

        self.employee_id = employee_id
        self.role = role
        self.email = email
        self.name = name
        self.roles = frozenset(EmployeeRole(r) for r in (roles or (role,)))
        self.permissions = access.permissions_for(self.roles)
        self.two_factor_ok = two_factor_ok
        self.regions = tuple(regions or ())

    @property
    def role_values(self) -> frozenset:
        return frozenset(r.value for r in self.roles)

    def _blocked_by_two_factor(self, permission: str) -> bool:
        from app.auth import access

        return not self.two_factor_ok and access.needs_two_factor(self.roles, permission)

    def holds(self, permission: str) -> bool:
        from app.auth import access

        if permission not in access.load().permissions:
            raise KeyError(f"Unknown permission {permission!r}")
        return permission in self.permissions and not self._blocked_by_two_factor(permission)

    def has(self, permission: str) -> bool:
        if self.holds(permission):
            return True
        if permission in self.permissions:  # held, but this session lacks the second factor
            raise HTTPException(status_code=403, detail=TWO_FACTOR_REQUIRED)
        return False

    # ── Identity and view narrowing ──

    @property
    def is_admin(self) -> bool:
        """Holds the Admin role — an identity check (e.g. the Admin floor), never a gate."""
        return EmployeeRole.admin in self.roles

    @property
    def is_sales(self) -> bool:
        return EmployeeRole.sales in self.roles

    @property
    def is_management(self) -> bool:
        return EmployeeRole.management in self.roles

    @property
    def is_engineer(self) -> bool:
        return EmployeeRole.engineer in self.roles

    @property
    def sees_all_leads(self) -> bool:
        return self.holds("leads.view_all")

    @property
    def technical_only(self) -> bool:
        """The Engineer view: technical fields only — no customer contact details, prices
        or commercial history. False as soon as any held role sees commercial fields."""
        return self.holds("leads.view_technical") and not self.holds("leads.view_commercial")

    # ── Gates (one permission each; config/access_control.yaml) ──

    def can_approve_standard(self) -> bool:
        return self.has("proposals.approve_standard")

    def can_approve_manager_signoff(self) -> bool:
        return self.has("proposals.manager_signoff")

    def can_approve_partner_signoff(self) -> bool:
        """The 3rd, highest approval tier (app/rules/approval_engine.py). There
        is no distinct 'Partner' EmployeeRole — Admin is the most senior role
        that actually exists in this system, so this tier maps to Admin only,
        the same way can_manage_employees() does. If a real Partner-vs-Admin
        distinction is ever wanted, that's a role-model change, not a
        permission-check change."""
        return self.has("proposals.partner_signoff")

    def can_edit_lead(self) -> bool:
        return self.has("leads.edit")

    def can_change_lead_stage(self) -> bool:
        """Opportunity-tracker moves. Sales is further limited to its own or
        unassigned leads by the route, as everywhere else on the Sales surface."""
        return self.has("leads.change_stage")

    def can_create_lead(self) -> bool:
        return self.has("leads.create")

    def can_assign_lead(self) -> bool:
        return self.has("leads.assign")

    def can_trigger_proposal_draft(self) -> bool:
        return self.has("proposals.draft")

    def can_send_proposal(self) -> bool:
        return self.has("proposals.send")

    def can_view_audit_logs(self) -> bool:
        return self.has("audit.view")

    def can_manage_employees(self) -> bool:
        return self.has("staff.manage")

    def can_view_full_analytics(self) -> bool:
        return self.has("analytics.view_full")

    def can_schedule_meeting(self) -> bool:
        return self.has("meetings.schedule")

    def can_adjust_pricing(self) -> bool:
        return self.has("pricing.adjust")

    def can_query_customer_memory(self) -> bool:
        """Engineers are deliberately excluded: customer memory is full of PII and
        commercial context, and the Engineer surface elsewhere is limited to
        technical fields for exactly that reason."""
        return self.has("customers.memory_query")

    def can_manage_proposal_inputs(self) -> bool:
        return self.has("proposal_inputs.manage")

    def can_review_proposal_inputs(self) -> bool:
        """Reviewing a site assessment: Admin and Sales, and Engineer (technical
        review). Creating, uploading to and drafting from it stay Admin/Sales."""
        return self.has("proposal_inputs.review")

    def can_record_project_stage(self) -> bool:
        """Hand-recorded progress stages (offline acceptance, PM Surya Ghar portal approval)."""
        return self.has("projects.record_stage")

    def can_review_associate_applications(self) -> bool:
        """Review, approve and reject Associate / Preferred Associate applications,
        and suspend, reinstate or deactivate associates."""
        return self.has("associates.applications.review")

    def can_view_onboardings(self) -> bool:
        """Associate-sourced customers. Sales is further limited to its own or
        unassigned leads, as everywhere else on the Sales surface."""
        return self.has("onboardings.view")

    def can_view_surveys(self) -> bool:
        """The on-site survey of an associate-sourced customer (S1). Engineers read it
        (they review it) but never the indicative prices in its results. Sales is
        limited to its own or unassigned leads."""
        return self.has("surveys.view")

    def can_review_surveys(self) -> bool:
        """The feasibility review of a site survey, applying or rejecting survey
        corrections, and generating the survey report (S1 Steps 5–7)."""
        return self.has("surveys.review")

    def can_clear_reverification(self) -> bool:
        """Clear a proposal flagged for re-verification after a survey change."""
        return self.has("proposals.clear_reverification")

    def can_verify_onboarding(self) -> bool:
        """The staff verification call on an associate-sourced customer."""
        return self.has("onboardings.verify")

    def can_resolve_attribution(self) -> bool:
        return self.has("associates.attribution.resolve")

    def can_manage_territories(self) -> bool:
        return self.has("territories.manage")

    def can_manage_seats(self) -> bool:
        return self.has("associates.seats.manage")

    def can_view_payments(self) -> bool:
        """Payment amounts and the ledger. Sales only for its own or unassigned
        leads (it records payments there too). Operations and Engineer: never."""
        return self.has("payments.view")

    def can_record_payment(self) -> bool:
        return self.has("payments.record")

    def can_process_payments(self) -> bool:
        """Deposit, verify, dishonour, cancel any payment; edit schedules; receipts;
        exports. Management reads but does not act."""
        return self.has("payments.process")

    def can_view_commissions(self) -> bool:
        """Commission entries, rate schedules, payout batches and queries. Management
        reads; Sales, Engineer and Operations: never."""
        return self.has("commissions.view")

    def can_process_commissions(self) -> bool:
        """Approve entries; prepare, approve and mark paid payout batches; answer
        queries; the commission and TDS exports."""
        return self.has("commissions.process")

    def can_manage_commission_rates(self) -> bool:
        """Publish a rate schedule, re-rate pending entries, reverse an entry (4.7)."""
        return self.has("commissions.rates.manage")

    def can_view_individual_rates(self) -> bool:
        """BE-12: an Associate's individual rate, its history (with the internal reason)
        and their performance summary. Admin sets; Management reads; Accounts reads
        the rates on the ledger entries only."""
        return self.has("commissions.individual_rates.view")

    def can_export_payout_bank_file(self) -> bool:
        """The payout bank file carries full account numbers: Accounts only."""
        return self.has("payouts.bank_file.export")

    def can_view_network_summary(self) -> bool:
        return self.has("associates.network_summary.view")

    def can_view_execution(self) -> bool:
        """Execution status, submissions, photos and the serial register. Sales is
        limited to its own or unassigned leads; Operations to projects at or after
        agreement_signed; Engineer reads the technical record. Accounts: none."""
        return self.has("execution.view")

    def can_verify_execution(self) -> bool:
        """Verify or reject an associate's stage submission; verify the serial register."""
        return self.has("execution.verify")

    def can_record_execution(self) -> bool:
        """Record a stage directly (grid_nova mode) and upload execution evidence. Also
        acts on the handover pack (ticks, customer link) — BE-5."""
        return self.has("execution.record")

    def can_handle_service_requests(self) -> bool:
        """Raise on a customer's behalf, assign, add notes and change the status of a
        warranty / O&M service request (BE-5). Management and Sales read (scoped as
        execution is); Accounts: none."""
        return self.has("service_requests.handle")


def _second_factor_ok(payload: dict) -> bool:
    """Clerk's `fva` claim: [minutes since first factor, minutes since second factor];
    -1 for the second means no second factor in this session."""
    fva = payload.get("fva")
    return (isinstance(fva, list) and len(fva) == 2 and isinstance(fva[1], int)
            and not isinstance(fva[1], bool) and fva[1] >= 0)


_DEACTIVATED = "This account has been deactivated."


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
            extra = [EmployeeRole(r.strip()) for r in (request.headers.get("X-Employee-Roles") or "").split(",")
                     if r.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {dev_role}")
        roles = sorted({role, *extra}, key=lambda r: r.value)
        try:
            held = _provision_employee(db, dev_id, role, roles=roles)
        except DeactivatedEmployeeError:
            raise HTTPException(status_code=403, detail=_DEACTIVATED)
        from app.memory.memory_manager import MemoryManager
        regions = MemoryManager(db).get_employee(dev_id).regions
        return AuthenticatedEmployee(employee_id=dev_id, role=role, roles=held, regions=regions,
                                     two_factor_ok=request.headers.get("X-Employee-Two-Factor", "").lower() != "off")

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

    # The token proves who this is; the database decides what they may do. Roles in the
    # token's metadata are only a copy for the frontend and are never read here — except
    # by the bootstrap path for a first Admin (app/staff/invitations.py).
    from app.memory.memory_manager import MemoryManager
    from app.staff import invitations

    memory = MemoryManager(db)
    emp = memory.get_employee(employee_id)
    if emp is None:
        emp = invitations.first_sign_in(db, employee_id, payload, metadata)
    if not emp.is_active:
        raise HTTPException(status_code=403, detail=_DEACTIVATED)
    roles = memory.employee_roles(employee_id)
    return AuthenticatedEmployee(
        employee_id=employee_id,
        role=EmployeeRole(emp.role),
        roles=roles,
        email=payload.get("email", "") or emp.email,
        name=payload.get("name", "") or emp.name,
        two_factor_ok=_second_factor_ok(payload),
        regions=emp.regions,
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
