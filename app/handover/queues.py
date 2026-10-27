"""Handover and service-request counts for the dashboard's navigation badges
(GET /api/v1/dashboard/work-queues → `handover`, `service_requests`). SQL only: one
statement each, whatever the data — tests/test_handover_queues.py holds the budget.

    awaiting_handover   commissioned, not yet handed over
    packs_incomplete    of those, a manual item is still outstanding (orientation,
                        customer acknowledgement or O&M contact) — the computed items
                        (documents, register, payment) are not totalled here because they
                        need per-project reads; the pack endpoint lists everything
    open / overdue      service requests open or in progress / past their SLA
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session

from app.auth.scoping import lead_scope, narrowed_to_assigned
from app.memory.models import (
    CustomerOnboarding, HandoverPack, Lead, ProjectExecution, ProjectStage, ProjectStageEvent,
    ServiceRequest,
)
from app.handover.requests import OPEN


def _stage(stage: ProjectStage):
    return exists().where(ProjectStageEvent.project_id == ProjectExecution.project_id, ProjectStageEvent.stage == stage)


def counts(db: Session, employee) -> dict:
    sales = narrowed_to_assigned(employee)
    incomplete = case((or_(HandoverPack.id.is_(None), HandoverPack.orientation_done_at.is_(None),
                           HandoverPack.acknowledged_at.is_(None), HandoverPack.om_contact_given_at.is_(None)), 1),
                      else_=0)
    q = (select(func.count(), func.coalesce(func.sum(incomplete), 0)).select_from(ProjectExecution)
         .join(CustomerOnboarding, CustomerOnboarding.id == ProjectExecution.onboarding_id)
         .outerjoin(HandoverPack, HandoverPack.project_id == ProjectExecution.project_id)
         .where(_stage(ProjectStage.commissioned), ~_stage(ProjectStage.handover_complete)))
    if sales:
        q = q.join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    awaiting, incomplete_n = db.execute(q).one()

    now = datetime.now(timezone.utc)
    overdue = case((ServiceRequest.sla_due_at.is_not(None) & (ServiceRequest.sla_due_at < now), 1), else_=0)
    q = select(func.count(), func.coalesce(func.sum(overdue), 0)).select_from(ServiceRequest).where(
        ServiceRequest.status.in_(OPEN))
    if sales:
        q = (q.join(CustomerOnboarding, CustomerOnboarding.id == ServiceRequest.onboarding_id)
             .join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition()))
    open_n, overdue_n = db.execute(q).one()
    return {"handover": {"awaiting_handover": int(awaiting), "packs_incomplete": int(incomplete_n)},
            "service_requests": {"open": int(open_n), "overdue": int(overdue_n)}}
