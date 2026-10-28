"""Moving a suspended or deactivated associate's customers to another associate (BE-19).

Attribution is never silently reassigned (Associate Terms A7): this is an explicit,
audited step an Admin or the Network Manager takes once the associate is no longer
active, choosing who looks after the customers from now on. Nothing about an active
associate's customers can be moved.

What moves: every customer record (onboarding) the associate owns that is not closed —
the new owner sees and works it in their portal from now on. What does not move:
history. Commission already recorded stays with the associate who earned it (frozen
while they are suspended, app/commissions/payouts.py); a payment verified after the
move earns the new owner (config/commissions.yaml reassigned_customers_earn, an
unconfirmed reading — registry C5-REASSIGN).

A project whose execution the associate runs (execution_mode associate) moves only to an
associate who may run execution (a Preferred Associate, or capability `execution`):
otherwise take its execution over first (execution mode switch) and move it then.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError, InvalidTransitionError, audit
from app.memory.models import (
    Associate, AssociateStatus, CustomerOnboarding, ExecutionMode, OnboardingStatus, ProjectExecution,
)


def reassign_customers(db: Session, source: Associate, target: Associate, performed_by: str, reason: Optional[str],
                       onboarding_ids: Optional[list[int]] = None) -> dict:
    """Caller commits. Returns {from, to, onboarding_ids, moved}."""
    from app.execution.service import mode_for

    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Give a reason.", code="reason_required", status_code=422, field="reason")
    if source.status == AssociateStatus.active:
        raise InvalidTransitionError("An active associate's customers cannot be moved: suspend or deactivate "
                                     "the associate first.", code="associate_active")
    if target.id == source.id:
        raise AssociateError("Choose a different associate to receive the customers.", code="same_associate",
                             status_code=422, field="to_associate_id")
    if target.status != AssociateStatus.active:
        raise AssociateError("Customers can only be moved to an active associate.", code="target_not_active",
                             status_code=422, field="to_associate_id")
    q = (select(CustomerOnboarding).where(CustomerOnboarding.owner_associate_id == source.id,
                                          CustomerOnboarding.status != OnboardingStatus.closed)
         .order_by(CustomerOnboarding.id).with_for_update())
    rows = list(db.execute(q).scalars())
    if onboarding_ids is not None:
        wanted = set(onboarding_ids)
        unknown = sorted(wanted - {o.id for o in rows})
        if unknown:
            raise AssociateError("Some of these customers are not open customers of this associate.",
                                 code="unknown_customers", status_code=422, field="onboarding_ids",
                                 extra={"onboarding_ids": unknown})
        rows = [o for o in rows if o.id in wanted]
    if not rows:
        raise AssociateError("This associate has no open customers to move.", code="nothing_to_move",
                             status_code=409)
    if mode_for(target) != ExecutionMode.associate:
        executing = db.execute(select(ProjectExecution.onboarding_id).where(
            ProjectExecution.onboarding_id.in_([o.id for o in rows]),
            ProjectExecution.execution_mode == ExecutionMode.associate)).scalars().all()
        if executing:
            raise AssociateError("These customers' projects are run by the associate; the chosen associate cannot "
                                 "run execution. Take execution over first, or choose an associate who can.",
                                 code="target_cannot_execute", status_code=409, field="to_associate_id",
                                 extra={"onboarding_ids": sorted(executing)})
    moved = []
    for onboarding in rows:
        onboarding.owner_associate_id = target.id
        moved.append(onboarding.id)
        audit(db, "customer_onboarding", str(onboarding.id), "owner_reassigned", performed_by,
              before={"owner_associate_id": source.associate_id},
              after={"owner_associate_id": target.associate_id, "reason": reason})
    db.flush()
    audit(db, "associate", source.associate_id, "customers_reassigned", performed_by,
          before={"owner_associate_id": source.associate_id, "onboarding_ids": moved},
          after={"owner_associate_id": target.associate_id, "onboarding_ids": moved, "reason": reason})
    return {"from_associate_id": source.associate_id, "to_associate_id": target.associate_id,
            "onboarding_ids": moved, "moved": len(moved)}


def open_customer_count(db: Session, associate: Associate) -> int:
    from sqlalchemy import func
    return db.execute(select(func.count()).select_from(CustomerOnboarding).where(
        CustomerOnboarding.owner_associate_id == associate.id,
        CustomerOnboarding.status != OnboardingStatus.closed)).scalar_one()
