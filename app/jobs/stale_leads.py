"""
Stale-lead detection job.

Finds early-stage leads that have gone quiet and fires an internal notification so a
human follows up — we never message the customer directly (no outbound account exists).

Dedup uses Lead.stale_notified_at compared against each threshold's boundary
(last_customer_message_at + threshold), so:
  - each threshold notifies at most once per quiet period,
  - a lead escalates from 48h to 5d across runs,
  - a fresh customer reply (which moves last_customer_message_at forward) re-arms both.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.memory.memory_manager import MemoryManager
from app.memory.models import PipelineStage

logger = logging.getLogger(__name__)

STALE_48H = timedelta(hours=48)
STALE_5D = timedelta(days=5)

# "Not yet qualified/closed" — only leads still being worked at the top of the funnel.
STALE_ELIGIBLE_STAGES = (PipelineStage.new, PipelineStage.qualifying)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite can hand back naive datetimes — treat those as UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def check_stale_leads(db: Session, now: Optional[datetime] = None) -> dict:
    """Scan eligible leads and fire staleness notifications. Idempotent — safe to call
    repeatedly. Returns a summary count dict."""
    now = now or datetime.now(timezone.utc)
    from app.notifications.notifier import notifier

    memory = MemoryManager(db)
    counts = {"lead_stale_48h": 0, "lead_stale_5d": 0, "checked": 0}

    for lead in memory.list_stale_candidate_leads(STALE_ELIGIBLE_STAGES):
        counts["checked"] += 1
        last_msg = _as_utc(lead.last_customer_message_at)
        if last_msg is None:
            continue
        notified_at = _as_utc(lead.stale_notified_at)
        boundary_48h = last_msg + STALE_48H
        boundary_5d = last_msg + STALE_5D

        # Check the more severe threshold first so a long-quiet, never-notified lead
        # gets the 5d alert rather than a stale 48h one.
        fired: Optional[str] = None
        if now >= boundary_5d and (notified_at is None or notified_at < boundary_5d):
            fired = "lead_stale_5d"
        elif now >= boundary_48h and (notified_at is None or notified_at < boundary_48h):
            fired = "lead_stale_48h"

        if fired:
            notifier.notify(fired, {
                "lead_id": lead.id,
                "last_customer_message_at": last_msg.isoformat(),
                "pipeline_stage": lead.pipeline_stage.value,
            })
            memory.mark_stale_notified(lead.id, now, threshold=fired)
            counts[fired] += 1

    db.commit()
    return counts
