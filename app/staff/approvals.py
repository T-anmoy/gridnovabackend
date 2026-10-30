"""Maker–checker: a sensitive change waits for a second, different person.

config/access_control.yaml `approvals.require_second_approval` switches each kind on or
off without code. While a request is pending nothing changes; the requester is told it
is waiting. On approval the change is re-checked with the approver's own permissions and
applied exactly as the requester asked; a change that would no longer be allowed (the
person's roles moved on, the Admin floor, a commission rule) is refused and the request
stays pending. Requests expire after `approvals.expiry_hours`.

Kinds:
  staff.set_roles   — a role change touching Admin or Accounts (switches role.admin /
                      role.accounts). Approver: staff.manage over the roles involved.
  staff.invite      — an invitation granting Admin or Accounts. Same approver rule.
  commissions.standard_rate_publish   — approver: commissions.rates.manage.
  commissions.individual_rate_change  — approver: commissions.rates.manage.
  payouts.batch_approve               — approver: commissions.process.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import access
from app.memory.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from app.staff import StaffError

# action → the permission the approver must hold
KINDS = {
    "staff.set_roles": "staff.manage",
    "staff.invite": "staff.manage",
    "commissions.standard_rate_publish": "commissions.rates.manage",
    "commissions.individual_rate_change": "commissions.rates.manage",
    "payouts.batch_approve": "commissions.process",
}
WAITING = "Waiting for a second approver. Nothing changes until another person with this permission approves it."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, req: ApprovalRequest, action: str, performed_by: str, before=None, after=None) -> None:
    db.add(AuditLog(entity_type="approval_request", entity_id=req.id, action=action, performed_by=performed_by,
                    before_value=before, after_value=after))


def is_expired(req: ApprovalRequest) -> bool:
    return req.status == ApprovalRequestStatus.pending and req.expires_at <= _now()


def status_of(req: ApprovalRequest) -> str:
    return ApprovalRequestStatus.expired.value if is_expired(req) else ApprovalRequestStatus(req.status).value


def serialize(req: ApprovalRequest, names: Optional[dict] = None) -> dict:
    """`names` (employee id → display name) fills `requested_by_name` (BE-19); null when
    not supplied or unknown."""
    def iso(v):
        return v.isoformat() if v else None
    return {"approval_request_id": req.id, "action": req.action, "summary": req.summary, "payload": req.payload,
            "status": status_of(req), "requested_by": req.requested_by,
            "requested_by_name": (names or {}).get(req.requested_by), "created_at": iso(req.created_at),
            "expires_at": iso(req.expires_at), "decided_by": req.decided_by, "decided_at": iso(req.decided_at),
            "decision_note": req.decision_note, "result": req.result}


def pending_response(req: ApprovalRequest) -> dict:
    return {"status": "pending_approval", "message": WAITING, "approval_request": serialize(req)}


def eligible_approvers(db: Session, req: ApprovalRequest) -> list:
    """Active employees (not the requester) who could approve this request."""
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Employee

    memory = MemoryManager(db)
    permission = KINDS[req.action]
    roles_involved = set(req.payload.get("roles") or []) | set(req.payload.get("before") or [])
    out = []
    for emp in db.execute(select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.id)).scalars():
        if emp.id == req.requested_by:
            continue
        held = memory.employee_roles(emp.id)
        if permission not in access.permissions_for(held):
            continue
        if req.action.startswith("staff.") and not roles_involved <= access.assignable_roles(held):
            continue
        out.append(emp)
    return out


def could_decide(actor, req: ApprovalRequest) -> bool:
    """Whether `actor` could approve or reject this request (BE-19, for the list): not the
    requester, holds the approver permission and — for a staff change — may assign every
    role it touches (the Network Manager is never shown an Admin or Accounts grant)."""
    if req.requested_by == actor.employee_id or not actor.holds(KINDS[req.action]):
        return False
    if req.action.startswith("staff."):
        involved = set(req.payload.get("roles") or []) | set(req.payload.get("before") or [])
        return involved <= access.assignable_roles(actor.role_values)
    return True


def requester_names(db: Session, rows) -> dict:
    from app.memory.models import Employee
    ids = {r.requested_by for r in rows}
    if not ids:
        return {}
    return dict(db.execute(select(Employee.id, Employee.name).where(Employee.id.in_(ids))).all())


def request(db: Session, actor, action: str, summary: str, payload: dict) -> ApprovalRequest:
    """Record a pending request and tell the eligible approvers. Caller commits."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module

    if action not in KINDS:
        raise ValueError(action)
    now = _now()
    req = ApprovalRequest(id=uuid.uuid4().hex, action=action, summary=summary, payload=payload,
                          status=ApprovalRequestStatus.pending, requested_by=actor.employee_id, created_at=now,
                          expires_at=now + timedelta(hours=access.load().approval_expiry_hours))
    db.add(req)
    db.flush()
    _audit(db, req, "approval_requested", actor.employee_id, after={"action": action, "payload": payload})
    from app.notifications.alerts import _usable

    recipients = [e.email for e in eligible_approvers(db, req) if _usable(e.email)]
    event = {"approval_request_id": req.id, "action": action, "summary": summary, "requested_by": actor.employee_id,
             "expires_at": req.expires_at.isoformat(), "recipients": recipients}
    MemoryManager(db).notify_after_commit(lambda: notifier_module.notifier.notify("approval_requested", event),
                                          f"approval requested {req.id}")
    return req


def get(db: Session, request_id: str) -> ApprovalRequest:
    req = db.get(ApprovalRequest, request_id)
    if req is None:
        raise StaffError("Approval request not found.", code="approval_not_found", status_code=404)
    return req


def list_all(db: Session, status: Optional[str] = None) -> list[ApprovalRequest]:
    rows = list(db.execute(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())).scalars())
    return [r for r in rows if status is None or status_of(r) == status]


def _decidable(db: Session, actor, req: ApprovalRequest) -> None:
    if req.status != ApprovalRequestStatus.pending:
        raise StaffError(f"This request is already {status_of(req)}.", code="approval_not_pending")
    if is_expired(req):
        req.status = ApprovalRequestStatus.expired
        _audit(db, req, "approval_expired", "system", after={"status": "expired"})
        db.commit()
        raise StaffError("This request has expired. Ask for the change again.", code="approval_expired")
    if req.requested_by == actor.employee_id:
        raise StaffError("A second, different person must decide this request.", code="self_approval",
                         status_code=403)
    if not actor.has(KINDS[req.action]):
        raise StaffError("You do not hold the permission this request needs.", code="not_an_approver",
                         status_code=403)


def approve(db: Session, actor, request_id: str, note: Optional[str] = None) -> ApprovalRequest:
    """Re-check and apply the change as requested; mark approved. Caller commits; a
    refused change rolls back (the request stays pending)."""
    req = get(db, request_id)
    _decidable(db, actor, req)
    result = EXECUTORS[req.action](db, actor, req)
    req.status = ApprovalRequestStatus.approved
    req.decided_by = actor.employee_id
    req.decided_at = _now()
    req.decision_note = (note or "").strip() or None
    req.result = result
    _audit(db, req, "approval_approved", actor.employee_id, before={"status": "pending"},
           after={"status": "approved", "result": result})
    db.flush()
    return req


def reject(db: Session, actor, request_id: str, note: Optional[str] = None) -> ApprovalRequest:
    req = get(db, request_id)
    _decidable(db, actor, req)
    req.status = ApprovalRequestStatus.rejected
    req.decided_by = actor.employee_id
    req.decided_at = _now()
    req.decision_note = (note or "").strip() or None
    _audit(db, req, "approval_rejected", actor.employee_id, before={"status": "pending"},
           after={"status": "rejected", "note": req.decision_note})
    db.flush()
    return req


def cancel(db: Session, actor, request_id: str) -> ApprovalRequest:
    """The requester withdraws their own pending request."""
    req = get(db, request_id)
    if req.requested_by != actor.employee_id:
        raise StaffError("Only the person who asked can withdraw a request.", code="not_requester", status_code=403)
    if req.status != ApprovalRequestStatus.pending:
        raise StaffError(f"This request is already {status_of(req)}.", code="approval_not_pending")
    req.status = ApprovalRequestStatus.cancelled
    req.decided_by = actor.employee_id
    req.decided_at = _now()
    _audit(db, req, "approval_cancelled", actor.employee_id, before={"status": "pending"},
           after={"status": "cancelled"})
    db.flush()
    return req


def cancel_for_leaver(db: Session, employee_id: str, performed_by: str) -> list[str]:
    """A deactivated person's own pending requests are withdrawn. Caller commits."""
    out = []
    for req in db.execute(select(ApprovalRequest).where(ApprovalRequest.requested_by == employee_id,
                                                        ApprovalRequest.status == ApprovalRequestStatus.pending)
                          ).scalars():
        req.status = ApprovalRequestStatus.cancelled
        req.decided_by = performed_by
        req.decided_at = _now()
        req.decision_note = "Requester deactivated."
        _audit(db, req, "approval_cancelled", performed_by, before={"status": "pending"},
               after={"status": "cancelled", "reason": "requester_deactivated"})
        out.append(req.id)
    db.flush()
    return out


# ── Executors: apply the requested change, checked with the approver's permissions ──

def _exec_set_roles(db: Session, actor, req: ApprovalRequest) -> dict:
    from app.memory.memory_manager import MemoryManager
    from app.staff import roles as staff_roles

    memory = MemoryManager(db)
    target = memory.get_employee(req.payload["employee_id"])
    if target is None:
        raise StaffError("That employee no longer exists.", code="employee_not_found", status_code=404)
    if sorted(memory.employee_roles(target.id)) != sorted(req.payload["before"]):
        raise StaffError("This person's roles changed after the request was made. Reject it and ask again.",
                         code="approval_stale")
    # The requester's own scope was checked when they asked; now the approver's.
    staff_roles.validate_role_change(memory, actor, target, req.payload["roles"])
    return staff_roles.apply_role_change(db, target, set(req.payload["roles"]), performed_by=req.requested_by)


def _exec_invite(db: Session, actor, req: ApprovalRequest) -> dict:
    from app.staff import invitations

    p = req.payload
    name, email, roles = invitations.validate_new(db, actor, p["name"], p["email"], p["roles"])
    inv = invitations.issue(db, name=name, email=email, roles=roles, invited_by=req.requested_by)
    return invitations.serialize(inv)


def _exec_rate_publish(db: Session, actor, req: ApprovalRequest) -> dict:
    from app.commissions import periods, rates

    p = req.payload
    today = periods.today_ist()
    schedule = _translate(lambda: rates.publish(db, effective_from=date.fromisoformat(p["effective_from"]),
                                                is_placeholder=p["is_placeholder"], rates=p["rates"], note=p.get("note"),
                                                actor=req.requested_by, today=today))
    return rates.schedule_json(schedule, rates.rates_of(db, schedule.id), today)


def _exec_individual_rate(db: Session, actor, req: ApprovalRequest) -> dict:
    from app.commissions import periods, rates
    from app.memory.models import Associate

    p = req.payload
    associate = db.execute(select(Associate).where(Associate.associate_id == p["associate_id"])).scalar_one_or_none()
    if associate is None:
        raise StaffError("That associate no longer exists.", code="associate_not_found", status_code=404)
    today = periods.today_ist()
    row = _translate(lambda: rates.set_individual(db, associate, rate_percent=p["rate_percent"],
                                                  effective_from=date.fromisoformat(p["effective_from"]),
                                                  reason=p.get("reason"), actor=req.requested_by, today=today))
    return {"associate_id": p["associate_id"], "rate_change_id": row.id}


def _exec_batch_approve(db: Session, actor, req: ApprovalRequest) -> dict:
    from app.commissions import payouts

    p = req.payload
    batch = _translate(lambda: payouts.approve(db, p["batch_id"], req.requested_by,
                                               is_admin=bool(p.get("segregation_override")), reason=p.get("reason"),
                                               tds_review_acknowledged=bool(p.get("tds_review_acknowledged"))))
    return {"batch_id": batch.id, "status": batch.status.value}


def _translate(fn: Callable):
    from app.associates.service import AssociateError

    try:
        return fn()
    except AssociateError as exc:
        raise StaffError(exc.message, code=exc.code, status_code=exc.status_code)


EXECUTORS = {
    "staff.set_roles": _exec_set_roles,
    "staff.invite": _exec_invite,
    "commissions.standard_rate_publish": _exec_rate_publish,
    "commissions.individual_rate_change": _exec_individual_rate,
    "payouts.batch_approve": _exec_batch_approve,
}
