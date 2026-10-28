"""Payments for direct customers — a lead with no associate: a website enquiry, a
staff-entered lead, or a Proposal Studio proposal converted to a lead (BE-20).

The same machinery as associate customers (app/payments/service.py), keyed the same way:
payment_milestones and payments rows on the lead's project, with lead_id set and
onboarding_id NULL. The ledger, verification (second pair of eyes), dishonour, cancel,
receipts and the public receipt link are the shared code; what differs is only where the
schedule and the "accepted" fact come from:

  * the accepted proposal is the lead's newest `accepted` Proposal (customer acceptance is
    final: online via the accept link, or a Studio acceptance carried by conversion);
  * the schedule is that proposal's own payment terms — the document's payment_terms with
    figures()["payments"] amounts (they sum to its total exactly) — and, for a proposal
    drafted before the document existed, the price-list template against total_value,
    exactly as associate customers get it;
  * the accepted total is the schedule's sum, fixed when the schedule was made (a later
    change of GST or price-list config never moves what the customer signed for).

Due status: the advance's trigger (`accepted`) is the proposal's accepted_at. Direct
projects have no execution stage log, so milestones after the advance are `not_due` until
paid (registry DP-2). No commission is ever earned: commissions.on_payment_verified needs
an onboarding. Refunds and cancellation of a direct order are not built (registry DP-1).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.models import (
    AuditLog, Customer, CustomerOnboarding, Lead, Payment, PaymentMilestone, PaymentStatus, Project, ProjectStage,
    Proposal, ProposalStatus, TimelineEventType,
)
from app.payments import service as payments
from app.payments.service import money, s

logger = logging.getLogger(__name__)


def onboarding_id_of(db: Session, lead: Lead) -> Optional[int]:
    return db.execute(select(CustomerOnboarding.id).where(CustomerOnboarding.lead_id == lead.id)).scalar_one_or_none()


def require_direct(db: Session, lead: Lead) -> None:
    """A lead brought by an associate keeps its payments on its customer record."""
    onboarding_id = onboarding_id_of(db, lead)
    if onboarding_id is not None:
        raise AssociateError("This customer was brought by an associate; their payments are on the customer record.",
                             code="associate_customer", status_code=409, extra={"onboarding_id": onboarding_id})


def accepted_proposal(db: Session, lead: Lead) -> Optional[Proposal]:
    """Same order as payments.accepted_proposal: the newest accepted version."""
    return db.execute(select(Proposal).where(Proposal.lead_id == lead.id, Proposal.status == ProposalStatus.accepted)
                      .order_by(Proposal.version.desc(), Proposal.id.desc())).scalars().first()


def _first_stage(trigger) -> str:
    """A template trigger without an onboarding: a list is "the first of these stages that
    applies", and with the project's details unknown every stage applies (as
    progress.applicable treats unknowns), so the first."""
    return trigger[0] if isinstance(trigger, list) else trigger


def _trigger(number: int) -> str:
    """The due trigger for milestone `number`: the template's trigger at that position (the
    last one beyond it) — the same unconfirmed reading as associate customers (P9)."""
    template = payments._cfg()["schedule_template"]
    return _first_stage(template[min(number, len(template)) - 1]["trigger"])


def terms(proposal: Proposal) -> Optional[dict]:
    """{source, total, items: [{number, label, percent, amount}]} for the accepted
    proposal, or None while it has no priced terms."""
    from app.proposals import document as d

    doc = d.stored(proposal.content)
    if doc is not None and doc.payment_terms:
        figs = d.figures(doc)
        if figs.get("total") and len(figs.get("payments") or []) == len(doc.payment_terms):
            items = [{"number": n, "label": t.label, "percent": Decimal(str(t.percent)), "amount": Decimal(p["amount"])}
                     for n, (t, p) in enumerate(zip(doc.payment_terms, figs["payments"]), start=1)]
            return {"source": "document", "total": Decimal(figs["total"]), "items": items}
    if doc is None and proposal.total_value:
        # Drafted before BE-13's document: the price-list schedule, as associate customers.
        total = money(proposal.total_value)
        template = payments._cfg()["schedule_template"]
        amounts = payments.split(total, [Decimal(str(m["percent"])) for m in template])
        return {"source": "price_list", "total": total,
                "items": [{"number": m["number"], "label": m["label"], "percent": Decimal(str(m["percent"])),
                           "amount": a} for m, a in zip(template, amounts)]}
    return None


def ensure_schedule(db: Session, lead: Lead, actor: str) -> list[PaymentMilestone]:
    """Create the direct customer's schedule from the accepted proposal's terms, once.
    Idempotent (the existing rows, or the unique (project_id, number) when two requests
    race); nothing, and no error, before acceptance or without priced terms."""
    existing = payments.milestones(db, lead.project_id)
    if existing:
        return existing
    if onboarding_id_of(db, lead) is not None:
        return []
    proposal = accepted_proposal(db, lead)
    found = terms(proposal) if proposal is not None else None
    if found is None:
        return []
    rows = [PaymentMilestone(project_id=lead.project_id, lead_id=lead.id, proposal_id=proposal.id,
                             number=item["number"], label=item["label"][:200], percent=item["percent"],
                             amount_due=item["amount"], trigger=_trigger(item["number"]), created_by=actor,
                             created_at=payments._now())
            for item in found["items"]]
    db.add_all(rows)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:   # created concurrently by another request
        return payments.milestones(db, lead.project_id)
    after = {"lead_id": lead.id, "proposal_id": proposal.id, "source": found["source"], "total": s(found["total"]),
             "milestones": [{"number": r.number, "percent": str(r.percent), "amount_due": s(r.amount_due)}
                            for r in rows]}
    if proposal.total_value and money(proposal.total_value) != found["total"]:
        after["proposal_total_value"] = s(money(proposal.total_value))   # recorded, never used
    db.add(AuditLog(entity_type="payment_schedule", entity_id=str(lead.project_id), action="created",
                    performed_by=actor, after_value=after))
    return rows


def on_accepted(db: Session, proposal: Proposal, actor: str) -> None:
    """Hook after a lead proposal becomes accepted (MemoryManager.mark_customer_accepted,
    studio convert). Never breaks the acceptance it rides on: a failure is logged and the
    schedule is made the next time it is read."""
    lead = db.get(Lead, proposal.lead_id)
    try:
        ensure_schedule(db, lead, actor)
    except Exception:
        logger.exception("Direct payment schedule not created for lead %s", proposal.lead_id)


def scheduled_total(db: Session, project_id: int) -> Optional[Decimal]:
    rows = payments.milestones(db, project_id)
    return sum((m.amount_due for m in rows), Decimal(0)) if rows else None


def milestone_status(db: Session, lead: Lead, now: Optional[datetime] = None) -> list[dict]:
    proposal = accepted_proposal(db, lead)
    accepted_at = proposal.accepted_at if proposal is not None else None
    return payments.status_rows(payments.milestones(db, lead.project_id), payments.paid(db, lead.project_id),
                                lambda stage: accepted_at if stage == ProjectStage.accepted else None, now)


def totals(db: Session, lead: Lead) -> dict:
    """The same shape as payments.totals."""
    total = scheduled_total(db, lead.project_id)
    received = payments.paid(db, lead.project_id)
    pending = sum((p.amount for p in payments._live_payments(db, lead.project_id)
                   if p.status in (PaymentStatus.recorded, PaymentStatus.deposited)), Decimal(0))
    return {"accepted_total": s(total), "paid_verified": s(received), "awaiting_verification": s(pending),
            "balance": s(total - received) if total is not None else None}


def _not_future(when: date) -> None:
    from app.timezones import _as_ist

    if when > _as_ist(payments._now()).date():
        raise AssociateError("The payment date cannot be in the future.", code="date_in_future", status_code=422,
                             field="instrument_date")


def record(db: Session, lead: Lead, data: dict, actor: str) -> Payment:
    """Accounts / Admin record a direct customer's payment. The same rules as for an
    associate customer (modes, references, cheque details, duplicates, the project lock
    and cumulative over-payment check), plus: the date may not be in the future, and
    over-payment answers 409 exceeds_outstanding."""
    require_direct(db, lead)
    if accepted_proposal(db, lead) is None:
        raise AssociateError("Payments are recorded once the customer has accepted the proposal.",
                             code="not_accepted", status_code=409)
    if not ensure_schedule(db, lead, actor):
        raise AssociateError("The accepted proposal has no priced payment terms yet.", code="no_accepted_total",
                             status_code=409)
    payment = payments.record_payment(db, project_id=lead.project_id, total=scheduled_total(db, lead.project_id),
                                      data=data, actor=actor, owner={"lead_id": lead.id}, by_associate=False,
                                      overpayment_code="exceeds_outstanding", check_instrument_date=_not_future)
    _timeline(db, payment, TimelineEventType.payment_recorded, actor,
              f"Payment recorded: INR {s(payment.amount)} by {payment.mode.value.upper()} ({payment.reference})")
    return payment


def on_verified(db: Session, payment: Payment, actor: str) -> None:
    """Inside payments.verify for a direct customer's payment: timeline and the alert."""
    _timeline(db, payment, TimelineEventType.payment_verified, actor,
              f"Payment verified: INR {s(payment.amount)} by {payment.mode.value.upper()} ({payment.reference})")
    payments.notify(db, "payment_verified", payment)


def on_dishonoured(db: Session, payment: Payment, actor: str) -> None:
    _timeline(db, payment, TimelineEventType.payment_dishonoured, actor,
              f"Payment dishonoured: INR {s(payment.amount)} by {payment.mode.value.upper()} — "
              f"{payment.dishonour_reason}")


def _timeline(db: Session, payment: Payment, event: TimelineEventType, actor: str, text: str) -> None:
    from app.memory.memory_manager import MemoryManager
    MemoryManager(db).add_timeline_event(lead_id=payment.lead_id, event_type=event, created_by=actor,
                                         description=text[:500])


def project_summary(db: Session, lead: Lead) -> dict:
    """The same shape as payments.project_summary, plus lead_id."""
    customer = db.get(Customer, lead.customer_id)
    project = db.get(Project, lead.project_id)
    return {"onboarding_id": None, "lead_id": lead.id, "customer_name": customer.name if customer else None,
            "customer_email": customer.email if customer else None,
            "project_reference_id": project.reference_id if project else None, "current_stage": None,
            **totals(db, lead)}


def overview(db: Session, lead: Lead) -> dict:
    """Milestones (with due status), payments (staff view) and totals — the shape of the
    associate customer's overview, keyed by lead_id."""
    from app.execution import config

    rows = db.execute(select(Payment).where(Payment.lead_id == lead.id)
                      .order_by(Payment.recorded_at.desc(), Payment.id.desc())).scalars()
    proposal = accepted_proposal(db, lead)
    return {"onboarding_id": None, "lead_id": lead.id,
            "proposal_id": proposal.id if proposal is not None else None,
            "totals": totals(db, lead), "milestones": milestone_status(db, lead),
            "payments": [payments.payment_json(db, p) for p in rows],
            "enabled_modes": list(config.payments()["enabled_modes"]), "proof_required": False}
