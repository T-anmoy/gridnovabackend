"""Accounts alerts for payments (Cloud Scheduler → POST
/api/v1/dashboard/internal/check-payment-alerts).

  * a milestone due and unpaid past payments.thresholds.milestone_overdue_days —
    emailed once per milestone (payment_milestones.overdue_alerted_at);
  * a payment still recorded/deposited past awaiting_verification_hours — emailed once
    per payment (payments.alerted_at).
One digest email per run. Idempotent.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    Customer, CustomerOnboarding, Payment, PaymentLedgerEntry, PaymentMilestone, PaymentStatus,
)
from app.payments import service as payments


def _open_onboarding_ids(db: Session) -> list[int]:
    """Onboardings that could still produce a milestone alert: at least one milestone not
    yet alerted, on a project whose verified money does not yet cover its schedule. A
    fully paid project, or one whose every milestone was already alerted, is never
    loaded again (it used to be re-scanned on every run, forever)."""
    scheduled = (select(PaymentMilestone.project_id.label("project_id"),
                        func.sum(PaymentMilestone.amount_due).label("due"))
                 .where(PaymentMilestone.onboarding_id.is_not(None))
                 .group_by(PaymentMilestone.project_id).subquery())
    received = (select(PaymentLedgerEntry.project_id.label("project_id"),
                       func.sum(PaymentLedgerEntry.amount).label("paid"))
                .group_by(PaymentLedgerEntry.project_id).subquery())
    return list(db.execute(
        select(PaymentMilestone.onboarding_id).distinct()
        .join(scheduled, scheduled.c.project_id == PaymentMilestone.project_id)
        .outerjoin(received, received.c.project_id == PaymentMilestone.project_id)
        .where(PaymentMilestone.onboarding_id.is_not(None), PaymentMilestone.overdue_alerted_at.is_(None),
               func.coalesce(received.c.paid, 0) < scheduled.c.due)
        .order_by(PaymentMilestone.onboarding_id)).scalars())


def check_payment_alerts(db: Session, now: datetime) -> dict:
    from app.notifications import notifier as notifier_module

    overdue, waiting = [], []
    for onboarding in (db.get(CustomerOnboarding, i) for i in _open_onboarding_ids(db)):
        customer = db.get(Customer, onboarding.customer_id)
        for m in payments.milestone_status(db, onboarding, now):
            row = db.get(PaymentMilestone, m["milestone_id"])
            if m["overdue"] and row.overdue_alerted_at is None:
                row.overdue_alerted_at = now
                overdue.append({"onboarding_id": onboarding.id, "customer_name": customer.name if customer else None,
                                "label": m["label"], "outstanding": m["outstanding"], "due_since": m["due_since"]})
    for payment in db.execute(select(Payment).where(
            Payment.status.in_([PaymentStatus.recorded, PaymentStatus.deposited]),
            Payment.alerted_at.is_(None))).scalars():
        if "awaiting_verification" in payments.attention(payment, now):
            payment.alerted_at = now
            waiting.append({"onboarding_id": payment.onboarding_id, "payment_id": payment.id,
                            "amount": payments.s(payment.amount), "mode": payment.mode.value})
    if overdue or waiting:
        payload = {"overdue_milestones": overdue, "awaiting_verification": waiting}
        MemoryManager(db).notify_after_commit(lambda: notifier_module.notifier.notify("payments_attention", payload),
                                              "payments_attention")
    db.commit()
    return {"overdue_milestones": len(overdue), "awaiting_verification": len(waiting)}
