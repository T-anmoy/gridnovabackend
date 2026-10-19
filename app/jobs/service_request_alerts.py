"""Overdue service requests (Cloud Scheduler → POST
/api/v1/dashboard/internal/check-service-requests).

A request is overdue when it is still open or in progress past its `sla_due_at`
(service.sla_hours when it was raised — null means no SLA and nothing is ever overdue).
Operations get one email per run listing the requests that became overdue since the last
run; `overdue_alerted_at` remembers each one, so a request is emailed once. Idempotent.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.memory_manager import MemoryManager
from app.memory.models import Customer, CustomerOnboarding, Project, ServiceRequest


def check_service_requests(db: Session, now: datetime) -> dict:
    from app.handover import requests
    from app.notifications import notifier as notifier_module

    overdue = list(db.execute(select(ServiceRequest).where(*requests.overdue_filter(now))
                              .order_by(ServiceRequest.sla_due_at, ServiceRequest.id)).scalars())
    fresh = [r for r in overdue if r.overdue_alerted_at is None]
    rows = []
    for req in fresh:
        req.overdue_alerted_at = now
        onboarding = db.get(CustomerOnboarding, req.onboarding_id)
        customer = db.get(Customer, onboarding.customer_id)
        hours = int((now - requests._aware(req.sla_due_at)).total_seconds() // 3600)
        rows.append({"request_id": req.id, "customer_name": customer.name if customer else None,
                     "project_reference_id": db.get(Project, req.project_id).reference_id, "hours_overdue": hours})
    if rows:
        payload = {"requests": rows}
        MemoryManager(db).notify_after_commit(
            lambda: notifier_module.notifier.notify("service_requests_overdue", payload), "service_requests_overdue")
    db.commit()
    return {"overdue": len(overdue), "newly_alerted": len(fresh)}
