"""Monthly payouts and TDS — Commission & Payout Policy 4.5 and 4.6.

A batch is one calendar month: Accounts prepares it after the month has ended, someone
else approves it, and Accounts marks it paid with a bank reference per associate. It
takes every APPROVED, rated entry that appeared before the end of the month and is not
in another batch; recoveries (negative entries) are netted against the same associate's
commission, and an unrecovered balance carries forward to their next payout.

Order is enforced so the financial-year TDS arithmetic is always over complete
history: one batch per month (a partial unique index), months in order, and no new
batch while another is still a draft. Unrated entries never reach a batch, and while
any entry of the month is still waiting for confirmed rates the batch is refused
(placeholder_rates) — no money under unconfirmed rates.

BE-19: a SUSPENDED associate's commission is frozen — their approved entries stay out of
every batch (still approved, still unbatched) and are paid in the first batch prepared
after reinstatement. A deactivated associate's earned commission is settled as usual
(Network Terms A11: commission earned before termination stays payable).

TDS (4.6) is ONE function, tds_due, with the draft rule from the policy; a Chartered
Accountant must confirm it (placeholder registry C5-11).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.commissions import config, periods
from app.memory.models import (
    Associate, AssociateApplication, AssociateStatus, AuditLog, CommissionEntry, CommissionEntryKind, CommissionEntryStatus,
    PayoutBatch, PayoutBatchStatus, PayoutLine, PreferredAssociateApplication,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
LIVE = (PayoutBatchStatus.draft, PayoutBatchStatus.approved, PayoutBatchStatus.paid)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, batch_id, action: str, actor: str, after=None, before=None) -> None:
    db.add(AuditLog(entity_type="payout_batch", entity_id=str(batch_id), action=action, performed_by=actor,
                    before_value=before, after_value=after))


def _s(v: Decimal) -> str:
    return str(Decimal(v).quantize(CENT))


# ── TDS (draft rule — for the Chartered Accountant to confirm) ───────────────────

def tds_due(*, fy_taxable_before: Decimal, fy_tds_before: Decimal, taxable_now: Decimal, pan_on_file: bool,
            cfg: Optional[dict] = None) -> tuple[Decimal, Decimal]:
    """(TDS to deduct from this payout, the rate applied).

    Draft rule (policy 4.6): nothing while the associate's taxable commission in the
    financial year, including this payout, is at or below the threshold. Once it is
    above, TDS is due on the WHOLE financial-year-to-date amount at the rate (the
    no-PAN rate without a PAN on file), less the TDS already deducted this year — so
    the month the threshold is crossed deducts on everything earned so far that year.
    Never more than this payout's taxable amount. Rounded per tds.rounding."""
    cfg = cfg or config.tds()
    rate = Decimal(str(cfg["rate_percent"] if pan_on_file else cfg["no_pan_rate_percent"]))
    threshold = Decimal(str(cfg["fy_threshold_inr"]))
    quantum = Decimal(str(cfg["rounding"]))
    total = fy_taxable_before + taxable_now
    if taxable_now <= 0 or total <= threshold:
        return ZERO, rate
    due = (total * rate / 100).quantize(quantum, rounding=ROUND_HALF_UP)
    tds = min(max(due - fy_tds_before, ZERO), taxable_now)
    return tds.quantize(CENT), rate


# ── Reading payout history ───────────────────────────────────────────────────────

def application_of(db: Session, associate: Associate):
    if associate.associate_application_id is not None:
        return db.get(AssociateApplication, associate.associate_application_id)
    return db.get(PreferredAssociateApplication, associate.preferred_application_id)


def fy_totals(db: Session, associate_ids, fy: str) -> dict[int, tuple[Decimal, Decimal]]:
    """associate id → (taxable, TDS) over the financial year's batches that are not
    cancelled — the TDS ledger is the payout lines."""
    ids = list(associate_ids)
    if not ids:
        return {}
    rows = db.execute(select(PayoutLine.associate_id, func.coalesce(func.sum(PayoutLine.taxable), 0),
                             func.coalesce(func.sum(PayoutLine.tds), 0))
                      .join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                      .where(PayoutLine.associate_id.in_(ids), PayoutBatch.financial_year == fy,
                             PayoutBatch.status != PayoutBatchStatus.cancelled)
                      .group_by(PayoutLine.associate_id)).all()
    found = {a: (Decimal(t).quantize(CENT), Decimal(d).quantize(CENT)) for a, t, d in rows}
    return {i: found.get(i, (ZERO, ZERO)) for i in ids}


def carried(db: Session, associate_ids) -> dict[int, Decimal]:
    """associate id → the balance still to recover, from their latest payout line."""
    ids = list(associate_ids)
    if not ids:
        return {}
    latest = (select(PayoutLine.associate_id, func.max(PayoutBatch.period).label("period"))
              .join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
              .where(PayoutLine.associate_id.in_(ids), PayoutBatch.status != PayoutBatchStatus.cancelled)
              .group_by(PayoutLine.associate_id).subquery())
    rows = db.execute(select(PayoutLine.associate_id, PayoutLine.carry_forward)
                      .join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                      .join(latest, (latest.c.associate_id == PayoutLine.associate_id)
                            & (latest.c.period == PayoutBatch.period))
                      .where(PayoutBatch.status != PayoutBatchStatus.cancelled)).all()
    found = {a: Decimal(c).quantize(CENT) for a, c in rows}
    return {i: found.get(i, ZERO) for i in ids}


# ── The batch lifecycle ──────────────────────────────────────────────────────────

def _locked(db: Session, batch_id: int) -> PayoutBatch:
    batch = db.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id).with_for_update()
                       .execution_options(populate_existing=True)).scalar_one_or_none()
    if batch is None:
        raise AssociateError("Payout batch not found.", code="not_found", status_code=404)
    return batch


def create(db: Session, period: str, actor: str, today: date) -> PayoutBatch:
    if periods.parse_period(period) is None:
        raise AssociateError("period must be a month, YYYY-MM.", code="invalid_period", status_code=422,
                             field="period")
    if periods.first_day(periods.next_period(period)) > today:
        raise AssociateError("A month is paid out only after it has ended.", code="period_not_ended",
                             status_code=409, field="period")
    live = list(db.execute(select(PayoutBatch).where(PayoutBatch.status.in_(LIVE)).with_for_update()).scalars())
    draft = next((b for b in live if b.status == PayoutBatchStatus.draft), None)
    if draft is not None:
        raise AssociateError("Another payout batch is still a draft: approve or cancel it first.",
                             code="draft_batch_exists", status_code=409, extra={"batch_id": draft.id})
    if any(b.period == period for b in live):
        raise AssociateError("This month already has a payout batch.", code="batch_exists", status_code=409,
                             field="period")
    latest = max((b.period for b in live), default=None)
    if latest is not None and period < latest:
        raise AssociateError("Months are paid out in order; a later month already has a batch.",
                             code="period_out_of_order", status_code=409, field="period",
                             extra={"latest_period": latest})
    cutoff = periods.period_end_utc(period)
    waiting = db.execute(select(func.count()).select_from(CommissionEntry).where(
        CommissionEntry.kind == CommissionEntryKind.earn, CommissionEntry.pending_reason == "placeholder_rates",
        CommissionEntry.status != CommissionEntryStatus.reversed, CommissionEntry.batch_id.is_(None),
        CommissionEntry.created_at < cutoff)).scalar_one()
    if waiting:
        raise AssociateError("Commission rates are not confirmed yet: entries of this month have no amount.",
                             code="placeholder_rates", status_code=409, extra={"pending_entries": waiting})
    suspended = select(Associate.id).where(Associate.status == AssociateStatus.suspended)
    entries = list(db.execute(select(CommissionEntry).where(
        CommissionEntry.status == CommissionEntryStatus.approved, CommissionEntry.batch_id.is_(None),
        CommissionEntry.amount.is_not(None), CommissionEntry.created_at < cutoff,
        CommissionEntry.associate_id.not_in(suspended))
        .order_by(CommissionEntry.id).with_for_update()).scalars())
    frozen = db.execute(select(CommissionEntry.id).where(
        CommissionEntry.status == CommissionEntryStatus.approved, CommissionEntry.batch_id.is_(None),
        CommissionEntry.amount.is_not(None), CommissionEntry.created_at < cutoff,
        CommissionEntry.associate_id.in_(suspended)).order_by(CommissionEntry.id)).scalars().all()
    if not entries:
        raise AssociateError("There is no approved commission to pay for this month.", code="no_approved_entries",
                             status_code=409, extra={"frozen_entries": len(frozen)} if frozen else None)
    fy = periods.fy_of_period(period)
    batch = PayoutBatch(period=period, financial_year=fy, status=PayoutBatchStatus.draft, prepared_by=actor,
                        prepared_at=_now())
    try:
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:   # uq_payout_batches_period: prepared at the same moment elsewhere
        raise AssociateError("This month already has a payout batch.", code="batch_exists", status_code=409,
                             field="period")
    by_associate: dict[int, list[CommissionEntry]] = defaultdict(list)
    for e in entries:
        by_associate[e.associate_id].append(e)
    totals, carry = fy_totals(db, by_associate, fy), carried(db, by_associate)
    lines = []
    for associate_id in sorted(by_associate):
        mine = by_associate[associate_id]
        associate = db.get(Associate, associate_id)
        application = application_of(db, associate)
        gross = sum((e.amount for e in mine if e.amount > 0), ZERO)
        recoveries = -sum((e.amount for e in mine if e.amount < 0), ZERO)
        balance = gross - recoveries - carry[associate_id]
        taxable, forward = max(balance, ZERO), max(-balance, ZERO)
        fy_taxable, fy_tds = totals[associate_id]
        pan_on_file = bool(application.pan_encrypted)
        tds, rate = tds_due(fy_taxable_before=fy_taxable, fy_tds_before=fy_tds, taxable_now=taxable,
                            pan_on_file=pan_on_file)
        line = PayoutLine(batch_id=batch.id, associate_id=associate_id, gross=gross, recoveries=recoveries,
                          carried_in=carry[associate_id], taxable=taxable, tds=tds, net=taxable - tds,
                          carry_forward=forward, tds_rate_percent=rate, pan_on_file=pan_on_file,
                          fy_taxable_before=fy_taxable, fy_tds_before=fy_tds,
                          account_holder_name=application.full_name,
                          bank_account_last4=application.bank_account_last4, bank_ifsc=application.bank_ifsc)
        db.add(line)
        lines.append(line)
        for e in mine:
            e.batch_id = batch.id
    db.flush()
    _audit(db, batch.id, "prepared", actor,
           after={"period": period, "financial_year": fy, "entries": [e.id for e in entries],
                  "frozen_entries": list(frozen),
                  "lines": [{"associate_id": ln.associate_id, "gross": _s(ln.gross), "recoveries": _s(ln.recoveries),
                             "carried_in": _s(ln.carried_in), "tds": _s(ln.tds), "net": _s(ln.net)}
                            for ln in lines]})
    return batch


def approve(db: Session, batch_id: int, actor: str, *, is_admin: bool, reason: Optional[str]) -> PayoutBatch:
    """Segregation of duties: the person who prepared a batch does not approve it;
    only an Admin may, with a reason (audited as segregation_override)."""
    batch = _locked(db, batch_id)
    if batch.status != PayoutBatchStatus.draft:
        raise AssociateError(f"A {batch.status.value} batch cannot be approved.", code="invalid_transition",
                             status_code=409)
    override = None
    if batch.prepared_by == actor:
        if not is_admin:
            raise AssociateError("A payout batch must be approved by someone other than the person who prepared it.",
                                 code="self_approval", status_code=409)
        override = (reason or "").strip()
        if not override:
            raise AssociateError("Say why you are approving a batch you prepared yourself.",
                                 code="reason_required", status_code=422, field="reason")
    batch.status, batch.approved_by, batch.approved_at = PayoutBatchStatus.approved, actor, _now()
    batch.approval_override_reason = override
    db.flush()
    _audit(db, batch.id, "approved", actor, before={"status": "draft"},
           after={"status": "approved", "segregation_override": override})
    if override:
        _audit(db, batch.id, "segregation_override", actor, after={"prepared_by": batch.prepared_by,
                                                                    "reason": override})
    return batch


def mark_paid(db: Session, batch_id: int, actor: str, references: dict[str, str]) -> PayoutBatch:
    """Record the bank transfer of every line (all at once): a bank reference for
    each line with money to pay; lines that net to nothing need none."""
    batch = _locked(db, batch_id)
    if batch.status != PayoutBatchStatus.approved:
        raise AssociateError(f"A {batch.status.value} batch cannot be marked paid.", code="invalid_transition",
                             status_code=409)
    lines = list(db.execute(select(PayoutLine).where(PayoutLine.batch_id == batch.id)).scalars())
    gna = {a.id: a.associate_id for a in db.execute(select(Associate).where(
        Associate.id.in_([ln.associate_id for ln in lines]))).scalars()}
    known = set(gna.values())
    unknown = sorted(set(references) - known)
    if unknown:
        raise AssociateError("Some associates are not in this batch.", code="unknown_associate", status_code=422,
                             field="lines", extra={"associate_ids": unknown})
    cleaned = {k: (v or "").strip() for k, v in references.items()}
    missing = sorted(gna[ln.associate_id] for ln in lines if ln.net > 0 and not cleaned.get(gna[ln.associate_id]))
    if missing:
        raise AssociateError("Give the bank reference of every transfer.", code="missing_bank_reference",
                             status_code=422, field="lines", extra={"associate_ids": missing})
    if any(len(v) > 100 for v in cleaned.values()):
        raise AssociateError("A bank reference is at most 100 characters.", code="invalid_bank_reference",
                             status_code=422, field="lines")
    now = _now()
    for ln in lines:
        ln.bank_reference = cleaned.get(gna[ln.associate_id]) or None
        ln.paid_at = now
    for e in db.execute(select(CommissionEntry).where(CommissionEntry.batch_id == batch.id)).scalars():
        e.status, e.paid_at = CommissionEntryStatus.paid, now
    batch.status, batch.paid_by, batch.paid_at = PayoutBatchStatus.paid, actor, now
    db.flush()
    _audit(db, batch.id, "paid", actor, before={"status": "approved"},
           after={"status": "paid", "references": {gna[ln.associate_id]: ln.bank_reference for ln in lines}})
    return batch


def cancel(db: Session, batch_id: int, actor: str, reason: str) -> PayoutBatch:
    """Discard a draft: its entries return to the pool (the lines stay as history)."""
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the batch is being cancelled.", code="reason_required", status_code=422,
                             field="reason")
    batch = _locked(db, batch_id)
    if batch.status != PayoutBatchStatus.draft:
        raise AssociateError(f"A {batch.status.value} batch cannot be cancelled.", code="invalid_transition",
                             status_code=409)
    released = []
    for e in db.execute(select(CommissionEntry).where(CommissionEntry.batch_id == batch.id)).scalars():
        e.batch_id = None
        released.append(e.id)
    batch.status, batch.cancelled_by, batch.cancelled_at, batch.cancel_reason = (
        PayoutBatchStatus.cancelled, actor, _now(), reason)
    db.flush()
    _audit(db, batch.id, "cancelled", actor, before={"status": "draft"},
           after={"status": "cancelled", "reason": reason, "entries_released": released})
    return batch


# ── JSON ─────────────────────────────────────────────────────────────────────────

def line_json(line: PayoutLine, associate: Optional[Associate], *, staff: bool) -> dict:
    row = {"line_id": line.id, "gross": _s(line.gross), "recoveries": _s(line.recoveries),
           "carried_in": _s(line.carried_in), "taxable": _s(line.taxable), "tds": _s(line.tds),
           "tds_rate_percent": str(Decimal(line.tds_rate_percent).quantize(Decimal("0.0001"))),
           "net": _s(line.net), "carry_forward": _s(line.carry_forward),
           "bank_account_last4": line.bank_account_last4, "bank_ifsc": line.bank_ifsc,
           "bank_reference": line.bank_reference, "paid_at": line.paid_at.isoformat() if line.paid_at else None}
    if staff:
        row.update(associate={"associate_id": associate.associate_id, "full_name": associate.full_name}
                   if associate else None, account_holder_name=line.account_holder_name,
                   pan_on_file=line.pan_on_file, fy_taxable_before=_s(line.fy_taxable_before),
                   fy_tds_before=_s(line.fy_tds_before))
    return row


def batch_json(db: Session, batch: PayoutBatch, *, with_lines: bool) -> dict:
    lines = list(db.execute(select(PayoutLine).where(PayoutLine.batch_id == batch.id)
                            .order_by(PayoutLine.associate_id)).scalars())
    body = {"batch_id": batch.id, "period": batch.period, "financial_year": batch.financial_year,
            "status": batch.status.value, "prepared_by": batch.prepared_by,
            "prepared_at": batch.prepared_at.isoformat(), "approved_by": batch.approved_by,
            "approved_at": batch.approved_at.isoformat() if batch.approved_at else None,
            "approval_override_reason": batch.approval_override_reason, "paid_by": batch.paid_by,
            "paid_at": batch.paid_at.isoformat() if batch.paid_at else None,
            "cancelled_by": batch.cancelled_by,
            "cancelled_at": batch.cancelled_at.isoformat() if batch.cancelled_at else None,
            "cancel_reason": batch.cancel_reason,
            "totals": {k: _s(sum((getattr(ln, k) for ln in lines), ZERO))
                       for k in ("gross", "recoveries", "carried_in", "taxable", "tds", "net", "carry_forward")},
            "line_count": len(lines)}
    if with_lines:
        associates = {a.id: a for a in db.execute(select(Associate).where(
            Associate.id.in_([ln.associate_id for ln in lines]))).scalars()} if lines else {}
        body["lines"] = [line_json(ln, associates.get(ln.associate_id), staff=True) for ln in lines]
    return body
