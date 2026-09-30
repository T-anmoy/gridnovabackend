"""What an associate should do next, per customer — computed from real state, never
stored. Feeds GET /api/v1/associate/home and the `progress_summary` on list items.

Items carry MESSAGE KEYS, not English: the app translates `message_key` (+ `params`)
into English, Hindi or Bengali.

    kind                      message_key                         priority
    stage_rejected            todo.stage_rejected                 1  (params: stage, stage_label, review_note)
    payment_due (overdue)     todo.payment_overdue                1  (params: milestone_number, label, outstanding)
    documents_missing         todo.documents_missing              1  (params: missing_documents)
    ready_to_submit           todo.ready_to_submit                1
    upload_signed_agreement   todo.upload_signed_agreement        2
    formal_proposal_ready     todo.formal_proposal_ready          2
    execution_next_step       todo.execution_next_step            2  (params: stage, stage_label)
    payment_due               todo.payment_due                    2  (params: milestone_number, label, outstanding)
    receipt_available         todo.receipt_available              3  (params: receipt_number) — home feed only

Priority 1 = do now, 2 = do soon, 3 = for information. Closed customers have none.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import (
    Customer, CustomerOnboarding, ExecutionMode, OnboardingStatus, ProjectStage, StageSubmission,
    StageSubmissionStatus,
)

RECEIPT_RECENT_DAYS = 7


def _submissions(db: Session, project_id: int) -> list[StageSubmission]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and project_id in pre.submissions:
        return pre.submissions[project_id]
    return list(db.execute(select(StageSubmission).where(StageSubmission.project_id == project_id)
                           .order_by(StageSubmission.submitted_at, StageSubmission.id)).scalars())


def _item(onboarding: CustomerOnboarding, name: Optional[str], kind: str, priority: int, params: Optional[dict] = None,
          message_key: Optional[str] = None) -> dict:
    return {"kind": kind, "message_key": message_key or f"todo.{kind}", "onboarding_id": onboarding.id,
            "customer_name": name, "priority": priority, "params": params or {}}


def items_for(db: Session, onboarding: CustomerOnboarding, *, include_info: bool = False) -> list[dict]:
    from app.execution import service as execution
    from app.onboarding import agreement, progress, service
    from app.payments import service as payments

    if onboarding.status == OnboardingStatus.closed:
        return []
    customer = db.get(Customer, onboarding.customer_id)
    name = customer.name if customer else None
    items = []
    if onboarding.status == OnboardingStatus.draft:
        missing = service.checklist(db, onboarding)["missing"]
        items.append(_item(onboarding, name, "documents_missing", 1, {"missing_documents": missing}) if missing
                     else _item(onboarding, name, "ready_to_submit", 1))
    done = progress.recorded(db, onboarding.project_id)
    if ProjectStage.proposal_prepared in done and ProjectStage.accepted not in done:
        items.append(_item(onboarding, name, "formal_proposal_ready", 2))
    if ProjectStage.accepted in done and ProjectStage.agreement_signed not in done:
        record = agreement.get(db, onboarding)
        if record is not None and not record.signed_scan_key:
            items.append(_item(onboarding, name, "upload_signed_agreement", 2))
    record = execution.get(db, onboarding)
    if record is not None and record.execution_mode == ExecutionMode.associate:
        subs = _submissions(db, onboarding.project_id)
        latest = {}
        for sub in subs:
            latest[sub.stage] = sub
        rejected = [s for st, s in latest.items() if s.status == StageSubmissionStatus.rejected and st not in done]
        for sub in rejected:
            items.append(_item(onboarding, name, "stage_rejected", 1,
                               {"stage": sub.stage.value, "stage_label": progress.LABELS[sub.stage],
                                "review_note": sub.review_note}))
        stages = progress.applicable(db, onboarding)
        nxt = next((st for st in stages if st not in done), None)
        pending = any(s.status == StageSubmissionStatus.pending for s in subs)
        if nxt in execution.EXECUTION_STAGES and not pending and nxt not in {s.stage for s in rejected}:
            items.append(_item(onboarding, name, "execution_next_step", 2,
                               {"stage": nxt.value, "stage_label": progress.LABELS[nxt]}))
    for m in payments.milestone_status(db, onboarding):
        if m["due_status"] in ("due", "partially_paid"):
            items.append(_item(onboarding, name, "payment_due", 1 if m["overdue"] else 2,
                               {"milestone_number": m["number"], "label": m["label"], "outstanding": m["outstanding"],
                                "overdue": m["overdue"]},
                               message_key="todo.payment_overdue" if m["overdue"] else "todo.payment_due"))
    if include_info:
        items += _receipts(db, onboarding, name)
    return items


def _receipts(db: Session, onboarding: CustomerOnboarding, name: Optional[str]) -> list[dict]:
    from app.memory.models import Payment, PaymentReceipt, ReceiptEmailStatus
    since = datetime.now(timezone.utc) - timedelta(days=RECEIPT_RECENT_DAYS)
    rows = db.execute(select(PaymentReceipt).join(Payment, Payment.id == PaymentReceipt.payment_id).where(
        Payment.onboarding_id == onboarding.id, PaymentReceipt.pdf_key.is_not(None),
        PaymentReceipt.email_status != ReceiptEmailStatus.held, PaymentReceipt.created_at >= since)).scalars()
    return [_item(onboarding, name, "receipt_available", 3, {"receipt_number": r.receipt_number}) for r in rows]


def progress_summary(db: Session, onboarding: CustomerOnboarding, items: Optional[list] = None) -> dict:
    """The one-line progress of a customer for a list card — no second request."""
    from app.execution import service as execution
    from app.onboarding import formal, progress

    stages = progress.applicable(db, onboarding)
    done = progress.recorded(db, onboarding.project_id)
    current = next((s for s in reversed(stages) if s in done), None)
    nxt = next((s for s in stages if s not in done), None)
    items = items_for(db, onboarding) if items is None else items
    blocked = None
    if formal.blocked(db, onboarding):
        blocked = {"reason": "formal_proposal_blocked"}
    elif nxt in execution.EXECUTION_STAGES and execution.get(db, onboarding) is not None:
        found = execution.stage_blockers(db, onboarding, nxt)
        if found:
            blocked = {"reason": found[0]["code"]}
    return {"current_stage": current.value if current else None,
            "stage_label": progress.LABELS[current] if current else None,
            "next_stage": nxt.value if nxt else None,
            "next_stage_label": progress.LABELS[nxt] if nxt else None,
            "needs_action": bool(items), "action_kinds": sorted({i["kind"] for i in items}),
            "blocked": blocked}


def home(db: Session, associate) -> dict:
    """The associate's to-do feed over their OWN customers (a Preferred Associate's too
    — never a downline customer's)."""
    from app.memory.models import Payment, PaymentStatus
    from app.onboarding.preload import preloaded

    own = list(db.execute(select(CustomerOnboarding).where(CustomerOnboarding.owner_associate_id == associate.id)
                          .order_by(CustomerOnboarding.created_at, CustomerOnboarding.id)).scalars())
    unverified = set(db.execute(select(Payment.onboarding_id).where(
        Payment.onboarding_id.in_([o.id for o in own]),
        Payment.status.in_([PaymentStatus.recorded, PaymentStatus.deposited]))).scalars()) if own else set()
    items, needing, awaiting = [], 0, 0
    with preloaded(db, own) as pre:
        for o in own:
            mine = items_for(db, o, include_info=True)
            items += mine
            if any(i["priority"] < 3 for i in mine):
                needing += 1
            events = pre.stage_events.get(o.project_id, {}) if pre else {}
            subs = pre.submissions.get(o.project_id, []) if pre else []
            waiting = (o.status == OnboardingStatus.submitted
                       or any(s.status == StageSubmissionStatus.pending for s in subs)
                       or (ProjectStage.proposal_requested in events and ProjectStage.proposal_prepared not in events)
                       or o.id in unverified)
            awaiting += 1 if waiting else 0
    items.sort(key=lambda i: (i["priority"], i["onboarding_id"]))
    return {"items": items, "totals": {"customers": len(own), "awaiting_grid_nova": awaiting,
                                       "needing_action": needing}}
