"""Reading the Studio for the dashboard: the proposal list with filters and the summary
figures (BE-18 §12). Amounts are decimal strings; counts are integers."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.memory.models import StudioCustomer, StudioProposal, StudioProposalVersion, StudioStatus
from app.studio import config as studio_config
from app.studio import service

OPEN_ISSUED = (StudioStatus.issued, StudioStatus.viewed)


def proposals(db: Session, *, status: Optional[str] = None, project_type: Optional[str] = None,
              owner_id: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
              q: Optional[str] = None, page: int = 1, page_size: int = 25,
              customer_id: Optional[int] = None) -> dict:
    cur = and_(StudioProposalVersion.proposal_id == StudioProposal.id,
               StudioProposalVersion.version == StudioProposal.current_version)
    stmt = (select(StudioProposal, StudioProposalVersion).join(StudioProposalVersion, cur)
            .join(StudioCustomer, StudioCustomer.id == StudioProposal.customer_id))
    if status:
        stmt = stmt.where(StudioProposal.status == StudioStatus(status))
    if project_type:
        stmt = stmt.where(StudioProposal.project_type == project_type)
    if owner_id:
        stmt = stmt.where(StudioProposal.owner_id == owner_id)
    if customer_id is not None:
        stmt = stmt.where(StudioProposal.customer_id == customer_id)
    if date_from:
        stmt = stmt.where(StudioProposal.created_at >= datetime.combine(date_from, datetime.min.time(), timezone.utc))
    if date_to:
        stmt = stmt.where(StudioProposal.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time(),
                                                                       timezone.utc))
    if q:
        text = q.strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        conds = [func.lower(StudioCustomer.name).contains(text.lower()), StudioProposal.code == text.upper(),
                 func.lower(StudioProposal.title).contains(text.lower())]
        if len(digits) >= 4:
            conds.append(StudioCustomer.mobile.contains(digits[-10:]))
        conds.append(StudioProposalVersion.document_reference.contains(text.upper()))
        stmt = stmt.where(or_(*conds))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(StudioProposal.updated_at.desc(), StudioProposal.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    names = service.owner_names(db, (p.owner_id for p, _ in rows))
    return {"items": [service.proposal_json(db, p, v, names) for p, v in rows], "total": total, "page": page,
            "page_size": page_size}


def summary(db: Session, *, owner_id: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    now = now or service.now()
    today = service.today_ist(now)
    month_start = today.replace(day=1)
    since_90 = now - timedelta(days=90)

    def scoped(stmt):
        return stmt.where(StudioProposal.owner_id == owner_id) if owner_id else stmt

    def count_status(*statuses) -> int:
        return db.execute(scoped(select(func.count(StudioProposal.id)).where(StudioProposal.status.in_(statuses))
                                 )).scalar_one()

    issued = db.execute(scoped(select(StudioProposalVersion).join(
        StudioProposal, StudioProposal.id == StudioProposalVersion.proposal_id)
        .where(StudioProposalVersion.issued_at.is_not(None)))).scalars().all()
    month = [v for v in issued if service.today_ist(service._aware(v.issued_at)) >= month_start]
    accepted = [v for v in issued if v.accepted_at is not None]
    recent = [v for v in issued if service._aware(v.issued_at) >= since_90]
    # Win rate: proposals (not versions) issued in the last 90 days that were accepted.
    recent_props = {v.proposal_id for v in recent}
    won_props = {v.proposal_id for v in recent if v.accepted_at is not None}
    days = [(service._aware(v.accepted_at) - service._aware(v.issued_at)).total_seconds() / 86400 for v in accepted]
    soon = today + timedelta(days=studio_config.load()["reminders"]["summary_expiring_within_days"])
    expiring = [v for v in issued if StudioStatus(v.status) in OPEN_ISSUED and v.valid_until
                and today <= v.valid_until <= soon]
    viewed_open = [v for v in issued if StudioStatus(v.status) == StudioStatus.viewed]

    def value(vs) -> str:
        return str(sum((Decimal(v.total) for v in vs if v.total is not None), Decimal(0)).quantize(Decimal("0.01")))

    return {
        "drafts": count_status(StudioStatus.draft, StudioStatus.ready),
        "awaiting_approval": count_status(StudioStatus.pending_approval),
        "issued_this_month": {"count": len(month), "value": value(month)},
        "viewed_not_accepted": len(viewed_open),
        "accepted": {"count": len(accepted), "value": value(accepted)},
        "win_rate_90_days": (round(len(won_props) / len(recent_props), 4) if recent_props else None),
        "expiring_in_7_days": len(expiring),
        "median_days_issued_to_accepted": round(median(days), 1) if days else None,
        "owner_id": owner_id,
    }
