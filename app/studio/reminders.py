"""The Studio's daily job (BE-18 §9), POST /api/v1/dashboard/internal/check-studio-reminders
on gridnova-jobs:

    issued, not opened after `not_viewed_after_days`   → remind the owner (once)
    expiring within `expiring_within_days`             → remind the owner (once), and email the
                                                          customer when customer_expiry_reminder
    past valid_until                                   → expired

Days are config/studio.yaml `reminders`. Idempotent: what was sent is recorded on the version
(`reminders_sent`), so a re-run (Cloud Scheduler retries) never repeats a reminder.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import StudioProposal, StudioProposalVersion, StudioStatus
from app.studio import config as studio_config
from app.studio import service


def run(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or service.now()
    today = service.today_ist(now)
    rem = studio_config.load()["reminders"]
    out = {"not_viewed": 0, "expiring": 0, "customer_emails": 0, "expired": 0}
    rows = db.execute(select(StudioProposalVersion).where(
        StudioProposalVersion.status.in_([StudioStatus.issued, StudioStatus.viewed]))).scalars().all()
    for v in rows:
        p = db.get(StudioProposal, v.proposal_id)
        sent = dict(v.reminders_sent or {})
        base = {"proposal_id": p.id, "owner_id": p.owner_id, "code": p.code, "title": p.title,
                "reference": v.document_reference,
                "valid_until": v.valid_until.strftime("%d.%m.%Y") if v.valid_until else None}
        if v.valid_until is not None and today > v.valid_until:
            v.status = StudioStatus.expired
            if p.current_version == v.version and StudioStatus(p.status) in (StudioStatus.issued, StudioStatus.viewed):
                p.status = StudioStatus.expired
            p.updated_at = now
            service.event(db, p, "expired", version=v.version, actor_type="system",
                          detail={"valid_until": v.valid_until.isoformat()})
            out["expired"] += 1
            continue
        issued = service._aware(v.issued_at)
        if (v.first_viewed_at is None and "not_viewed" not in sent and issued is not None
                and now - issued >= timedelta(days=rem["not_viewed_after_days"])):
            sent["not_viewed"] = now.isoformat()
            service._notify_after_commit(db, "studio_not_viewed", {**base, "days": (now - issued).days})
            service.event(db, p, "reminder_sent", version=v.version, actor_type="system",
                          detail={"kind": "not_viewed", "to": "owner"})
            out["not_viewed"] += 1
        if (v.valid_until is not None and "expiring" not in sent
                and (v.valid_until - today).days <= rem["expiring_within_days"]):
            sent["expiring"] = now.isoformat()
            service._notify_after_commit(db, "studio_expiring", base)
            service.event(db, p, "reminder_sent", version=v.version, actor_type="system",
                          detail={"kind": "expiring", "to": "owner"})
            out["expiring"] += 1
            if rem["customer_expiry_reminder"]:
                from app.studio import public
                if public.expiry_email(db, p, v):
                    service.event(db, p, "reminder_sent", version=v.version, actor_type="system",
                                  detail={"kind": "expiring", "to": "customer"})
                    out["customer_emails"] += 1
        if sent != (v.reminders_sent or {}):
            v.reminders_sent = sent
    db.flush()
    return out
