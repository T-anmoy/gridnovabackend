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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.memory_manager import MemoryManager
from app.memory.models import Customer, CustomerOnboarding, Payment, PaymentMilestone, PaymentStatus
from app.payments import service as payments


def check_payment_alerts(db: Session, now: datetime) -> dict:
    from app.notifications import notifier as notifier_module

    overdue, waiting = [], []
    onboarding_ids = db.execute(select(PaymentMilestone.onboarding_id).where(
        PaymentMilestone.onboarding_id.is_not(None)).distinct()).scalars().all()
    for onboarding in (db.get(CustomerOnboarding, i) for i in onboarding_ids):
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
