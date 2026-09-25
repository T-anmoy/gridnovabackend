"""
Stale-lead detection job.

Finds open leads (any stage but won/lost) nobody has worked on for a while and fires
an internal notification so a human follows up — we never message the customer
directly (no outbound account exists). "Worked on" is the lead's latest activity:
its creation, a customer message, or anything on its timeline or audit trail (stage
change, assignment, notes, meetings, proposal actions, site assessments) — see
MemoryManager.last_activity_by_lead.

Dedup uses Lead.stale_notified_at compared against each threshold's boundary
(last activity + threshold), so:
  - each threshold notifies at most once per quiet period,
  - a lead escalates from 48h to 5d across runs,
  - any new activity (which moves the last-activity time forward) re-arms both.

Per lead, the existing log/Slack event still fires; on top of that, one digest per
run goes to staff email when anything went stale (nothing is sent otherwise).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.memory import reflection
from app.memory.memory_manager import MemoryManager
from app.memory.models import PipelineStage
from app.timezones import format_ist_datetime

logger = logging.getLogger(__name__)

STALE_48H = timedelta(hours=48)
STALE_5D = timedelta(days=5)

# Every open stage: only won and lost are finished.
STALE_ELIGIBLE_STAGES = tuple(s for s in PipelineStage if s not in (PipelineStage.won, PipelineStage.lost))


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite can hand back naive datetimes — treat those as UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def check_stale_leads(db: Session, now: Optional[datetime] = None) -> dict:
    """Scan open leads and fire staleness notifications. Idempotent — safe to call
    repeatedly. Returns a summary count dict."""
    now = now or datetime.now(timezone.utc)
    from app.notifications.notifier import notifier

    memory = MemoryManager(db)
    counts = {"lead_stale_48h": 0, "lead_stale_5d": 0, "checked": 0}
    newly_quiet: list[int] = []
    digest: list[dict] = []

    leads = memory.list_stale_candidate_leads(STALE_ELIGIBLE_STAGES)
    last_activity = memory.last_activity_by_lead(leads)
    for lead in leads:
        counts["checked"] += 1
        last = last_activity.get(lead.id)
        if last is None:
            continue
        notified_at = _as_utc(lead.stale_notified_at)
        boundary_48h = last + STALE_48H
        boundary_5d = last + STALE_5D

        # Check the more severe threshold first so a long-quiet, never-notified lead
        # gets the 5d alert rather than a stale 48h one.
        fired: Optional[str] = None
        if now >= boundary_5d and (notified_at is None or notified_at < boundary_5d):
            fired = "lead_stale_5d"
        elif now >= boundary_48h and (notified_at is None or notified_at < boundary_48h):
            fired = "lead_stale_48h"

        if not fired:
            continue
        last_msg = _as_utc(lead.last_customer_message_at)
        payload = {
            "lead_id": lead.id,
            "last_activity_at": format_ist_datetime(last),
            "last_customer_message_at": format_ist_datetime(last_msg) if last_msg else None,
            "pipeline_stage": lead.pipeline_stage.value,
        }
        memory.notify_after_commit(
            lambda event=fired, payload=payload: notifier.notify(event, payload), fired,
        )
        memory.mark_stale_notified(lead.id, now, threshold=fired, performed_by="stale_lead_job")
        counts[fired] += 1
        digest.append({
            "lead_id": lead.id,
            "name": (lead.customer.name if lead.customer else None)
                    or (lead.collected_fields or {}).get("name"),
            "source": lead.source.value,
            "stage": lead.pipeline_stage.value,
            "days_quiet": round((now - last).total_seconds() / 86_400, 1),
            "assigned_employee_id": lead.assigned_employee_id,
            "threshold": fired,
        })
        if last_msg is not None:
            newly_quiet.append(lead.id)

    if digest:
        memory.notify_after_commit(lambda: notifier.stale_lead_digest(digest), "stale_lead_digest")
    db.commit()

    # A conversation going quiet is the third reflection milestone: this is the
    # moment to write down what it was about. Only for leads that had a conversation
    # (a lead nobody ever messaged has nothing to reflect on), fired after the commit,
    # and only on the run that first notices — mark_stale_notified above is what stops
    # the same quiet period reflecting twice.
    for lead_id in newly_quiet:
        try:
            reflection.reflect_on_lead(
                MemoryManager(db), lead_id,
                trigger=reflection.TRIGGER_LEAD_QUIET, performed_by="stale_lead_job",
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Reflection on quiet lead %s failed", lead_id)

    return counts
