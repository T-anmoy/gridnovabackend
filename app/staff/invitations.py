"""Staff invitations and the first sign-in.

An invitation names a person, their email and their roles. Clerk emails the link and,
on acceptance, verifies the address and copies `public_metadata` ({roles, role,
staff_invitation_id}) onto the new Clerk user. The first time that user calls the API,
first_sign_in() matches them to the pending invitation by a verified email address — and
by the invitation id in their metadata when present — and creates the employee with
exactly the invited roles.

Nobody else gets in: no employee row and no matching invitation is 403 not_invited. The
one exception is the bootstrap path for a first Admin, kept from before BE-17: a Clerk
user whose metadata an operator set to {"role": "admin"} by hand, carrying no invitation
id — and, since BE-21, only with a verified address on BOOTSTRAP_ADMIN_EMAILS
(scripts/grant_admin.py is the other way in; RUNBOOK §8).

When Clerk cannot create the invitation (BE-21), the Grid Nova invitation still stands
with `delivery: "manual"`: the person signs up normally at the dashboard sign-up page
with the invited address, and the verified-email match above admits them.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import access
from app.config import settings
from app.memory.models import (
    AuditLog, Employee, EmployeeRole, EmployeeRoleGrant, StaffInvitation, StaffInvitationStatus,
)
from app.staff import StaffError
from app.staff.roles import check_combination, check_scope

logger = logging.getLogger(__name__)

NOT_INVITED = {"code": "not_invited",
               "message": "You have not been invited to the Grid Nova dashboard. Ask a Grid Nova administrator "
                          "to invite you."}
INVITATION_EXPIRED = {"code": "invitation_expired",
                      "message": "Your invitation to the Grid Nova dashboard has expired. Ask a Grid Nova "
                                 "administrator to send it again."}
INVITATION_APPROVAL_ACTIONS = {"admin": "role.admin", "accounts": "role.accounts"}
BOOTSTRAP = "bootstrap"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def _expired(inv: StaffInvitation, now: Optional[datetime] = None) -> bool:
    return inv.status == StaffInvitationStatus.pending and inv.expires_at <= (now or _now())


def status_of(inv: StaffInvitation) -> str:
    return StaffInvitationStatus.expired.value if _expired(inv) else StaffInvitationStatus(inv.status).value


def serialize(inv: StaffInvitation) -> dict:
    def iso(v):
        return v.isoformat() if v else None
    return {"invitation_id": inv.id, "name": inv.name, "email": inv.email, "roles": list(inv.roles),
            "status": status_of(inv), "invited_by": inv.invited_by, "clerk_status": inv.clerk_status,
            "delivery": delivery_of(inv), "manual_instructions": manual_instructions(inv),
            "created_at": iso(inv.created_at), "expires_at": iso(inv.expires_at), "resent_at": iso(inv.resent_at),
            "accepted_employee_id": inv.accepted_employee_id, "accepted_at": iso(inv.accepted_at),
            "revoked_by": inv.revoked_by, "revoked_at": iso(inv.revoked_at)}


def _audit(db: Session, inv: StaffInvitation, action: str, performed_by: str, before=None, after=None) -> None:
    db.add(AuditLog(entity_type="staff_invitation", entity_id=inv.id, action=action, performed_by=performed_by,
                    before_value=before, after_value=after))


def _close_expired(db: Session, email: str) -> None:
    """A pending row past its expiry becomes `expired`, so the address can be invited again."""
    for inv in db.execute(select(StaffInvitation).where(StaffInvitation.email == email,
                                                        StaffInvitation.status == StaffInvitationStatus.pending)
                          ).scalars():
        if _expired(inv):
            inv.status = StaffInvitationStatus.expired
            _audit(db, inv, "invitation_expired", "system", after={"status": "expired"})
    db.flush()


def approval_actions(roles: list[str]) -> list[str]:
    return [a for r, a in INVITATION_APPROVAL_ACTIONS.items() if r in roles and access.second_approval_required(a)]


def validate_new(db: Session, actor, name: str, email: str, roles: list[str]) -> tuple[str, str, list[str]]:
    name = (name or "").strip()
    email = normalise_email(email)
    roles = sorted(set(roles))
    if not name:
        raise StaffError("Give the person's name.", code="invalid_name", status_code=422)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise StaffError("Give a valid email address.", code="invalid_email", status_code=422)
    check_combination(set(roles))
    check_scope(actor, set(roles), verb="invite")
    if db.execute(select(Employee.id).where(func.lower(Employee.email) == email)).first():
        raise StaffError("Someone on the staff already uses that email address.", code="already_staff")
    _close_expired(db, email)
    if db.execute(select(StaffInvitation.id).where(StaffInvitation.email == email,
                                                   StaffInvitation.status == StaffInvitationStatus.pending)).first():
        raise StaffError("That address already has a pending invitation. Resend or revoke it.",
                         code="invitation_exists")
    return name, email, roles


def _send_clerk(inv: StaffInvitation) -> Optional[str]:
    """Ask Clerk to email the invitation. When Clerk refuses (BE-21) the Grid Nova
    invitation still stands — `clerk_status: failed`, delivery manual — and the error is
    returned for the audit row."""
    from app.auth import clerk_client

    metadata = {"roles": list(inv.roles), "role": access.primary_role(inv.roles), "staff_invitation_id": inv.id}
    try:
        created, clerk_id = clerk_client.create_staff_invitation(
            inv.email, metadata, settings.staff_invite_redirect_url, access.load().invitation_expiry_days)
    except clerk_client.ClerkSyncError as exc:
        logger.warning("Clerk staff invitation failed; delivery is manual", extra={"invitation_id": inv.id})
        inv.clerk_invitation_id = None
        inv.clerk_status = "failed"
        return str(exc)
    inv.clerk_invitation_id = clerk_id
    inv.clerk_status = "sent" if created else "skipped"
    return None


def delivery_of(inv: StaffInvitation) -> str:
    """"clerk_email" when Clerk emailed the link; "manual" when it did not (refused, or no
    Clerk key) and someone must tell the person to sign up themselves."""
    return "clerk_email" if inv.clerk_status == "sent" else "manual"


def manual_instructions(inv: StaffInvitation) -> Optional[str]:
    if delivery_of(inv) != "manual" or status_of(inv) != StaffInvitationStatus.pending.value:
        return None
    return (f"Ask {inv.name} to sign up at {settings.frontend_base_url.rstrip('/')}/dashboard/sign-up "
            f"with {inv.email}.")


def issue(db: Session, *, name: str, email: str, roles: list[str], invited_by: str, send: bool = True) -> StaffInvitation:
    """Create the pending invitation and (when `send`) Clerk's. Caller has validated and
    commits."""
    now = _now()
    inv = StaffInvitation(id=uuid.uuid4().hex, email=email, name=name, roles=sorted(set(roles)),
                          status=StaffInvitationStatus.pending, invited_by=invited_by, created_at=now,
                          expires_at=now + timedelta(days=access.load().invitation_expiry_days))
    db.add(inv)
    db.flush()
    clerk_error = _send_clerk(inv) if send else None
    _audit(db, inv, "staff_invited", invited_by, after={"email": email, "name": name, "roles": inv.roles,
                                                        "expires_at": inv.expires_at.isoformat(),
                                                        "clerk_status": inv.clerk_status,
                                                        "delivery": delivery_of(inv), "clerk_error": clerk_error})
    db.flush()
    return inv


def get(db: Session, invitation_id: str) -> StaffInvitation:
    inv = db.get(StaffInvitation, invitation_id)
    if inv is None:
        raise StaffError("Invitation not found.", code="invitation_not_found", status_code=404)
    return inv


def resend(db: Session, actor, invitation_id: str) -> StaffInvitation:
    """A fresh link and expiry. Clerk has no resend: the old Clerk invitation is revoked
    and a new one created."""
    from app.auth import clerk_client

    inv = get(db, invitation_id)
    check_scope(actor, set(inv.roles), verb="invite")
    if inv.status not in (StaffInvitationStatus.pending, StaffInvitationStatus.expired):
        raise StaffError(f"This invitation is {status_of(inv)}; only a pending or expired one can be resent.",
                         code="invitation_not_pending")
    if inv.status == StaffInvitationStatus.expired:
        other = db.execute(select(StaffInvitation.id).where(StaffInvitation.email == inv.email,
                                                            StaffInvitation.status == StaffInvitationStatus.pending,
                                                            StaffInvitation.id != inv.id)).first()
        if other:
            raise StaffError("That address already has another pending invitation.", code="invitation_exists")
    if inv.clerk_invitation_id:
        try:
            clerk_client.revoke_invitation(inv.clerk_invitation_id)
        except clerk_client.ClerkSyncError:
            # Already accepted, revoked or expired in Clerk: the new one replaces it anyway.
            logger.warning("Clerk revoke before resend failed", extra={"invitation_id": inv.id})
    before = {"expires_at": inv.expires_at.isoformat(), "status": status_of(inv)}
    now = _now()
    inv.status = StaffInvitationStatus.pending
    inv.expires_at = now + timedelta(days=access.load().invitation_expiry_days)
    inv.resent_at = now
    db.flush()
    clerk_error = _send_clerk(inv)
    _audit(db, inv, "invitation_resent", actor.employee_id, before=before,
           after={"expires_at": inv.expires_at.isoformat(), "status": "pending", "clerk_status": inv.clerk_status,
                  "delivery": delivery_of(inv), "clerk_error": clerk_error})
    db.flush()
    return inv


def revoke(db: Session, actor, invitation_id: str) -> tuple[StaffInvitation, Optional[str]]:
    """Revoked here and in Clerk. The database decides: even if Clerk's revoke fails, a
    revoked invitation is never accepted (its id in the user's metadata also keeps them
    off the bootstrap path)."""
    from app.auth import clerk_client

    inv = get(db, invitation_id)
    check_scope(actor, set(inv.roles), verb="invite")
    if inv.status not in (StaffInvitationStatus.pending, StaffInvitationStatus.expired):
        raise StaffError(f"This invitation is {status_of(inv)} and cannot be revoked.", code="invitation_not_pending")
    clerk_error = None
    if inv.clerk_invitation_id:
        try:
            clerk_client.revoke_invitation(inv.clerk_invitation_id)
        except clerk_client.ClerkSyncError as exc:
            clerk_error = str(exc)
    before = {"status": status_of(inv)}
    inv.status = StaffInvitationStatus.revoked
    inv.revoked_by = actor.employee_id
    inv.revoked_at = _now()
    _audit(db, inv, "invitation_revoked", actor.employee_id, before=before,
           after={"status": "revoked", "clerk_error": clerk_error})
    db.flush()
    return inv, clerk_error


def list_all(db: Session, status: Optional[str] = None) -> list[StaffInvitation]:
    rows = list(db.execute(select(StaffInvitation).order_by(StaffInvitation.created_at.desc())).scalars())
    return [r for r in rows if status is None or status_of(r) == status]


# ── First sign-in ────────────────────────────────────────────────────────────

def _verified_emails(employee_id: str, payload: dict) -> set[str]:
    """Clerk's verified addresses for this user. Without a Clerk secret key (development
    and test only) the token's email claim stands in."""
    from app.auth import clerk_client

    try:
        emails = clerk_client.verified_emails(employee_id)
    except clerk_client.ClerkSyncError:
        raise HTTPException(status_code=503, detail={
            "code": "identity_check_unavailable",
            "message": "We could not confirm your email address with Clerk. Try again in a minute."})
    if emails is not None:
        return set(emails)
    if settings.allows_dev_auth and payload.get("email"):
        return {normalise_email(payload["email"])}
    return set()


def _pending_for(db: Session, metadata: dict, verified: set[str]) -> Optional[StaffInvitation]:
    invitation_id = metadata.get("staff_invitation_id")
    if invitation_id:
        inv = db.get(StaffInvitation, str(invitation_id))
        return inv if inv is not None and inv.email in verified else None
    if not verified:
        return None
    return db.execute(select(StaffInvitation).where(StaffInvitation.email.in_(sorted(verified)),
                                                    StaffInvitation.status.in_((StaffInvitationStatus.pending,
                                                                                StaffInvitationStatus.expired)))
                      .order_by(StaffInvitation.created_at.desc())).scalars().first()


def _create_employee(db: Session, employee_id: str, name: str, email: str, roles: list[str],
                     granted_by: str) -> Employee:
    if db.execute(select(Employee.id).where(func.lower(Employee.email) == email.lower())).first():
        raise HTTPException(status_code=403, detail={
            "code": "email_in_use",
            "message": "Another staff account already uses this email address. Ask a Grid Nova administrator."})
    emp = Employee(id=employee_id, name=name or employee_id, email=email, role=EmployeeRole(access.primary_role(roles)))
    db.add(emp)
    db.flush()
    for role in roles:
        db.add(EmployeeRoleGrant(employee_id=employee_id, role=role, granted_by=granted_by))
    db.add(AuditLog(entity_type="employee", entity_id=employee_id, action="created", performed_by="system",
                    before_value=None, after_value={"name": emp.name, "role": emp.role.value}))
    for role in roles:
        db.add(AuditLog(entity_type="employee", entity_id=employee_id, action="role_granted", performed_by=granted_by,
                        before_value={"roles": []}, after_value={"roles": roles, "role": role}))
    db.flush()
    return emp


def first_sign_in(db: Session, employee_id: str, payload: dict, metadata: dict) -> Employee:
    """Create the employee for a Clerk user calling for the first time, or refuse."""
    verified = _verified_emails(employee_id, payload)
    inv = _pending_for(db, metadata, verified)
    if inv is not None:
        if inv.status == StaffInvitationStatus.pending and not _expired(inv):
            emp = _create_employee(db, employee_id, inv.name or payload.get("name", ""), inv.email, list(inv.roles),
                                   granted_by=inv.invited_by)
            inv.status = StaffInvitationStatus.accepted
            inv.accepted_employee_id = employee_id
            inv.accepted_at = _now()
            _audit(db, inv, "invitation_accepted", employee_id, before={"status": "pending"},
                   after={"status": "accepted", "employee_id": employee_id, "roles": list(inv.roles)})
            db.commit()
            return emp
        if _expired(inv) or inv.status == StaffInvitationStatus.expired:
            if inv.status == StaffInvitationStatus.pending:
                inv.status = StaffInvitationStatus.expired
                _audit(db, inv, "invitation_expired", "system", after={"status": "expired"})
                db.commit()
            raise HTTPException(status_code=403, detail=INVITATION_EXPIRED)
        raise HTTPException(status_code=403, detail=NOT_INVITED)
    # Bootstrap: a first Admin set up by hand in Clerk. Never for an invited user (their
    # metadata carries the invitation id), never any other role, and only for a VERIFIED
    # address on BOOTSTRAP_ADMIN_EMAILS (BE-21) — the token's own email claim is never enough.
    if not metadata.get("staff_invitation_id") and metadata.get("role") == EmployeeRole.admin.value:
        match = sorted(verified & settings.bootstrap_admin_email_set)
        if not match:
            db.add(AuditLog(entity_type="employee", entity_id=employee_id, action="bootstrap_refused",
                            performed_by=BOOTSTRAP, before_value=None,
                            after_value={"clerk_user_id": employee_id,
                                         "reason": "no verified address on BOOTSTRAP_ADMIN_EMAILS"}))
            db.commit()
            logger.warning("Bootstrap Admin refused: no verified address on the list",
                           extra={"clerk_user_id": employee_id})
            raise HTTPException(status_code=403, detail=NOT_INVITED)
        emp = _create_employee(db, employee_id, payload.get("name", ""), match[0], ["admin"], granted_by=BOOTSTRAP)
        db.add(AuditLog(entity_type="employee", entity_id=employee_id, action="bootstrap_admin",
                        performed_by=BOOTSTRAP, before_value=None,
                        after_value={"clerk_user_id": employee_id, "list_match": match[0]}))
        db.commit()
        logger.warning("Admin provisioned through the bootstrap path", extra={"clerk_user_id": employee_id})
        return emp
    raise HTTPException(status_code=403, detail=NOT_INVITED)
