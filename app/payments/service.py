"""Customer payments for associate-sourced projects — no cash anywhere.

    recorded ─► deposited (cheque / demand draft only, optional) ─► verified
        │              │                                              │
        ├─► cancelled  ├─► cancelled / dishonoured                    └─► dishonoured
        └─► dishonoured

`verified` means the funds are confirmed in Grid Nova's bank; there is no separate
"cleared" state. Verification writes a ledger CREDIT; dishonouring a verified payment
writes a REVERSAL that references it. payment_ledger_entries is append-only (a database
trigger refuses UPDATE/DELETE, and a unique (payment_id, kind) means two simultaneous
verifications of one payment produce one credit). A project's paid amount and balance
come from the ledger, never from payment statuses.

Money is Decimal throughout (Numeric(12, 2) in the database) and leaves the API as a
decimal string ("12345.67"). The accepted proposal's legacy float total_value is
converted once with Decimal(str(v)).quantize(Decimal("0.01")).

Milestones come from payments.schedule_template (config/execution_payments.yaml, P1).
Their due status is computed, never stored: not_due until the trigger stage is
recorded, then due → partially_paid → paid, against the CUMULATIVE verified amount (a
payment's milestone_id is a label for people, not an allocation). A milestone due and
unpaid for longer than payments.thresholds.milestone_overdue_days is overdue.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.execution import config
from app.memory.models import (
    INSTRUMENT_MODES, AuditLog, Customer, CustomerOnboarding, LedgerEntryKind, OnboardingStatus, Payment,
    PaymentLedgerEntry, PaymentMilestone, PaymentMode, PaymentStatus, Project, ProjectStage, Proposal,
    ProposalStatus,
)

CENT = Decimal("0.01")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def money(value) -> Decimal:
    """Exact rupees and paise. Floats go through str() first, never binary."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def s(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(Decimal(value).quantize(CENT))


def parse_amount(raw, field: str = "amount") -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise AssociateError("Enter the amount in rupees, e.g. 25000 or 25000.50.", code="invalid_amount",
                             status_code=422, field=field)
    if not value.is_finite() or value <= 0 or value != value.quantize(CENT) or value >= Decimal("10000000000"):
        raise AssociateError("Enter a positive amount with at most two decimal places.", code="invalid_amount",
                             status_code=422, field=field)
    return value.quantize(CENT)


def normalize_reference(raw: str) -> str:
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", raw or ""))
    return re.sub(r"\s+", "", text).upper()


def normalize_bank(raw: Optional[str]) -> str:
    text = unicodedata.normalize("NFKC", raw or "").lower()
    return re.sub(r"[^0-9a-z]+", "", text)


def _cfg() -> dict:
    return config.payments()


# ── The accepted total and the schedule ──────────────────────────────────────────

def accepted_proposal(db: Session, onboarding: CustomerOnboarding) -> Optional[Proposal]:
    return db.execute(select(Proposal).where(Proposal.lead_id == onboarding.lead_id,
                                             Proposal.status == ProposalStatus.accepted)
                      .order_by(Proposal.version.desc(), Proposal.id.desc())).scalars().first()


def accepted_total(db: Session, onboarding: CustomerOnboarding) -> Optional[Decimal]:
    proposal = accepted_proposal(db, onboarding)
    if proposal is None or not proposal.total_value:
        return None
    return money(proposal.total_value)


def split(total: Decimal, percents: list[Decimal]) -> list[Decimal]:
    """Amounts for each percentage of `total`, rounded to the paisa; the last takes
    the remainder so the parts sum to the total exactly."""
    parts = [(total * p / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP) for p in percents[:-1]]
    return parts + [total - sum(parts, Decimal(0))]


def milestones(db: Session, project_id: int) -> list[PaymentMilestone]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and project_id in pre.milestones:
        return list(pre.milestones[project_id])
    return list(db.execute(select(PaymentMilestone).where(PaymentMilestone.project_id == project_id)
                           .order_by(PaymentMilestone.number)).scalars())


def resolve_trigger(db: Session, onboarding: CustomerOnboarding, trigger) -> str:
    """A template trigger as the stage it means for THIS project: a list is "the first
    of these stages that applies" (e.g. inspection_net_meter where the project has it,
    else installed); the last is used when none applies."""
    if not isinstance(trigger, list):
        return trigger
    from app.onboarding import progress
    applies = {s.value for s in progress.applicable(db, onboarding)}
    return next((t for t in trigger if t in applies), trigger[-1])


def ensure_schedule(db: Session, onboarding: CustomerOnboarding, actor: str) -> list[PaymentMilestone]:
    """Create the schedule from the template the first time it is needed after
    acceptance. Idempotent; nothing (and no error) without an accepted total."""
    existing = milestones(db, onboarding.project_id)
    if existing:
        return existing
    proposal = accepted_proposal(db, onboarding)
    if proposal is None or not proposal.total_value:
        return []
    total = money(proposal.total_value)
    template = [{**m, "trigger": resolve_trigger(db, onboarding, m["trigger"])} for m in _cfg()["schedule_template"]]
    amounts = split(total, [Decimal(str(m["percent"])) for m in template])
    rows = []
    for m, amount in zip(template, amounts):
        row = PaymentMilestone(project_id=onboarding.project_id, onboarding_id=onboarding.id, proposal_id=proposal.id,
                               number=m["number"], label=m["label"], percent=Decimal(str(m["percent"])),
                               amount_due=amount, trigger=m["trigger"], created_by=actor, created_at=_now())
        db.add(row)
        rows.append(row)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:   # created concurrently by another request
        return milestones(db, onboarding.project_id)
    db.add(AuditLog(entity_type="payment_schedule", entity_id=str(onboarding.project_id), action="created",
                    performed_by=actor, after_value={"proposal_id": proposal.id, "total": s(total),
                                                     "milestones": [{"number": r.number, "percent": str(r.percent),
                                                                     "amount_due": s(r.amount_due)} for r in rows]}))
    return rows


def replace_schedule(db: Session, onboarding: CustomerOnboarding, items: list[dict], actor: str) -> list[PaymentMilestone]:
    """Accounts/Admin edit the schedule — only while no payment has been recorded on
    the project (cancelled ones aside). Percentages must sum to 100; amounts are
    recomputed from the accepted total. Audited with before and after."""
    total = accepted_total(db, onboarding)
    if total is None:
        raise AssociateError("There is no accepted proposal total to schedule against.", code="no_accepted_total",
                             status_code=409)
    if _live_payments(db, onboarding.project_id):
        raise AssociateError("A payment has been recorded; the schedule can no longer change.",
                             code="schedule_locked", status_code=409)
    current = milestones(db, onboarding.project_id) or ensure_schedule(db, onboarding, actor)
    if len(items) != len(current):
        # Milestones are never deleted, so the schedule keeps its shape: edit labels,
        # percentages and triggers of the existing milestones.
        raise AssociateError(f"Give exactly {len(current)} milestones.", code="invalid_schedule", status_code=422)
    stages = {st.value for st in ProjectStage}
    try:
        percents = [Decimal(str(i["percent"])) for i in items]
    except (InvalidOperation, KeyError):
        raise AssociateError("Each milestone needs a percent.", code="invalid_schedule", status_code=422)
    if any(p <= 0 for p in percents) or sum(percents) != 100:
        raise AssociateError("Percentages must be positive and add up to 100.", code="invalid_schedule",
                             status_code=422)
    if any(not (i.get("label") or "").strip() or i.get("trigger") not in stages for i in items):
        raise AssociateError("Each milestone needs a label and a trigger stage.", code="invalid_schedule",
                             status_code=422)
    before = [{"number": m.number, "label": m.label, "percent": str(m.percent), "amount_due": s(m.amount_due),
               "trigger": m.trigger} for m in milestones(db, onboarding.project_id)]
    proposal = accepted_proposal(db, onboarding)
    existing = {m.number: m for m in milestones(db, onboarding.project_id)}
    amounts = split(total, percents)
    for number, (item, amount, pct) in enumerate(zip(items, amounts, percents), start=1):
        row = existing[number]
        row.proposal_id, row.label, row.percent, row.amount_due, row.trigger = (
            proposal.id, item["label"].strip(), pct, amount, item["trigger"])
        row.updated_at = _now()
    db.flush()
    after = [{"number": m.number, "label": m.label, "percent": str(m.percent), "amount_due": s(m.amount_due),
              "trigger": m.trigger} for m in milestones(db, onboarding.project_id)]
    db.add(AuditLog(entity_type="payment_schedule", entity_id=str(onboarding.project_id), action="updated",
                    performed_by=actor, before_value={"milestones": before}, after_value={"milestones": after}))
    return milestones(db, onboarding.project_id)


# ── The ledger ───────────────────────────────────────────────────────────────────

def paid(db: Session, project_id: int) -> Decimal:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and project_id in pre.ledger_totals:
        return money(pre.ledger_totals[project_id])
    total = db.execute(select(func.coalesce(func.sum(PaymentLedgerEntry.amount), 0)).where(
        PaymentLedgerEntry.project_id == project_id)).scalar_one()
    return money(total)


def _live_payments(db: Session, project_id: int) -> list[Payment]:
    return list(db.execute(select(Payment).where(Payment.project_id == project_id,
                                                 Payment.status != PaymentStatus.cancelled)).scalars())


def milestone_status(db: Session, onboarding: CustomerOnboarding, now: Optional[datetime] = None) -> list[dict]:
    """Each milestone with its computed due status against the cumulative verified
    amount. Amounts as decimal strings."""
    from app.onboarding import progress

    now = now or _now()
    rows = milestones(db, onboarding.project_id)
    done = progress.recorded(db, onboarding.project_id)
    remaining = paid(db, onboarding.project_id)
    overdue_days = _cfg()["thresholds"]["milestone_overdue_days"]
    out = []
    for m in rows:
        toward = min(max(remaining, Decimal(0)), m.amount_due)
        remaining -= toward
        try:
            trigger = ProjectStage(m.trigger)
        except ValueError:
            trigger = None
        event = done.get(trigger) if trigger else None
        if toward >= m.amount_due:
            state = "paid"
        elif event is None:
            state = "not_due"
        elif toward > 0:
            state = "partially_paid"
        else:
            state = "due"
        due_since = _aware(event.recorded_at) if event else None
        overdue = state in ("due", "partially_paid") and due_since is not None and \
            now - due_since > timedelta(days=overdue_days)
        out.append({"milestone_id": m.id, "number": m.number, "label": m.label, "percent": str(m.percent),
                    "amount_due": s(m.amount_due), "paid_toward": s(toward),
                    "outstanding": s(m.amount_due - toward), "trigger": m.trigger, "due_status": state,
                    "due_since": due_since.isoformat() if due_since else None, "overdue": overdue})
    return out


def milestones_verified_through(db: Session, project_id: int, number: int) -> bool:
    """Every milestone up to and including `number` fully covered by verified money
    (the E6 dispatch gate)."""
    rows = [m for m in milestones(db, project_id) if m.number <= number]
    if not rows:
        return False
    return paid(db, project_id) >= sum((m.amount_due for m in rows), Decimal(0))


def totals(db: Session, onboarding: CustomerOnboarding) -> dict:
    total = accepted_total(db, onboarding)
    received = paid(db, onboarding.project_id)
    pending = sum((p.amount for p in _live_payments(db, onboarding.project_id)
                   if p.status in (PaymentStatus.recorded, PaymentStatus.deposited)), Decimal(0))
    return {"accepted_total": s(total), "paid_verified": s(received), "awaiting_verification": s(pending),
            "balance": s(total - received) if total is not None else None}


# ── Recording and moving a payment ───────────────────────────────────────────────

def onboarding_of(db: Session, payment: Payment) -> Optional[CustomerOnboarding]:
    """The associate-sourced customer record behind a payment, if it has one (the key
    of a payment is its project; onboarding_id is NULL for any other kind of project)."""
    return db.get(CustomerOnboarding, payment.onboarding_id) if payment.onboarding_id is not None else None


def customer_of(db: Session, payment: Payment) -> Optional[Customer]:
    project = db.get(Project, payment.project_id)
    return db.get(Customer, project.customer_id) if project is not None else None


def _payment_for_update(db: Session, payment_id: int) -> Optional[Payment]:
    return db.execute(select(Payment).where(Payment.id == payment_id).with_for_update()).scalar_one_or_none()


def record(db: Session, onboarding: CustomerOnboarding, data: dict, actor: str, *, by_associate: bool) -> Payment:
    from app.onboarding import progress

    if onboarding.status == OnboardingStatus.closed:
        raise AssociateError("This customer record is closed.", code="not_editable", status_code=409)
    if ProjectStage.accepted not in progress.recorded(db, onboarding.project_id):
        raise AssociateError("Payments are recorded once the customer has accepted the proposal.",
                             code="not_accepted", status_code=409)
    total = accepted_total(db, onboarding)
    if total is None:
        raise AssociateError("The accepted proposal has no total yet; ask Grid Nova to set it.",
                             code="no_accepted_total", status_code=409)
    ensure_schedule(db, onboarding, actor)
    amount = parse_amount(data.get("amount"))
    try:
        mode = PaymentMode(data.get("mode"))
    except ValueError:
        mode = None
    if mode is None or mode.value not in _cfg()["enabled_modes"]:
        raise AssociateError(f"Payment mode must be one of {_cfg()['enabled_modes']} (cash is never accepted).",
                             code="mode_not_enabled", status_code=422, field="mode")
    reference = (data.get("reference") or "").strip()
    ref_norm = normalize_reference(reference)
    if not (3 <= len(ref_norm) <= 100):
        raise AssociateError("Enter the cheque/DD number or the transaction reference (UTR).",
                             code="reference_required", status_code=422, field="reference")
    bank = (data.get("bank_name") or "").strip() or None
    instrument_date = data.get("instrument_date")
    if isinstance(instrument_date, str) and instrument_date:
        try:
            instrument_date = date.fromisoformat(instrument_date)
        except ValueError:
            raise AssociateError("instrument_date must be a date (YYYY-MM-DD).", code="invalid_date",
                                 status_code=422, field="instrument_date")
    if mode in INSTRUMENT_MODES and (not bank or not instrument_date):
        raise AssociateError("A cheque or demand draft needs the bank name and its date.",
                             code="instrument_details_required", status_code=422,
                             field="bank_name" if not bank else "instrument_date")
    milestone_id = data.get("milestone_id")
    if milestone_id is not None and not any(m.id == milestone_id for m in milestones(db, onboarding.project_id)):
        raise AssociateError("That milestone is not on this project.", code="unknown_milestone", status_code=422,
                             field="milestone_id")
    tolerance = Decimal(str(_cfg()["thresholds"]["overpayment_tolerance_inr"]))
    # Lock the project row before summing: two payments recorded at the same moment
    # must not both pass the overpayment check (SELECT … FOR UPDATE on PostgreSQL;
    # SQLite serialises writers anyway). Held until this transaction ends.
    db.execute(select(Project.id).where(Project.id == onboarding.project_id).with_for_update())
    counted = sum((p.amount for p in _live_payments(db, onboarding.project_id)
                   if p.status != PaymentStatus.dishonoured), Decimal(0))
    if counted + amount > total + tolerance:
        raise AssociateError("This payment would take the total received above the accepted proposal total.",
                             code="exceeds_project_total", status_code=409,
                             extra={"accepted_total": s(total), "already_recorded": s(counted)})
    previous_id = _previous_dishonoured(db, onboarding.project_id, mode, ref_norm, normalize_bank(bank))
    payment = Payment(project_id=onboarding.project_id, onboarding_id=onboarding.id, milestone_id=milestone_id,
                      previous_payment_id=previous_id,
                      amount=amount, mode=mode, reference=reference[:100], reference_normalized=ref_norm,
                      bank_name=bank, bank_name_normalized=normalize_bank(bank), instrument_date=instrument_date or None,
                      payer_name=((data.get("payer_name") or "").strip() or None), status=PaymentStatus.recorded,
                      recorded_by=actor, recorded_at=_now())
    try:
        with db.begin_nested():
            db.add(payment)
            db.flush()
    except IntegrityError:
        raise AssociateError("This payment (same mode, reference and bank) is already recorded.",
                             code="duplicate_payment_reference", status_code=409, field="reference")
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="recorded", performed_by=actor,
                    after_value={"onboarding_id": onboarding.id, "amount": s(amount), "mode": mode.value,
                                 "by_associate": by_associate, "previous_payment_id": previous_id}))
    _notify(db, "payment_recorded", payment)
    return payment


def _previous_dishonoured(db: Session, project_id: int, mode: PaymentMode, ref_norm: str,
                          bank_norm: str) -> Optional[int]:
    """A dishonoured payment releases its reference, so the same cheque may be presented
    again on the SAME project — linked to the bounce. One dishonoured on a different
    project is refused (an instrument belongs to one project), and a live or verified
    payment with the reference is refused by the unique index at insert."""
    rows = list(db.execute(select(Payment).where(
        Payment.mode == mode, Payment.reference_normalized == ref_norm, Payment.bank_name_normalized == bank_norm,
        Payment.status == PaymentStatus.dishonoured).order_by(Payment.id.desc())).scalars())
    if any(p.project_id != project_id for p in rows):
        raise AssociateError("This payment (same mode, reference and bank) is already recorded on another project.",
                             code="duplicate_payment_reference", status_code=409, field="reference")
    return rows[0].id if rows else None


def attach_proof(db: Session, payment: Payment, *, key: str, filename: str, content_type: str, actor: str) -> Payment:
    if payment.status not in (PaymentStatus.recorded, PaymentStatus.deposited):
        raise AssociateError(f"Proof cannot be added to a {payment.status.value} payment.", code="payment_not_open",
                             status_code=409)
    before = {"proof_filename": payment.proof_filename}
    payment.proof_key, payment.proof_filename, payment.proof_content_type = key, filename, content_type
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="proof_attached", performed_by=actor,
                    before_value=before, after_value={"proof_filename": filename}))
    db.flush()
    return payment


def _move(payment: Payment, allowed: tuple, target: str) -> None:
    if payment.status not in allowed:
        raise AssociateError(f"A {payment.status.value} payment cannot be {target}.", code="invalid_transition",
                             status_code=409)


def deposit(db: Session, payment_id: int, actor: str) -> Payment:
    payment = _payment_for_update(db, payment_id)
    if payment.mode not in INSTRUMENT_MODES:
        raise AssociateError("Only a cheque or demand draft is deposited.", code="not_an_instrument",
                             status_code=409)
    _move(payment, (PaymentStatus.recorded,), "deposited")
    payment.status, payment.deposited_by, payment.deposited_at = PaymentStatus.deposited, actor, _now()
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="deposited", performed_by=actor,
                    before_value={"status": "recorded"}, after_value={"status": "deposited"}))
    db.flush()
    return payment


def proof_missing(payment: Payment) -> bool:
    return (_cfg()["proof_required_for_associates"] and payment.recorded_by.startswith("associate:")
            and not payment.proof_key)


def verify(db: Session, payment_id: int, actor: str, *, is_admin: bool = False,
           reason: Optional[str] = None) -> Payment:
    """Funds confirmed in Grid Nova's bank: status verified + one ledger credit. The
    row lock and the ledger's unique (payment_id, kind) make a double verification
    impossible even when two requests race.

    Segregation of duties: nobody verifies a payment they recorded themselves. Only an
    Admin may, and then with a reason, audited as `segregation_override`."""
    payment = _payment_for_update(db, payment_id)
    _move(payment, (PaymentStatus.recorded, PaymentStatus.deposited), "verified")
    override = None
    if payment.recorded_by == actor:
        if not is_admin:
            raise AssociateError("A payment must be verified by someone other than the person who recorded it.",
                                 code="self_verification", status_code=409)
        override = (reason or "").strip()
        if not override:
            raise AssociateError("Say why you are verifying a payment you recorded yourself.",
                                 code="reason_required", status_code=422, field="reason")
    if proof_missing(payment):
        raise AssociateError("The associate has not attached proof of this payment yet.", code="proof_required",
                             status_code=409)
    before = payment.status.value
    payment.status, payment.verified_by, payment.verified_at = PaymentStatus.verified, actor, _now()
    credit = PaymentLedgerEntry(payment_id=payment.id, project_id=payment.project_id, kind=LedgerEntryKind.credit,
                                amount=payment.amount, actor=actor, created_at=_now())
    try:
        with db.begin_nested():
            db.add(credit)
            db.flush()
    except IntegrityError:
        raise AssociateError("This payment is already verified.", code="invalid_transition", status_code=409)
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="verified", performed_by=actor,
                    before_value={"status": before},
                    after_value={"status": "verified", "ledger_entry_id": credit.id, "amount": s(payment.amount),
                                 "segregation_override": override}))
    if override:
        db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="segregation_override",
                        performed_by=actor, after_value={"recorded_by": payment.recorded_by, "reason": override}))
    from app.payments import receipts
    receipts.on_verified(db, payment, onboarding_of(db, payment), actor)
    return payment


def dishonour(db: Session, payment_id: int, actor: str, reason: str) -> Payment:
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the payment was dishonoured.", code="reason_required", status_code=422,
                             field="reason")
    payment = _payment_for_update(db, payment_id)
    _move(payment, (PaymentStatus.recorded, PaymentStatus.deposited, PaymentStatus.verified), "dishonoured")
    before = payment.status.value
    reversal = None
    if payment.status == PaymentStatus.verified:
        credit = db.execute(select(PaymentLedgerEntry).where(PaymentLedgerEntry.payment_id == payment.id,
                                                             PaymentLedgerEntry.kind == LedgerEntryKind.credit)
                            ).scalar_one()
        reversal = PaymentLedgerEntry(payment_id=payment.id, project_id=payment.project_id,
                                      kind=LedgerEntryKind.reversal, amount=-credit.amount,
                                      reverses_entry_id=credit.id, actor=actor, created_at=_now())
        try:
            with db.begin_nested():
                db.add(reversal)
                db.flush()
        except IntegrityError:
            raise AssociateError("This payment is already dishonoured.", code="invalid_transition", status_code=409)
    payment.status, payment.dishonoured_by, payment.dishonoured_at = PaymentStatus.dishonoured, actor, _now()
    payment.dishonour_reason = reason
    if reversal is not None:
        from app.payments import receipts
        receipts.void(db, payment, actor, reason)
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="dishonoured", performed_by=actor,
                    before_value={"status": before},
                    after_value={"status": "dishonoured", "reason": reason,
                                 "reversal_entry_id": reversal.id if reversal else None}))
    db.flush()
    _notify(db, "payment_dishonoured", payment)
    return payment


def cancel(db: Session, payment_id: int, actor: str, reason: str, *, by_associate: bool) -> Payment:
    """An associate may cancel only their own `recorded` payment; Accounts/Admin a
    recorded or deposited one. A verified payment is never cancelled — dishonour it."""
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the payment is being cancelled.", code="reason_required", status_code=422,
                             field="reason")
    payment = _payment_for_update(db, payment_id)
    if by_associate:
        if payment.recorded_by != actor:
            raise AssociateError("You can cancel only a payment you recorded.", code="not_yours", status_code=403)
        _move(payment, (PaymentStatus.recorded,), "cancelled")
    else:
        _move(payment, (PaymentStatus.recorded, PaymentStatus.deposited), "cancelled")
    before = payment.status.value
    payment.status, payment.cancelled_by, payment.cancelled_at, payment.cancel_reason = (
        PaymentStatus.cancelled, actor, _now(), reason)
    db.add(AuditLog(entity_type="payment", entity_id=str(payment.id), action="cancelled", performed_by=actor,
                    before_value={"status": before}, after_value={"status": "cancelled", "reason": reason}))
    db.flush()
    return payment


# ── Attention and serialization ──────────────────────────────────────────────────

def attention(payment: Payment, now: Optional[datetime] = None, receipt=None) -> list[str]:
    now = now or _now()
    th = _cfg()["thresholds"]
    reasons = []
    if payment.status in (PaymentStatus.recorded, PaymentStatus.deposited):
        if now - _aware(payment.recorded_at) > timedelta(hours=th["awaiting_verification_hours"]):
            reasons.append("awaiting_verification")
        if payment.status == PaymentStatus.deposited and payment.deposited_at and \
                now - _aware(payment.deposited_at) > timedelta(days=th["cheque_hold_days"]):
            reasons.append("cheque_hold_exceeded")
        if proof_missing(payment):
            reasons.append("proof_missing")
    if receipt is not None:
        from app.payments import receipts
        reasons += receipts.attention(receipt)
    return reasons


_LOOKUP = object()


def payment_json(db: Session, payment: Payment, *, staff: bool = True, receipt=_LOOKUP) -> dict:
    """`receipt` may be passed in (a list page loads every row's receipt in one query);
    by default it is looked up."""
    from app.payments import receipts

    if receipt is _LOOKUP:
        receipt = receipts.for_payment(db, payment.id)
    body = {
        "payment_id": payment.id, "onboarding_id": payment.onboarding_id, "milestone_id": payment.milestone_id,
        "previous_payment_id": payment.previous_payment_id,
        "amount": s(payment.amount), "mode": payment.mode.value, "reference": payment.reference,
        "bank_name": payment.bank_name,
        "instrument_date": payment.instrument_date.isoformat() if payment.instrument_date else None,
        "payer_name": payment.payer_name, "status": payment.status.value,
        "has_proof": bool(payment.proof_key), "proof_filename": payment.proof_filename,
        "recorded_by": payment.recorded_by, "recorded_at": _aware(payment.recorded_at).isoformat(),
        "deposited_at": _aware(payment.deposited_at).isoformat() if payment.deposited_at else None,
        "verified_at": _aware(payment.verified_at).isoformat() if payment.verified_at else None,
        "dishonoured_at": _aware(payment.dishonoured_at).isoformat() if payment.dishonoured_at else None,
        "dishonour_reason": payment.dishonour_reason,
        "cancelled_at": _aware(payment.cancelled_at).isoformat() if payment.cancelled_at else None,
        "cancel_reason": payment.cancel_reason,
        "receipt": receipts.receipt_json(receipt, staff=staff) if receipt else None,
    }
    if staff:
        body.update(deposited_by=payment.deposited_by, verified_by=payment.verified_by,
                    dishonoured_by=payment.dishonoured_by, cancelled_by=payment.cancelled_by)
        reasons = attention(payment, receipt=receipt)
        body.update(needs_attention=bool(reasons), attention_reasons=reasons)
    return body


def payment_project_summary(db: Session, payment: Payment) -> dict:
    """The project summary for a payment — from its onboarding when it has one, else
    from the project alone (no stage, no accepted total)."""
    onboarding = onboarding_of(db, payment)
    if onboarding is not None:
        return project_summary(db, onboarding)
    customer, project = customer_of(db, payment), db.get(Project, payment.project_id)
    received = paid(db, payment.project_id)
    return {"onboarding_id": None, "customer_name": customer.name if customer else None,
            "customer_email": customer.email if customer else None,
            "project_reference_id": project.reference_id if project else None, "current_stage": None,
            "accepted_total": None, "paid_verified": s(received), "awaiting_verification": s(Decimal(0)),
            "balance": None}


def project_summary(db: Session, onboarding: CustomerOnboarding) -> dict:
    """What Accounts needs about the project — no execution detail."""
    from app.onboarding import progress

    customer = db.get(Customer, onboarding.customer_id)
    project = db.get(Project, onboarding.project_id)
    timeline = progress.timeline(db, onboarding)
    return {"onboarding_id": onboarding.id, "customer_name": customer.name if customer else None,
            "customer_email": customer.email if customer else None,
            "project_reference_id": project.reference_id if project else None,
            "current_stage": timeline["current_stage"], **totals(db, onboarding)}


def _notify(db: Session, event: str, payment: Payment) -> None:
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module

    customer = customer_of(db, payment)
    payload = {"payment_id": payment.id, "onboarding_id": payment.onboarding_id, "amount": s(payment.amount),
               "mode": payment.mode.value, "customer_name": customer.name if customer else None,
               "recorded_by": payment.recorded_by, "reason": payment.dishonour_reason}
    MemoryManager(db).notify_after_commit(lambda: notifier_module.notifier.notify(event, payload), event)
