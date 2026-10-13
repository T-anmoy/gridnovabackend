"""The "Network Command Centre" summary — aggregates the database can prove, each one
SQL query with GROUP BY (never a loop over projects), so the query count does not grow
with the network (tests/test_network_summary.py holds the budget).

Definitions (period = [date_from, date_to] as IST days):
  * associates            — count by type, capability/level and status, now.
  * seats                 — per state: State Head seats held (of the maximum), districts
                            with a District Head (of the active districts), blocks with
                            at least one Block Associate (of the active blocks), now.
  * onboardings           — by status, and by current stage (the stage recorded last).
  * verified_revenue      — payment-ledger credits and reversals written in the period.
  * commissions           — earn entries created in the period (`earned`), reversals and
                            recoveries created in it, entries approved in it, and payouts
                            paid in it (net and TDS of payout lines); `pending_count` is
                            every entry still without an amount, now.
  * overdue               — now: verification calls past their due time, survey reviews
                            past target (the review queue's own rule), payments awaiting
                            verification past payments.thresholds.awaiting_verification_hours,
                            commission queries past their due time.
Overdue payment milestones are not included: their due status is computed per project
from the stage history (app/payments/service.py milestone_status), not stored.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.commissions import periods, serialize
from app.memory.models import (
    MAX_STATE_HEADS_PER_STATE, Associate, CommissionEntry, CommissionEntryKind, CommissionEntryStatus, CommissionQuery,
    CommissionQueryStatus, CustomerOnboarding, LedgerEntryKind, OnboardingStatus, Payment, PaymentLedgerEntry,
    PaymentStatus, PayoutBatch, PayoutBatchStatus, PayoutLine, ProjectStageEvent, TerritoryBlock,
    TerritoryDistrict, TerritoryLevel, TerritorySeat, TerritoryState,
)

ZERO = Decimal("0")


def _v(x) -> str:
    return serialize.s(Decimal(x or 0))


def summary(db: Session, date_from: date, date_to: date, now: datetime) -> dict:
    start, end = periods.start_utc(date_from), periods.start_utc(date_to + timedelta(days=1))
    return {"period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
            "generated_at": now.isoformat(),
            "associates": _associates(db), "seats": _seats(db), "onboardings": _onboardings(db),
            "verified_revenue": _revenue(db, start, end), "commissions": _commissions(db, start, end),
            "overdue": _overdue(db, now)}


def _associates(db: Session) -> list[dict]:
    rows = db.execute(select(Associate.associate_type, Associate.capability, Associate.level, Associate.status,
                             func.count()).group_by(Associate.associate_type, Associate.capability, Associate.level,
                                                    Associate.status)).all()
    return sorted(({"associate_type": t.value, "capability": c.value if c else None, "level": lv.value if lv else None,
                    "status": st.value, "count": n} for t, c, lv, st, n in rows),
                  key=lambda r: (r["associate_type"], r["capability"] or "", r["level"] or "", r["status"]))


def _seats(db: Session) -> list[dict]:
    held = TerritorySeat.released_at.is_(None)
    states = db.execute(select(TerritoryState.id, TerritoryState.lgd_code, TerritoryState.name)
                        .where(TerritoryState.is_active.is_(True)).order_by(TerritoryState.name)).all()
    heads = dict(db.execute(select(TerritorySeat.state_id, func.count()).where(
        held, TerritorySeat.level == TerritoryLevel.state_head).group_by(TerritorySeat.state_id)).all())
    districts = dict(db.execute(select(TerritoryDistrict.state_id, func.count()).where(
        TerritoryDistrict.is_active.is_(True)).group_by(TerritoryDistrict.state_id)).all())
    district_heads = dict(db.execute(select(TerritorySeat.state_id, func.count(func.distinct(TerritorySeat.district_id)))
                                     .where(held, TerritorySeat.level == TerritoryLevel.district_head)
                                     .group_by(TerritorySeat.state_id)).all())
    blocks = dict(db.execute(select(TerritoryDistrict.state_id, func.count(TerritoryBlock.id))
                             .join(TerritoryBlock, TerritoryBlock.district_id == TerritoryDistrict.id)
                             .where(TerritoryBlock.is_active.is_(True)).group_by(TerritoryDistrict.state_id)).all())
    block_holders = dict(db.execute(select(TerritorySeat.state_id, func.count(func.distinct(TerritorySeat.block_id)))
                                    .where(held, TerritorySeat.level == TerritoryLevel.block)
                                    .group_by(TerritorySeat.state_id)).all())
    return [{"state_lgd_code": code, "state_name": name,
             "state_heads": {"held": heads.get(sid, 0), "max": MAX_STATE_HEADS_PER_STATE},
             "districts": {"with_head": district_heads.get(sid, 0), "total": districts.get(sid, 0)},
             "blocks": {"with_associate": block_holders.get(sid, 0), "total": blocks.get(sid, 0)}}
            for sid, code, name in states]


def _onboardings(db: Session) -> dict:
    by_status = {s.value: n for s, n in db.execute(select(CustomerOnboarding.status, func.count())
                                                    .group_by(CustomerOnboarding.status)).all()}
    latest = (select(ProjectStageEvent.project_id, func.max(ProjectStageEvent.recorded_at).label("at"))
              .group_by(ProjectStageEvent.project_id).subquery())
    current = db.execute(select(ProjectStageEvent.stage, func.count(func.distinct(CustomerOnboarding.id)))
                         .join(latest, and_(latest.c.project_id == ProjectStageEvent.project_id,
                                            latest.c.at == ProjectStageEvent.recorded_at))
                         .join(CustomerOnboarding, CustomerOnboarding.project_id == ProjectStageEvent.project_id)
                         .where(CustomerOnboarding.status != OnboardingStatus.closed)
                         .group_by(ProjectStageEvent.stage)).all()
    return {"by_status": {s.value: by_status.get(s.value, 0) for s in OnboardingStatus},
            "by_current_stage": {stage.value: n for stage, n in current}}


def _revenue(db: Session, start: datetime, end: datetime) -> dict:
    rows = dict(db.execute(select(PaymentLedgerEntry.kind, func.sum(PaymentLedgerEntry.amount)).where(
        PaymentLedgerEntry.created_at >= start, PaymentLedgerEntry.created_at < end)
        .group_by(PaymentLedgerEntry.kind)).all())
    credits, reversals = Decimal(rows.get(LedgerEntryKind.credit) or 0), Decimal(rows.get(LedgerEntryKind.reversal) or 0)
    return {"credits": _v(credits), "reversals": _v(reversals), "net": _v(credits + reversals)}


def _commissions(db: Session, start: datetime, end: datetime) -> dict:
    in_period = and_(CommissionEntry.created_at >= start, CommissionEntry.created_at < end)
    by_kind = dict(db.execute(select(CommissionEntry.kind, func.sum(CommissionEntry.amount)).where(in_period)
                              .group_by(CommissionEntry.kind)).all())
    approved = db.execute(select(func.sum(CommissionEntry.amount)).where(
        CommissionEntry.approved_at >= start, CommissionEntry.approved_at < end,
        CommissionEntry.kind == CommissionEntryKind.earn)).scalar()
    paid_net, paid_tds = db.execute(select(func.sum(PayoutLine.net), func.sum(PayoutLine.tds)).join(
        PayoutBatch, PayoutBatch.id == PayoutLine.batch_id).where(
        PayoutBatch.status == PayoutBatchStatus.paid, PayoutBatch.paid_at >= start, PayoutBatch.paid_at < end)).one()
    pending = db.execute(select(func.count()).select_from(CommissionEntry).where(
        CommissionEntry.amount.is_(None), CommissionEntry.kind == CommissionEntryKind.earn,
        CommissionEntry.status != CommissionEntryStatus.reversed)).scalar_one()
    return {"earned": _v(by_kind.get(CommissionEntryKind.earn)),
            "reversed": _v((by_kind.get(CommissionEntryKind.reversal) or ZERO)
                           + (by_kind.get(CommissionEntryKind.recovery) or ZERO)),
            "approved": _v(approved), "paid_net": _v(paid_net), "paid_tds": _v(paid_tds), "pending_count": pending}


def _overdue(db: Session, now: datetime) -> dict:
    from app.execution import config as execution_config
    from app.survey import queue as survey_queue

    hours = execution_config.payments()["thresholds"]["awaiting_verification_hours"]
    calls = db.execute(select(func.count()).select_from(CustomerOnboarding).where(
        CustomerOnboarding.status == OnboardingStatus.submitted, CustomerOnboarding.response_due_at < now)).scalar_one()
    payments = db.execute(select(func.count()).select_from(Payment).where(
        Payment.status.in_([PaymentStatus.recorded, PaymentStatus.deposited]),
        Payment.recorded_at < now - timedelta(hours=hours))).scalar_one()
    queries = db.execute(select(func.count()).select_from(CommissionQuery).where(
        CommissionQuery.status == CommissionQueryStatus.open, CommissionQuery.due_at < now)).scalar_one()
    return {"verification_calls": calls, "survey_reviews": survey_queue.counts(db, None, now)["overdue"],
            "payments_awaiting_verification": payments, "commission_queries": queries}


def default_period(today: date) -> tuple[date, date]:
    """This month so far."""
    return today.replace(day=1), today

