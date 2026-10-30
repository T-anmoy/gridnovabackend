"""What an associate should do next, per customer — computed from real state, never
stored. Feeds GET /api/v1/associate/home and the `progress_summary` on list items.

Items carry MESSAGE KEYS, not English: the app translates `message_key` (+ `params`)
into English, Hindi or Bengali.

    kind                      message_key                         priority
    stage_rejected            todo.stage_rejected                 1  (params: stage, stage_label, review_note)
    payment_due (overdue)     todo.payment_overdue                1  (params: milestone_number, label, outstanding)
    documents_missing         todo.documents_missing              1  (params: missing_documents)
    site_location_missing     todo.site_location_missing          1  (draft only)
    site_location_unconfirmed todo.site_location_unconfirmed      1  (draft only)
    ready_to_submit           todo.ready_to_submit                1  (documents, confirmed location AND — unless
                                                                     grandfathered — a complete, signed survey)
    survey_revisit_needed     todo.survey_revisit_needed          1  (params: revisit_items, notes, via — "edit"
                                                                     for a draft/submitted customer, "correction"
                                                                     for a verified one; until the survey changes)
    survey_resign_needed      todo.survey_resign_needed           1  (params: unsigned — a verified customer whose
                                                                     signatures a correction cleared)
    upload_signed_agreement   todo.upload_signed_agreement        2
    formal_proposal_ready     todo.formal_proposal_ready          2
    execution_next_step       todo.execution_next_step            2  (params: stage, stage_label)
    payment_due               todo.payment_due                    2  (params: milestone_number, label, outstanding)
    receipt_available         todo.receipt_available              3  (params: receipt_number) — home feed only
    handover_pending          todo.handover_pending               3  (commissioned, not yet handed over) — home feed only
    service_request_open      todo.service_request_open           3  (params: request_id, category, status,
                                                                     within_warranty) — home feed only
    service_request_closed    todo.service_request_closed         3  (params: request_id, category, outcome —
                                                                     resolved|rejected) — home feed only, for
                                                                     service.associate_notice_days
    commission_payout_paid    todo.commission_payout_paid         3  (params: batch_id, period, net, bank_reference,
                                                                     bank_account_last4) — home feed only; not about
                                                                     one customer, so onboarding_id/customer_name null
    commission_query_answered todo.commission_query_answered      3  (params: query_id, entry_id) — home feed only;
                                                                     onboarding_id is the entry's customer (or null)
    commission_rate_changed   todo.commission_rate_changed        3  (params: rate_percent_from, rate_percent_to,
                                                                     effective_from, scheduled) — BE-12, home feed
                                                                     only: an Admin changed the associate's own rate,
                                                                     set in the last payout.recent_days days or taking
                                                                     effect later; never the reason

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
        from app.onboarding import site_location
        missing = service.checklist(db, onboarding)["missing"]
        gaps = site_location.submit_gaps(onboarding)
        if missing:
            items.append(_item(onboarding, name, "documents_missing", 1, {"missing_documents": missing}))
        if gaps["missing_site_location"]:
            items.append(_item(onboarding, name, "site_location_missing", 1))
        elif gaps["site_location_unconfirmed"]:
            items.append(_item(onboarding, name, "site_location_unconfirmed", 1))
        from app.memory.models import ProposalInputs
        from app.survey import status as survey_status
        survey_gaps = survey_status.submit_gaps(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id))
        if not missing and not any(gaps.values()) and not any(survey_gaps.values()):
            items.append(_item(onboarding, name, "ready_to_submit", 1))
    items += _survey(db, onboarding, name)
    done = progress.recorded(db, onboarding.project_id)
    if ProjectStage.proposal_prepared in done and ProjectStage.accepted not in done:
        items.append(_item(onboarding, name, "formal_proposal_ready", 2))
    if ProjectStage.accepted in done and ProjectStage.agreement_signed not in done:
        record = agreement.get(db, onboarding)
        if record is not None and not record.signed_scan_key and agreement.servable(db, record):
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
        if ProjectStage.commissioned in done and ProjectStage.handover_complete not in done:
            items.append(_item(onboarding, name, "handover_pending", 3))
    return items


def _survey(db: Session, onboarding: CustomerOnboarding, name: Optional[str]) -> list[dict]:
    """The Engineer asked for a revisit (S1 Step 5), or a correction cleared the
    signatures of a verified customer (Step 6)."""
    from app.memory.models import FeasibilityOutcome, ProposalInputs
    from app.survey import status as survey_status

    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    items = []
    if inputs.feasibility_outcome == FeasibilityOutcome.revisit_needed and not inputs.survey_revised_after_review:
        items.append(_item(onboarding, name, "survey_revisit_needed", 1,
                           {"revisit_items": inputs.revisit_items or [], "notes": inputs.feasibility_notes,
                            "via": "correction" if onboarding.status == OnboardingStatus.verified else "edit"}))
    if onboarding.status == OnboardingStatus.verified and survey_status.required_to_submit(onboarding):
        unsigned = survey_status.unsigned(inputs)
        if unsigned:
            items.append(_item(onboarding, name, "survey_resign_needed", 1, {"unsigned": unsigned}))
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
    from app.onboarding import site_location
    return {"current_stage": current.value if current else None,
            "stage_label": progress.LABELS[current] if current else None,
            "next_stage": nxt.value if nxt else None,
            "next_stage_label": progress.LABELS[nxt] if nxt else None,
            "needs_action": bool(items), "action_kinds": sorted({i["kind"] for i in items}),
            "blocked": blocked,
            # Safe for every viewer (no coordinates).
            "site_location": {"present": site_location.is_present(onboarding),
                              "confirmed": site_location.is_confirmed(onboarding)}}


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
    items += commission_items(db, associate)
    items += service_request_items(db, own)
    items.sort(key=lambda i: (i["priority"], i["onboarding_id"] or 0))
    return {"items": items, "totals": {"customers": len(own), "awaiting_grid_nova": awaiting,
                                       "needing_action": needing}}


def service_request_items(db: Session, onboardings: list) -> list[dict]:
    """BE-5: open service requests, and ones closed in the last service.associate_notice_days
    days, for the associate's own customers. One query for the whole feed."""
    from app.handover import config as handover_config
    from app.handover.requests import OPEN
    from app.memory.models import ServiceRequest

    if not onboardings:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=handover_config.service()["associate_notice_days"])
    names = {o.id: o for o in onboardings}
    out = []
    for req in db.execute(select(ServiceRequest).where(
            ServiceRequest.onboarding_id.in_(list(names)),
            (ServiceRequest.status.in_(OPEN)) | (ServiceRequest.closed_at >= since))
            .order_by(ServiceRequest.id)).scalars():
        customer = db.get(Customer, names[req.onboarding_id].customer_id)
        base = {"onboarding_id": req.onboarding_id, "customer_name": customer.name if customer else None,
                "priority": 3}
        if req.status in OPEN:
            out.append({"kind": "service_request_open", "message_key": "todo.service_request_open", **base,
                        "params": {"request_id": req.id, "category": req.category.value, "status": req.status.value,
                                   "within_warranty": req.within_warranty}})
        else:
            out.append({"kind": "service_request_closed", "message_key": "todo.service_request_closed", **base,
                        "params": {"request_id": req.id, "category": req.category.value,
                                   "outcome": req.status.value}})
    return out


def commission_items(db: Session, associate) -> list[dict]:
    """BE-4: a payout paid, or a commission query answered, in the last
    payout.recent_days days (config/commissions.yaml). Two queries. BE-12: and a
    change of the associate's own rate (rate_items)."""
    from app.commissions import config as commission_config
    from app.commissions.serialize import s
    from app.memory.models import (
        CommissionEntry, CommissionQuery, CommissionQueryStatus, PayoutBatch, PayoutBatchStatus, PayoutLine,
    )

    since = datetime.now(timezone.utc) - timedelta(days=commission_config.payout()["recent_days"])
    out = []
    for line, batch in db.execute(select(PayoutLine, PayoutBatch).join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                                  .where(PayoutLine.associate_id == associate.id,
                                         PayoutBatch.status == PayoutBatchStatus.paid, PayoutBatch.paid_at >= since)
                                  .order_by(PayoutBatch.period)).all():
        out.append({"kind": "commission_payout_paid", "message_key": "todo.commission_payout_paid",
                    "onboarding_id": None, "customer_name": None, "priority": 3,
                    "params": {"batch_id": batch.id, "period": batch.period, "net": s(line.net),
                               "bank_reference": line.bank_reference, "bank_account_last4": line.bank_account_last4}})
    for query, onboarding_id in db.execute(
            select(CommissionQuery, CommissionEntry.onboarding_id)
            .join(CommissionEntry, CommissionEntry.id == CommissionQuery.entry_id)
            .where(CommissionQuery.associate_id == associate.id, CommissionQuery.status == CommissionQueryStatus.answered,
                   CommissionQuery.responded_at >= since).order_by(CommissionQuery.id)).all():
        out.append({"kind": "commission_query_answered", "message_key": "todo.commission_query_answered",
                    "onboarding_id": onboarding_id, "customer_name": None, "priority": 3,
                    "params": {"query_id": query.id, "entry_id": query.entry_id}})
    return out + rate_items(db, associate, since)


def rate_items(db: Session, associate, since: datetime) -> list[dict]:
    """BE-12: each individual-rate change for this associate set since `since` or taking
    effect after today — the last one set for its date only (a same-day correction
    replaces the item). From/to are the rates the day before and from that date."""
    from app.commissions import periods, rates
    from app.commissions.serialize import pct
    from app.memory.models import AssociateCommissionRate

    today = periods.today_ist()
    rows = list(db.execute(select(AssociateCommissionRate).where(
        AssociateCommissionRate.associate_id == associate.id,
        (AssociateCommissionRate.created_at >= since) | (AssociateCommissionRate.effective_from > today))
        .order_by(AssociateCommissionRate.effective_from, AssociateCommissionRate.id)).scalars())
    latest = {(r.rate_key, r.effective_from): r for r in rows}
    out, cache = [], {}
    for row in latest.values():
        before, after = rates.change_of(db, associate, row, cache)
        out.append({"kind": "commission_rate_changed", "message_key": "todo.commission_rate_changed",
                    "onboarding_id": None, "customer_name": None, "priority": 3,
                    "params": {"rate_percent_from": pct(before), "rate_percent_to": pct(after),
                               "effective_from": row.effective_from.isoformat(),
                               "scheduled": row.effective_from > today}})
    return out
