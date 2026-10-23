"""One Associate's performance over a period (BE-12) — what an Admin looks at before
raising or lowering their individual rate. Built only from data the system records;
each figure is one aggregate query (the count does not grow with the Associate's
customers). Period = [date_from, date_to] as IST days.

  * customers.onboarded   — onboardings this Associate owns that were created in the
                            period; `verified` — of their onboardings, those Grid Nova
                            verified in the period (verified_at); `commissioned` —
                            their customers' projects whose `commissioned` stage was
                            recorded in the period.
  * verified_payments     — the payment ledger for their own customers' projects in the
                            period: credits (payments verified), reversals (dishonoured),
                            net. As in the Network Command Centre (app/commissions/network.py).
  * commission            — this Associate's own commission ledger entries created in the
                            period: earned (earn entries with an amount, every role),
                            of which personal_sale; reversed (reversals and recoveries);
                            pending_count (earn entries still without an amount).

Installed capacity (kW) is not reported: projects.capacity_kw is the capacity the
customer asked for at onboarding (desired_output_kw), not what was installed.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.commissions import periods, serialize
from app.memory.models import (
    Associate, CommissionEntry, CommissionEntryKind, CommissionRole, CustomerOnboarding, LedgerEntryKind,
    PaymentLedgerEntry, ProjectStage, ProjectStageEvent,
)


def _v(x) -> str:
    return serialize.s(Decimal(x or 0))


def summary(db: Session, associate: Associate, date_from: date, date_to: date) -> dict:
    start, end = periods.start_utc(date_from), periods.start_utc(date_to + timedelta(days=1))
    own = CustomerOnboarding.owner_associate_id == associate.id
    projects = select(CustomerOnboarding.project_id).where(own)

    onboarded = db.execute(select(func.count()).select_from(CustomerOnboarding).where(
        own, CustomerOnboarding.created_at >= start, CustomerOnboarding.created_at < end)).scalar_one()
    verified = db.execute(select(func.count()).select_from(CustomerOnboarding).where(
        own, CustomerOnboarding.verified_at >= start, CustomerOnboarding.verified_at < end)).scalar_one()
    commissioned = db.execute(select(func.count(func.distinct(ProjectStageEvent.project_id))).where(
        ProjectStageEvent.project_id.in_(projects), ProjectStageEvent.stage == ProjectStage.commissioned,
        ProjectStageEvent.recorded_at >= start, ProjectStageEvent.recorded_at < end)).scalar_one()

    ledger = dict(db.execute(select(PaymentLedgerEntry.kind, func.sum(PaymentLedgerEntry.amount)).where(
        PaymentLedgerEntry.project_id.in_(projects), PaymentLedgerEntry.created_at >= start,
        PaymentLedgerEntry.created_at < end).group_by(PaymentLedgerEntry.kind)).all())
    credits = Decimal(ledger.get(LedgerEntryKind.credit) or 0)
    reversals = Decimal(ledger.get(LedgerEntryKind.reversal) or 0)

    mine = and_(CommissionEntry.associate_id == associate.id, CommissionEntry.created_at >= start,
                CommissionEntry.created_at < end)
    by = db.execute(select(CommissionEntry.kind, CommissionEntry.earning_role, func.sum(CommissionEntry.amount),
                           func.count().filter(CommissionEntry.amount.is_(None)))
                    .where(mine).group_by(CommissionEntry.kind, CommissionEntry.earning_role)).all()
    earned = sum((Decimal(t or 0) for k, _, t, _ in by if k == CommissionEntryKind.earn), Decimal(0))
    personal = sum((Decimal(t or 0) for k, r, t, _ in by
                    if k == CommissionEntryKind.earn and r == CommissionRole.personal_sale), Decimal(0))
    reversed_ = sum((Decimal(t or 0) for k, _, t, _ in by
                     if k in (CommissionEntryKind.reversal, CommissionEntryKind.recovery)), Decimal(0))
    pending = sum(n for k, _, _, n in by if k == CommissionEntryKind.earn)

    return {"associate_id": associate.associate_id,
            "period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
            "customers": {"onboarded": onboarded, "verified": verified, "commissioned": commissioned},
            "verified_payments": {"credits": _v(credits), "reversals": _v(reversals), "net": _v(credits + reversals)},
            "commission": {"earned": _v(earned), "earned_personal_sale": _v(personal), "reversed": _v(reversed_),
                           "pending_count": pending}}
