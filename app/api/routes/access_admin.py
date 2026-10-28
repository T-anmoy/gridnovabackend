"""Access control (BE-17): who can do what, staff invitations, role changes, handover,
regions and second approvals. Contracts: docs/api/access_control.md.

Errors carry {code, message}: 403 role_not_assignable / not_an_approver / self_approval /
two_factor_required, 404 employee_not_found / invitation_not_found / approval_not_found,
409 admin_floor / own_admin_role / self_deactivation / already_staff / invitation_exists /
invitation_not_pending / approval_not_pending / approval_expired / approval_stale,
422 role_combination_not_supported / invalid_email / invalid_name / not_assignable /
unknown_region, 502 clerk_sync_failed / clerk_invitation_failed.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import access
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import EmployeeRole, TerritoryState
from app.notifications import alerts as staff_alerts
from app.staff import StaffError, approvals, invitations
from app.staff import roles as staff_roles

router = APIRouter(prefix="/api/v1/dashboard")


def _fail(exc: StaffError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _run(db: Session, fn):
    try:
        result = fn()
    except StaffError as exc:
        db.rollback()
        raise _fail(exc)
    db.commit()
    return result


def _staff_manager(employee: AuthenticatedEmployee) -> None:
    if not employee.has("staff.manage"):
        raise HTTPException(status_code=403, detail="Your role cannot manage staff.")


# ── Who can do what ────────────────────────────────────────────────────────────

@router.get("/access/matrix")
def access_matrix(employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Every role × permission with labels and descriptions (config/access_control.yaml)
    — the "Who can do what" page. Any staff member."""
    return access.matrix()


@router.get("/access/alerts")
def access_alerts(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Which roles receive each staff alert, and whether each currently reaches anyone.
    Any staff member sees counts; a viewer with staff.manage also gets `addresses` (BE-19)."""
    return {"events": staff_alerts.describe(db, with_addresses=employee.holds("staff.manage"))}


@router.get("/access/admins")
def access_admins(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """BE-19: the active Admins, so "ask an Admin" can name them. Any staff member;
    `email` only for a viewer who is an Admin (null otherwise)."""
    memory = MemoryManager(db)
    show_email = "admin" in employee.role_values
    people = sorted(memory.holders_of_role("admin"), key=lambda e: ((e.name or "").lower(), e.id))
    return {"items": [{"id": e.id, "name": e.name, "email": e.email if show_email else None} for e in people]}


# ── Invitations ────────────────────────────────────────────────────────────────

class InvitationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    roles: list[EmployeeRole] = Field(min_length=1, max_length=7)


@router.post("/staff/invitations", status_code=201, responses={202: {"description": "Waiting for a second approval (pending_approval)"}})
def create_invitation(body: InvitationIn, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Invite a person with the given roles (each one you may assign). An invitation
    granting Admin or Accounts waits for a second Admin (202 pending_approval)."""
    _staff_manager(employee)
    roles = [r.value for r in body.roles]

    def act():
        name, email, wanted = invitations.validate_new(db, employee, body.name, body.email, roles)
        actions = invitations.approval_actions(wanted)
        if actions:
            req = approvals.request(db, employee, "staff.invite",
                                    f"Invite {name} <{email}> as {', '.join(access.role_label(r) for r in wanted)}",
                                    {"name": name, "email": email, "roles": wanted, "switches": actions})
            return 202, approvals.pending_response(req)
        return 201, invitations.serialize(invitations.issue(db, name=name, email=email, roles=wanted,
                                                            invited_by=employee.employee_id))
    code, payload = _run(db, act)
    return JSONResponse(status_code=code, content=payload)


@router.get("/staff/invitations")
def list_invitations(status: Optional[str] = Query(default=None, pattern="^(pending|accepted|revoked|expired)$"),
                     db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _staff_manager(employee)
    return {"items": [invitations.serialize(i) for i in invitations.list_all(db, status)]}


@router.post("/staff/invitations/{invitation_id}/resend")
def resend_invitation(invitation_id: str, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _staff_manager(employee)
    return invitations.serialize(_run(db, lambda: invitations.resend(db, employee, invitation_id)))


@router.delete("/staff/invitations/{invitation_id}")
def revoke_invitation(invitation_id: str, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _staff_manager(employee)
    inv, clerk_error = _run(db, lambda: invitations.revoke(db, employee, invitation_id))
    return {**invitations.serialize(inv), "clerk_error": clerk_error}


# ── Roles ──────────────────────────────────────────────────────────────────────

class RolesIn(BaseModel):
    roles: list[EmployeeRole] = Field(min_length=1, max_length=7)


def change_roles(db: Session, employee: AuthenticatedEmployee, employee_id: str, roles: list[str],
                 clerk_sync=None) -> tuple[int, dict]:
    """The one role-change path (PUT …/roles and the older POST …/role). Returns
    (status, body): 200 applied, 202 waiting for a second approval."""
    memory = MemoryManager(db)

    def act():
        target = memory.get_employee(employee_id)
        if target is None:
            raise StaffError("Employee not found.", code="employee_not_found", status_code=404)
        before, after = staff_roles.validate_role_change(memory, employee, target, roles)
        if before == after:
            return 200, {"employee_id": target.id, "roles": sorted(after), "role": access.primary_role(after),
                         "previous_roles": sorted(before), "clerk_synced": False, "unchanged": True}
        actions = staff_roles.approval_actions(before, after)
        if actions:
            req = approvals.request(db, employee, "staff.set_roles",
                                    f"Change {target.name}'s roles from {', '.join(sorted(before))} to "
                                    f"{', '.join(sorted(after))}",
                                    {"employee_id": target.id, "roles": sorted(after), "before": sorted(before),
                                     "switches": actions})
            return 202, approvals.pending_response(req)
        return 200, staff_roles.apply_role_change(db, target, after, employee.employee_id, clerk_sync)
    return _run(db, act)


@router.put("/employees/{employee_id}/roles", responses={202: {"description": "Waiting for a second approval (pending_approval)"}})
def set_employee_roles(employee_id: str, body: RolesIn, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Replace a person's roles. Only roles you may assign, on people whose roles you may
    assign; never your own Admin role; never below the Admin floor. Granting or removing
    Admin or Accounts waits for a second Admin (202). Takes effect on their next request."""
    _staff_manager(employee)
    code, payload = change_roles(db, employee, employee_id, [r.value for r in body.roles])
    return JSONResponse(status_code=code, content=payload)


class RegionsIn(BaseModel):
    regions: list[str] = Field(default_factory=list, max_length=40)


@router.put("/employees/{employee_id}/regions")
def set_employee_regions(employee_id: str, body: RegionsIn, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The states a person works in (TerritoryState names); empty = every region. Applied
    to Network Manager associate lists (app/auth/scoping.py)."""
    _staff_manager(employee)
    memory = MemoryManager(db)

    def act():
        target = memory.get_employee(employee_id)
        if target is None:
            raise StaffError("Employee not found.", code="employee_not_found", status_code=404)
        staff_roles.check_scope(employee, set(memory.employee_roles(target.id)), verb="change the regions of")
        known = set(db.execute(select(TerritoryState.name)).scalars())
        wanted = sorted({r.strip() for r in body.regions if r.strip()})
        unknown = [r for r in wanted if r not in known]
        if unknown:
            raise StaffError(f"Unknown state(s): {', '.join(unknown)}.", code="unknown_region", status_code=422)
        before = target.regions or []
        target.regions = wanted or None
        memory._audit("employee", target.id, "regions_changed", employee.employee_id,
                      before={"regions": before}, after={"regions": wanted})
        return {"employee_id": target.id, "regions": wanted}
    return _run(db, act)


# ── Offboarding ────────────────────────────────────────────────────────────────

@router.get("/employees/{employee_id}/handover")
def employee_handover(employee_id: str, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """What this person leaves behind: open leads, open proposals they drafted, pending
    invitations they sent."""
    _staff_manager(employee)
    if MemoryManager(db).get_employee(employee_id) is None:
        raise HTTPException(status_code=404, detail={"code": "employee_not_found", "message": "Employee not found."})
    return {"employee_id": employee_id, **staff_roles.handover(db, employee_id)}


class ReassignIn(BaseModel):
    to_employee_id: str = Field(min_length=1, max_length=200)


@router.post("/employees/{employee_id}/reassign")
def reassign_leads(employee_id: str, body: ReassignIn, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Move every open lead of this person to another employee who takes leads. Needs
    staff management and lead assignment."""
    _staff_manager(employee)
    if not employee.can_assign_lead():
        raise HTTPException(status_code=403, detail="Your role cannot assign leads.")
    return _run(db, lambda: staff_roles.reassign_open_leads(db, employee, employee_id, body.to_employee_id))


# ── Second approvals ───────────────────────────────────────────────────────────

def _approver_permissions(employee: AuthenticatedEmployee) -> set[str]:
    return {p for p in set(approvals.KINDS.values()) if employee.holds(p)}


@router.get("/approvals")
def list_approvals(status: Optional[str] = Query(default=None,
                                                 pattern="^(pending|approved|rejected|cancelled|expired)$"),
                   db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Requests you asked for, or that you could decide (BE-19: a staff change only when
    you may assign every role it touches). `{items: [...]}`, newest first; each item
    carries `requested_by_name`."""
    held = _approver_permissions(employee)
    if not held:
        raise HTTPException(status_code=403, detail="Your role cannot see approval requests.")
    rows = [r for r in approvals.list_all(db, status)
            if r.requested_by == employee.employee_id or approvals.could_decide(employee, r)]
    names = approvals.requester_names(db, rows)
    return {"items": [approvals.serialize(r, names) for r in rows]}


class DecisionIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/approvals/{request_id}/approve")
def approve_request(request_id: str, body: Optional[DecisionIn] = None, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Apply the change, re-checked with your permissions. You cannot approve your own."""
    if not _approver_permissions(employee):
        raise HTTPException(status_code=403, detail="Your role cannot decide approval requests.")
    req = _run(db, lambda: approvals.approve(db, employee, request_id,
                                                                  body.note if body else None))
    return approvals.serialize(req, approvals.requester_names(db, [req]))


@router.post("/approvals/{request_id}/reject")
def reject_request(request_id: str, body: Optional[DecisionIn] = None, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not _approver_permissions(employee):
        raise HTTPException(status_code=403, detail="Your role cannot decide approval requests.")
    req = _run(db, lambda: approvals.reject(db, employee, request_id,
                                                                 body.note if body else None))
    return approvals.serialize(req, approvals.requester_names(db, [req]))


@router.post("/approvals/{request_id}/cancel")
def cancel_request(request_id: str, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Withdraw your own pending request."""
    if not _approver_permissions(employee):
        raise HTTPException(status_code=403, detail="Your role cannot see approval requests.")
    req = _run(db, lambda: approvals.cancel(db, employee, request_id))
    return approvals.serialize(req, approvals.requester_names(db, [req]))
