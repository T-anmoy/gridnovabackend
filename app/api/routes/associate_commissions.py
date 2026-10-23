"""Commissions for associates — associate-authenticated (BE-4, policy 4.4–4.8).

Every associate sees only their OWN entries: a Preferred Associate's own entries include
the territory commission they earn on customers in their territory (with the customer and
payment each relates to, 4.4) — never another associate's entries. Payouts show the
batches that are approved or paid (a draft is Accounts' working copy).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.commissions import config, payouts, periods, rates, serialize
from app.commissions import queries as commission_queries
from app.memory.db import get_db
from app.memory.models import (
    Associate, CommissionEntry, CommissionEntryKind, CommissionEntryStatus, CommissionQuery, CommissionRole,
    PayoutBatch, PayoutBatchStatus, PayoutLine,
)

router = APIRouter(prefix="/api/v1/associate")
SHOWN_BATCHES = (PayoutBatchStatus.approved, PayoutBatchStatus.paid)


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def next_payout(db: Session, today: date) -> dict:
    """The month to be paid next and the day it is expected by (payout.pay_by_day…):
    last month, until its batch is paid; then this month."""
    period = periods.previous_period(periods.period_of(today))
    paid = db.execute(select(PayoutBatch.id).where(PayoutBatch.period == period,
                                                   PayoutBatch.status == PayoutBatchStatus.paid)).first()
    if paid:
        period = periods.period_of(today)
    by = periods.first_day(periods.next_period(period)).replace(day=config.payout()["pay_by_day_of_following_month"])
    return {"period": period, "expected_by": by.isoformat()}


def summary(db: Session, associate: Associate, today: date) -> dict:
    sums = dict(db.execute(select(CommissionEntry.status, func.sum(CommissionEntry.amount)).where(
        CommissionEntry.associate_id == associate.id, CommissionEntry.amount.is_not(None),
        CommissionEntry.status != CommissionEntryStatus.reversed).group_by(CommissionEntry.status)).all())
    pending = db.execute(select(func.count()).select_from(CommissionEntry).where(
        CommissionEntry.associate_id == associate.id, CommissionEntry.amount.is_(None),
        CommissionEntry.status != CommissionEntryStatus.reversed)).scalar_one()
    fy = periods.financial_year(today)
    taxable, tds = db.execute(select(func.coalesce(func.sum(PayoutLine.taxable), 0),
                                     func.coalesce(func.sum(PayoutLine.tds), 0))
                              .join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                              .where(PayoutLine.associate_id == associate.id, PayoutBatch.financial_year == fy,
                                     PayoutBatch.status.in_(SHOWN_BATCHES))).one()
    last = db.execute(select(PayoutLine.carry_forward).join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                      .where(PayoutLine.associate_id == associate.id, PayoutBatch.status.in_(SHOWN_BATCHES))
                      .order_by(PayoutBatch.period.desc()).limit(1)).scalar()
    return {
        "earned": serialize.s(Decimal(sums.get(CommissionEntryStatus.earned) or 0)),
        "approved": serialize.s(Decimal(sums.get(CommissionEntryStatus.approved) or 0)),
        "paid": serialize.s(Decimal(sums.get(CommissionEntryStatus.paid) or 0)),
        "pending_count": pending,
        "to_recover": serialize.s(Decimal(last or 0)),
        "financial_year": fy, "fy_taxable_commission": serialize.s(Decimal(taxable)),
        "fy_tds_deducted": serialize.s(Decimal(tds)),
        "next_payout": next_payout(db, today),
    }


@router.get("/commissions")
def my_commissions(status: Optional[CommissionEntryStatus] = None, kind: Optional[CommissionEntryKind] = None,
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
                   db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The caller's own commission entries, newest first, with a summary."""
    associate = db.get(Associate, current.associate_pk)
    q = select(CommissionEntry).where(CommissionEntry.associate_id == associate.id)
    if status:
        q = q.where(CommissionEntry.status == status)
    if kind:
        q = q.where(CommissionEntry.kind == kind)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = list(db.execute(q.order_by(CommissionEntry.created_at.desc(), CommissionEntry.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)).scalars())
    return {"items": serialize.entries_json(db, rows, staff=False), "page": page, "page_size": page_size,
            "total": total, "summary": summary(db, associate, periods.today_ist())}


@router.get("/payouts")
def my_payouts(db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The caller's payouts in approved or paid batches, newest month first."""
    rows = db.execute(select(PayoutLine, PayoutBatch).join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                      .where(PayoutLine.associate_id == current.associate_pk,
                             PayoutBatch.status.in_(SHOWN_BATCHES))
                      .order_by(PayoutBatch.period.desc())).all()
    return {"items": [{"batch_id": b.id, "period": b.period, "financial_year": b.financial_year,
                       "status": b.status.value, **payouts.line_json(ln, None, staff=False)} for ln, b in rows]}


class QueryIn(BaseModel):
    message: str = Field(min_length=1, max_length=commission_queries.MAX_MESSAGE)


@router.post("/commissions/{entry_id}/queries", status_code=201)
def raise_query(entry_id: int, body: QueryIn, db: Session = Depends(get_db),
                current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Query one of your own entries (within queries.window_days of it appearing)."""
    associate = db.get(Associate, current.associate_pk)
    try:
        query = commission_queries.raise_query(db, associate, entry_id, body.message, f"associate:{associate.associate_id}")
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return serialize.query_json(query, staff=False)


@router.get("/commissions/{entry_id}/queries")
def entry_queries(entry_id: int, db: Session = Depends(get_db),
                  current: AuthenticatedAssociate = Depends(get_current_associate)):
    entry = db.get(CommissionEntry, entry_id)
    if entry is None or entry.associate_id != current.associate_pk:
        raise HTTPException(status_code=404, detail="Commission entry not found")
    rows = db.execute(select(CommissionQuery).where(CommissionQuery.entry_id == entry_id)
                      .order_by(CommissionQuery.created_at.desc(), CommissionQuery.id.desc())).scalars()
    return {"items": [serialize.query_json(q, staff=False) for q in rows]}


# ── My commission rate (BE-12) ───────────────────────────────────────────────────

class MyEffectiveRate(BaseModel):
    """rate_percent null = no confirmed rate yet (pending_reason placeholder_rates)."""
    rate_percent: Optional[str]
    source: Optional[Literal["standard", "individual"]]
    effective_from: Optional[str]
    pending_reason: Optional[Literal["placeholder_rates"]]


class MyScheduledChange(BaseModel):
    effective_from: str
    rate_percent: Optional[str]
    source: Optional[Literal["standard", "individual"]]


class MyRateResponse(BaseModel):
    rate_key: str
    today: str
    effective: MyEffectiveRate
    scheduled_changes: list[MyScheduledChange]


@router.get("/commission-rate", response_model=MyRateResponse)
def my_commission_rate(db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The caller's own personal-sale rate today (standard or individual) and every
    scheduled change. Never the reason for an individual rate, never anyone else's."""
    associate = db.get(Associate, current.associate_pk)
    today, cache = periods.today_ist(), {}
    return {"rate_key": rates.rate_key(CommissionRole.personal_sale, associate), "today": today.isoformat(),
            "effective": rates.resolved_json(rates.resolve(db, associate, CommissionRole.personal_sale, today,
                                                           cache=cache)),
            "scheduled_changes": rates.scheduled_changes(db, associate, today, cache)}
