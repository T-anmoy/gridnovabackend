"""Escalate associate-sourced customers whose staff verification call is overdue.

A submitted customer's call is due at response_due_at (its score band's response
target after submission — app/onboarding/scoring.py). Once overdue, each further
target period moves it one step up the chain of people responsible for the SITE's
territory, skipping steps nobody holds:

    1. block-level Preferred Associate(s) of the site's block
    2. District Head of the site's district
    3. State Head(s) of the site's state
    4. Admin (staff email)

The owning associate is never notified about their own customer. Notifications go
through the existing deferred notifier (sent only once this run commits). Idempotent:
each step is sent once; re-running does nothing until the next step is due. Recording
the verification call stops the clock (the customer leaves `submitted`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.notifications.notifier as notifier_module
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    Associate, AssociateStatus, AssociateType, AuditLog, CustomerOnboarding, OnboardingStatus, TerritoryLevel,
    TimelineEventType,
)
from app.onboarding import scoring

logger = logging.getLogger(__name__)
ADMIN = "admin"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _preferred(db: Session, level: TerritoryLevel, column, territory_id: Optional[int],
               exclude_pk: int) -> list[Associate]:
    if territory_id is None:
        return []
    return list(db.execute(select(Associate).where(
        Associate.associate_type == AssociateType.preferred_associate, Associate.status == AssociateStatus.active,
        Associate.level == level, column == territory_id, Associate.id != exclude_pk,
    ).order_by(Associate.id)).scalars())


def escalation_chain(db: Session, onboarding: CustomerOnboarding) -> list:
    """The non-empty steps, nearest first; Admin is always last."""
    owner = onboarding.owner_associate_id
    steps = [
        _preferred(db, TerritoryLevel.block, Associate.territory_block_id, onboarding.block_id, owner),
        _preferred(db, TerritoryLevel.district_head, Associate.territory_district_id, onboarding.district_id, owner),
        _preferred(db, TerritoryLevel.state_head, Associate.territory_state_id, onboarding.state_id, owner),
    ]
    return [s for s in steps if s] + [ADMIN]


def check_onboarding_escalations(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    memory = MemoryManager(db)
    escalated = []
    rows = db.execute(select(CustomerOnboarding).where(
        CustomerOnboarding.status == OnboardingStatus.submitted,
        CustomerOnboarding.response_due_at.isnot(None)).order_by(CustomerOnboarding.id)).scalars().all()
    for onboarding in rows:
        # The owner's track record may have moved since the last change to this customer.
        scoring.rescore(db, onboarding, "onboarding_escalation_job")
        due = _aware(onboarding.response_due_at)
        if now <= due:
            continue
        period = scoring.response_target(onboarding.score_band)
        steps_due = 1 + int((now - due) / period)
        chain = escalation_chain(db, onboarding)
        level = min(steps_due, len(chain))
        if level <= onboarding.escalation_level:
            continue
        target = chain[level - 1]
        before = onboarding.escalation_level
        onboarding.escalation_level = level
        onboarding.last_escalated_at = now
        overdue_hours = round((now - due).total_seconds() / 3600, 1)
        payload = {"onboarding_id": onboarding.id, "lead_id": onboarding.lead_id,
                   "band": onboarding.score_band.value, "score": onboarding.score,
                   "response_due_at": due.isoformat(), "overdue_hours": overdue_hours,
                   "escalation_level": level}
        if target == ADMIN:
            payload["escalated_to"] = ADMIN
        else:
            payload["escalated_to"] = "preferred_associates"
            payload["recipients"] = [{"associate_id": a.associate_id, "email": a.email} for a in target]
        db.add(AuditLog(entity_type="customer_onboarding", entity_id=str(onboarding.id), action="escalated",
                        performed_by="onboarding_escalation_job", before_value={"escalation_level": before},
                        after_value={k: v for k, v in payload.items() if k != "recipients"}
                        | {"to": [r["associate_id"] for r in payload.get("recipients", [])] or ADMIN}))
        who = "Admin" if target == ADMIN else ", ".join(a.associate_id for a in target)
        memory.add_timeline_event(lead_id=onboarding.lead_id, event_type=TimelineEventType.escalated,
                                  description=f"Verification call overdue by {overdue_hours}h — escalated to {who}",
                                  created_by="onboarding_escalation_job")
        memory.notify_after_commit(
            (lambda p=payload: notifier_module.notifier.notify("onboarding_overdue", p)),
            f"onboarding {onboarding.id} overdue (level {level})")
        escalated.append({"onboarding_id": onboarding.id, "escalation_level": level,
                          "escalated_to": payload["escalated_to"],
                          "recipients": [r["associate_id"] for r in payload.get("recipients", [])]})
    db.commit()
    logger.info("Onboarding escalation check: %d checked, %d escalated", len(rows), len(escalated))
    return {"checked": len(rows), "escalated": escalated}
