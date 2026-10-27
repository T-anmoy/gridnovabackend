"""Role changes, deactivation, reactivation and lead handover.

The rules every path shares (the dashboard routes and the second approval of a pending
request alike):

  * scope    — the actor may touch only people whose roles, before and after, are all
               within the roles the actor may assign (config `can_assign_roles`): a
               Network Manager handles Sales, Engineer and Operations; an Admin anyone.
  * floor    — never fewer than `staff.min_active_admins` active Admins.
  * self     — you may not remove your own Admin role.
  * combine  — the `exclusive_roles` are held one at a time.

Clerk is told after the database decision: roles are copied to public_metadata (for
the frontend), a deactivated person is banned (Clerk revokes every session), a
reactivated one unbanned. The database is what stops them — on their next request.
"""
from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import access
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    AuditLog, Employee, Lead, PipelineStage, Proposal, ProposalStatus, StaffInvitation,
    StaffInvitationStatus,
)
from app.staff import StaffError

logger = logging.getLogger(__name__)

CLOSED_STAGES = (PipelineStage.won, PipelineStage.lost)
OPEN_PROPOSAL_STATUSES = (ProposalStatus.draft, ProposalStatus.pending_pricing_validation,
                          ProposalStatus.pending_approval, ProposalStatus.revision_requested)
# Second-approval switches (config approvals.require_second_approval) for role grants.
ROLE_APPROVAL_ACTIONS = {"admin": "role.admin", "accounts": "role.accounts"}


def _target(memory: MemoryManager, employee_id: str) -> Employee:
    target = memory.get_employee(employee_id)
    if target is None:
        raise StaffError("Employee not found.", code="employee_not_found", status_code=404)
    return target


def check_scope(actor, roles: set[str], *, verb: str) -> None:
    """403 unless every role in `roles` is one the actor may assign."""
    allowed = access.assignable_roles(actor.roles)
    outside = sorted(set(roles) - allowed)
    if outside:
        labels = ", ".join(access.role_label(r) for r in outside)
        raise StaffError(f"You cannot {verb} someone with the {labels} role.", code="role_not_assignable",
                         status_code=403, extra={"roles": outside})


def check_combination(roles: set[str]) -> None:
    problem = access.combination_problem(roles)
    if problem:
        raise StaffError(problem, code="role_combination_not_supported", status_code=422)


def check_admin_floor(memory: MemoryManager, employee_id: str) -> None:
    """409 when `employee_id` stopping being an active Admin would leave too few."""
    floor = access.load().min_active_admins
    if memory.count_active_admins(exclude_employee_id=employee_id) < floor:
        raise StaffError(f"Grid Nova must keep at least {floor} active Admins. Make someone else an Admin first.",
                         code="admin_floor", status_code=409, extra={"min_active_admins": floor})


def approval_actions(before: set[str], after: set[str]) -> list[str]:
    """The second-approval switches a change from `before` to `after` touches and that
    are on."""
    changed = before ^ after
    return [action for role, action in ROLE_APPROVAL_ACTIONS.items()
            if role in changed and access.second_approval_required(action)]


def validate_role_change(memory: MemoryManager, actor, target: Employee, roles: list[str]) -> tuple[set, set]:
    after = set(roles)
    check_combination(after)
    before = set(memory.employee_roles(target.id))
    check_scope(actor, before | after, verb="change the roles of")
    if target.id == actor.employee_id and "admin" in before and "admin" not in after:
        raise StaffError("You cannot remove your own Admin role. Ask another Admin.", code="own_admin_role",
                         status_code=409)
    if "admin" in before and "admin" not in after and target.is_active:
        check_admin_floor(memory, target.id)
    return before, after


def apply_role_change(db: Session, target: Employee, after: set[str], performed_by: str,
                      clerk_sync=None) -> dict:
    """Clerk first (a refusal changes nothing), then the database. Caller commits."""
    from app.auth import clerk_client

    ordered = sorted(after)
    primary = access.primary_role(ordered)
    sync = clerk_sync or (lambda: clerk_client.update_user_roles(target.id, ordered, primary))
    try:
        synced = sync()
    except clerk_client.ClerkSyncError:
        raise StaffError("Failed to update the roles in Clerk. No change applied.", code="clerk_sync_failed",
                         status_code=502)
    before, now = MemoryManager(db).replace_employee_roles(target.id, ordered, performed_by)
    return {"employee_id": target.id, "roles": now, "role": primary, "previous_roles": before,
            "clerk_synced": synced}


# ── Deactivation and handover ────────────────────────────────────────────────

def handover(db: Session, employee_id: str) -> dict:
    """What a leaver leaves behind: open leads assigned to them, open proposals they
    drafted, and staff invitations they sent that are still pending."""
    leads = list(db.execute(select(Lead).where(Lead.assigned_employee_id == employee_id,
                                               Lead.pipeline_stage.not_in(CLOSED_STAGES))
                            .order_by(Lead.id)).scalars())
    proposals = list(db.execute(select(Proposal).where(Proposal.drafted_by == employee_id,
                                                       Proposal.status.in_(OPEN_PROPOSAL_STATUSES))
                                .order_by(Proposal.id)).scalars())
    invites = list(db.execute(select(StaffInvitation).where(StaffInvitation.invited_by == employee_id,
                                                            StaffInvitation.status == StaffInvitationStatus.pending)
                              .order_by(StaffInvitation.created_at)).scalars())
    return {
        "open_leads": [{"lead_id": lead.id, "pipeline_stage": lead.pipeline_stage.value} for lead in leads],
        "open_proposals": [{"proposal_id": p.id, "lead_id": p.lead_id, "status": ProposalStatus(p.status).value}
                           for p in proposals],
        "pending_invitations": [{"invitation_id": i.id, "email": i.email} for i in invites],
    }


def validate_deactivation(memory: MemoryManager, actor, target: Employee) -> None:
    roles = set(memory.employee_roles(target.id))
    check_scope(actor, roles, verb="deactivate")
    if target.id == actor.employee_id:
        raise StaffError("You cannot deactivate yourself.", code="self_deactivation", status_code=409)
    if "admin" in roles:
        check_admin_floor(memory, target.id)


def ban_in_clerk(db: Session, employee_id: str, performed_by: str) -> dict:
    """After the deactivation committed: Clerk bans the user, which revokes every session
    and refuses sign-in. A failure leaves the deactivation standing (the database already
    refuses every request) and is reported for a retry."""
    from app.auth import clerk_client

    try:
        banned = clerk_client.ban_user(employee_id)
    except clerk_client.ClerkSyncError as exc:
        logger.error("Clerk ban failed after deactivation", extra={"employee_id": employee_id})
        return {"clerk_banned": False, "sessions_revoked": False, "clerk_error": str(exc)}
    if banned:
        db.add(AuditLog(entity_type="employee", entity_id=employee_id, action="sessions_revoked",
                        performed_by=performed_by, before_value=None,
                        after_value={"clerk": "banned", "sessions": "all revoked"}))
        db.commit()
    return {"clerk_banned": banned, "sessions_revoked": banned, "clerk_error": None}


def unban_in_clerk(employee_id: str) -> dict:
    from app.auth import clerk_client

    try:
        return {"clerk_unbanned": clerk_client.unban_user(employee_id), "clerk_error": None}
    except clerk_client.ClerkSyncError as exc:
        logger.error("Clerk unban failed after reactivation", extra={"employee_id": employee_id})
        return {"clerk_unbanned": False, "clerk_error": str(exc)}


def can_be_assigned_leads(memory: MemoryManager, employee: Employee) -> bool:
    return employee.is_active and "leads.be_assigned" in access.permissions_for(memory.employee_roles(employee.id))


def reassign_open_leads(db: Session, actor, from_employee_id: str, to_employee_id: str) -> dict:
    """Move every open lead of one employee to another who may be assigned leads.
    Closed (won/lost) leads keep their history. Caller commits."""
    memory = MemoryManager(db)
    _target(memory, from_employee_id)
    to = _target(memory, to_employee_id)
    if to.id == from_employee_id:
        raise StaffError("Choose a different employee to receive the leads.", code="same_employee", status_code=422)
    if not can_be_assigned_leads(memory, to):
        raise StaffError("Leads can only be assigned to active employees who hold a role that takes leads "
                         "(Admin or Sales).", code="not_assignable", status_code=422)
    lead_ids = list(db.execute(select(Lead.id).where(Lead.assigned_employee_id == from_employee_id,
                                                     Lead.pipeline_stage.not_in(CLOSED_STAGES))
                               .order_by(Lead.id)).scalars())
    for lead_id in lead_ids:
        memory.reassign_lead(lead_id, to.id, performed_by=actor.employee_id)
    db.add(AuditLog(entity_type="employee", entity_id=from_employee_id, action="leads_reassigned",
                    performed_by=actor.employee_id, before_value={"assigned_employee_id": from_employee_id,
                                                                  "lead_ids": lead_ids},
                    after_value={"assigned_employee_id": to.id, "lead_ids": lead_ids}))
    db.flush()
    return {"from_employee_id": from_employee_id, "to_employee_id": to.id, "lead_ids": lead_ids,
            "moved": len(lead_ids)}

