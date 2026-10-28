"""The commission ledger — Commission & Payout Policy §4.2–4.4 and 4.7.

Earning (4.2, 4.3). When Accounts verifies a customer payment (app/payments/service.py
verify, same transaction, the payment row locked), every associate who earns on it gets
one `earn` entry for the verified amount:

  * personal_sale — the associate the customer is attributed to (the onboarding's
    owner), at the rate for their type and capability/level;
  * territory_state / territory_district / territory_block — whoever holds that seat
    for the site's state / district / block (TerritorySeat history) at the moment the
    agreement was signed, or the payment verified (config territory_seat_holder_at).
    Several holders share the rate or each earn it (multiple_territory_holders).

The basis is the verified amount without GST (basis_pro_rata: × the accepted proposal's
basic ÷ final price) or the verified amount as received (gross). The rate is the one in
force on the date the customer's agreement was signed (4.1): rates.resolve — the
Associate's individual rate on that date when an Admin set one (BE-12; the entry keeps
individual_rate_id), else the confirmed schedule in force then (schedule_id).

An entry whose rate cannot be known yet is recorded with its basis and no amount:
pending_reason `agreement_not_signed` (rated automatically when the agreement is
signed), `placeholder_rates` (no confirmed schedule: rated only by an Admin's audited
re-rate once a real schedule is published) or `basis_unavailable` (the accepted
proposal has no basic price — a custom quote). No payout includes an unrated entry.

Reversal (4.7). When a verified payment is dishonoured (same transaction as the ledger
reversal and the receipt being voided), every earn entry on it is reversed:
  * not yet in a payout batch → a `reversal` entry; both are marked `reversed` and never
    paid (a draft batch holding it is cancelled first, so it is prepared again);
  * in an approved or paid batch → a `recovery` entry (approved at once), netted against
    the associate's next payouts; an unrecovered balance carries forward.
An Admin may reverse an entry by hand for false information or a breach of the Network
Terms (4.7), with a reason — the same rule.

Status (4.4): earned → approved (checked by Accounts) → paid (in a paid batch).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.commissions import config, rates
from app.memory.models import (
    Associate, AssociateStatus, AuditLog, CommissionEntry, CommissionEntryKind, CommissionEntryStatus,
    CommissionRateSchedule, CommissionRole, CustomerOnboarding, OnboardingStatus, Payment, PaymentStatus,
    PayoutBatch, PayoutBatchStatus, ProjectStage, TerritoryLevel, TerritorySeat,
)

CENT = Decimal("0.01")
PENDING_REASONS = ("agreement_not_signed", "placeholder_rates", "basis_unavailable")
_TERRITORY = (
    (CommissionRole.territory_state, TerritoryLevel.state_head, "state_id"),
    (CommissionRole.territory_district, TerritoryLevel.district_head, "district_id"),
    (CommissionRole.territory_block, TerritoryLevel.block, "block_id"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ist_date(when: datetime) -> date:
    from app.commissions.periods import ist_date as _ist_date
    return _ist_date(when)


def _audit(db: Session, entity_id, action: str, actor: str, after=None, before=None,
           entity_type: str = "commission_entry") -> None:
    db.add(AuditLog(entity_type=entity_type, entity_id=str(entity_id), action=action, performed_by=actor,
                    before_value=before, after_value=after))


# ── What an entry is computed from ───────────────────────────────────────────────

def agreement_event(db: Session, onboarding: CustomerOnboarding):
    from app.onboarding import progress
    return progress.recorded(db, onboarding.project_id).get(ProjectStage.agreement_signed)


def agreement_date(db: Session, onboarding: CustomerOnboarding) -> Optional[date]:
    """The date the customer's agreement was signed (IST), per agreement_date_source:
    the day the signed agreement was recorded, or the stamp paper's date (falling back
    to the recorded day when the stage was recorded without an agreement on file)."""
    event = agreement_event(db, onboarding)
    if event is None:
        return None
    if config.commissions()["agreement_date_source"] == "stamp_paper_date":
        from app.onboarding import agreement
        record = agreement.get(db, onboarding)
        if record is not None and record.stamp_paper_date is not None:
            return record.stamp_paper_date
    return ist_date(event.recorded_at)


def basis_for(db: Session, onboarding: CustomerOnboarding, amount: Decimal) -> Optional[Decimal]:
    """What the rate applies to (config commissions.basis); None when it cannot be known."""
    if config.commissions()["basis"] == "gross":
        return amount
    from app.payments import service as payments
    proposal = payments.accepted_proposal(db, onboarding)
    return basis_of(amount, accepted_price(proposal) if proposal is not None else {})


def accepted_price(proposal) -> dict:
    """{"basic", "final"} of the price the customer accepted. A proposal with a document
    (BE-13, and Studio conversions) carries its price there — adjust-pricing writes into the
    document and the PDF is made from it — so its figures win; an older proposal has only
    the content engine's `pricing`. BE-19: before this, every formal proposal priced
    through the document had no basis (pending_reason basis_unavailable) and never paid."""
    from app.proposals import document as d
    content = proposal.content or {}
    try:
        doc = d.stored(content)
    except Exception:   # an unreadable document falls back to the legacy pricing
        doc = None
    if doc is not None:
        figs = d.figures(doc)
        if figs.get("basic") and figs.get("total"):
            return {"basic": figs["basic"], "final": figs["total"]}
    return content.get("pricing") or {}


def basis_of(amount: Decimal, pricing: dict) -> Optional[Decimal]:
    """`amount` on the commission basis (config commissions.basis), given the price it
    is part of ({"basic", "final"}): × basic ÷ final under basic_pro_rata, as received
    under gross. None when the price has no usable basic/final. Also used, unchanged,
    for the associate's estimated commission (app/commissions/estimate.py)."""
    if config.commissions()["basis"] == "gross":
        return amount
    try:
        basic, final = Decimal(str(pricing["basic"])), Decimal(str(pricing["final"]))
    except (KeyError, TypeError, ArithmeticError):
        return None
    if not (basic.is_finite() and final.is_finite()) or basic <= 0 or final <= 0 or basic > final:
        return None
    return (amount * basic / final).quantize(CENT, rounding=ROUND_HALF_UP)


def earns(associate: Associate) -> bool:
    return config.commissions()["deactivated_associates_earn"] or associate.status != AssociateStatus.deactivated


def _seat_holders(db: Session, level: TerritoryLevel, column: str, value: int, at: datetime) -> list[Associate]:
    q = (select(Associate).join(TerritorySeat, TerritorySeat.holder_id == Associate.id)
         .where(TerritorySeat.level == level, getattr(TerritorySeat, column) == value,
                TerritorySeat.claimed_at <= at,
                (TerritorySeat.released_at.is_(None)) | (TerritorySeat.released_at > at))
         .order_by(Associate.id))
    return list(db.execute(q).scalars().unique())


def recipients(db: Session, onboarding: CustomerOnboarding, *, personal: bool,
               territory_at: Optional[datetime]) -> list[tuple[Associate, CommissionRole, int]]:
    """(associate, role, holders) for everyone who earns on a payment of this customer.
    Territory roles only when `territory_at` (the seat-holding instant) is known."""
    cfg = config.commissions()
    owner = db.get(Associate, onboarding.owner_associate_id)
    out = []
    if personal and earns(owner):
        out.append((owner, CommissionRole.personal_sale, 1))
    if territory_at is None:
        return out
    for role, level, column in _TERRITORY:
        value = getattr(onboarding, column)
        if value is None:
            continue
        holders = [a for a in _seat_holders(db, level, column, value, territory_at)
                   if earns(a) and (cfg["territory_on_own_customers"] or a.id != owner.id)]
        share = len(holders) if cfg["multiple_territory_holders"] == "split_equally" else 1
        out += [(a, role, max(share, 1)) for a in holders]
    return out


def _territory_instant(db: Session, onboarding: CustomerOnboarding, payment: Payment) -> Optional[datetime]:
    if config.commissions()["territory_seat_holder_at"] == "payment_verified":
        return payment.verified_at
    event = agreement_event(db, onboarding)
    return event.recorded_at if event is not None else None


# ── Earning ──────────────────────────────────────────────────────────────────────

def _new_earn(payment: Payment, onboarding: CustomerOnboarding, associate: Associate, role: CommissionRole,
              holders: int, basis: Optional[Decimal], agreed: Optional[date], actor: str) -> CommissionEntry:
    return CommissionEntry(associate_id=associate.id, earning_role=role, kind=CommissionEntryKind.earn,
                           status=CommissionEntryStatus.earned, onboarding_id=onboarding.id,
                           project_id=payment.project_id, payment_id=payment.id, basis_amount=basis,
                           holders=holders, agreement_date=agreed, pending_reason="agreement_not_signed",
                           created_by=actor, created_at=_now())


def _insert(db: Session, entries: list[CommissionEntry]) -> list[CommissionEntry]:
    if not entries:
        return []
    try:
        with db.begin_nested():
            db.add_all(entries)
            db.flush()
    except IntegrityError:
        # uq_commission_entries_earn_once: these were written already (cannot happen
        # under the payment row lock; kept as the database's backstop).
        raise AssociateError("Commission for this payment is already recorded.", code="already_recorded",
                             status_code=409)
    return entries


def on_payment_verified(db: Session, payment: Payment, actor: str) -> list[CommissionEntry]:
    """Hook in payments.verify — same transaction, payment row locked."""
    from app.payments import service as payments

    onboarding = payments.onboarding_of(db, payment)
    if onboarding is None:
        return []
    if onboarding.status == OnboardingStatus.closed:
        _audit(db, payment.id, "commission_not_earned", actor, entity_type="payment",
               after={"reason": "onboarding_closed", "onboarding_id": onboarding.id})
        return []
    basis = basis_for(db, onboarding, payment.amount)
    agreed = agreement_date(db, onboarding)
    entries = [_new_earn(payment, onboarding, a, role, holders, basis, agreed, actor)
               for a, role, holders in recipients(db, onboarding, personal=True,
                                                  territory_at=_territory_instant(db, onboarding, payment))]
    _insert(db, entries)
    rate(db, entries, actor)
    _audit(db, payment.id, "commission_earned", actor, entity_type="payment",
           after={"entries": [{"entry_id": e.id, "associate_id": e.associate_id, "role": e.earning_role.value,
                               "amount": _s(e.amount), "pending_reason": e.pending_reason} for e in entries]})
    return entries


def on_agreement_signed(db: Session, onboarding: CustomerOnboarding, actor: str) -> list[CommissionEntry]:
    """Hook in progress.record(agreement_signed) — same transaction. Gives the rate
    date to the project's entries that waited for it, creates the territory entries
    that waited for the seat-holding date, and rates what can now be rated."""
    agreed = agreement_date(db, onboarding)
    waiting = list(db.execute(select(CommissionEntry).where(CommissionEntry.project_id == onboarding.project_id,
                                                           CommissionEntry.kind == CommissionEntryKind.earn,
                                                           CommissionEntry.agreement_date.is_(None))).scalars())
    for entry in waiting:
        entry.agreement_date = agreed
    created = []
    if config.commissions()["territory_seat_holder_at"] == "agreement_signed":
        at = agreement_event(db, onboarding).recorded_at
        verified = list(db.execute(select(Payment).where(Payment.project_id == onboarding.project_id,
                                                        Payment.status == PaymentStatus.verified)
                                   .order_by(Payment.id)).scalars())
        territory = recipients(db, onboarding, personal=False, territory_at=at) if verified else []
        for payment in verified:
            have = {(e.associate_id, e.earning_role) for e in db.execute(select(CommissionEntry).where(
                CommissionEntry.payment_id == payment.id, CommissionEntry.kind == CommissionEntryKind.earn)).scalars()}
            basis = basis_for(db, onboarding, payment.amount)
            created += [_new_earn(payment, onboarding, a, role, holders, basis, agreed, actor)
                        for a, role, holders in territory if (a.id, role) not in have]
        _insert(db, created)
    pending = [e for e in waiting if e.amount is None] + created
    rate(db, pending, actor)
    if waiting or created:
        _audit(db, onboarding.project_id, "commission_agreement_dated", actor, entity_type="project",
               after={"agreement_date": agreed.isoformat() if agreed else None,
                      "entries_dated": [e.id for e in waiting], "entries_created": [e.id for e in created]})
    return created


# ── Rating ───────────────────────────────────────────────────────────────────────

def equal_parts(total: Decimal, n: int) -> list[Decimal]:
    """`total` in n parts to the paisa, the first ones taking any remaining paise."""
    base = (total / n).quantize(CENT, rounding=ROUND_DOWN)
    extra = int(((total - base * n) / CENT).to_integral_value())
    return [base + (CENT if i < extra else 0) for i in range(n)]


def schedule_on(db: Session, day: date,
                fallback: Optional[CommissionRateSchedule] = None) -> Optional[CommissionRateSchedule]:
    """The confirmed schedule a commission dated `day` is rated with (rates.confirmed_on)."""
    return rates.confirmed_on(db, day, fallback)


def commission_of(basis: Decimal, percent: Decimal) -> Decimal:
    """basis × percent, to the paisa — one holder's amount."""
    return (basis * percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def rate(db: Session, entries: Iterable[CommissionEntry], actor: str,
         fallback: Optional[CommissionRateSchedule] = None) -> list[CommissionEntry]:
    """Fill in the amount of every unrated earn entry given that can be rated now
    (and of the reversals mirroring them); set the pending reason of the rest.
    `fallback` is the real schedule an Admin's re-rate applies where the schedule in
    force on the agreement date is a placeholder (or there is none). Each rate is
    rates.resolve's for the entry's associate, role and agreement date."""
    rated = []
    cache: dict = {}
    groups: dict[tuple, list[CommissionEntry]] = {}
    for entry in entries:
        if entry.kind != CommissionEntryKind.earn or entry.amount is not None:
            continue
        if entry.basis_amount is None:
            entry.pending_reason = "basis_unavailable"
            continue
        if entry.agreement_date is None:
            entry.pending_reason = "agreement_not_signed"
            continue
        if _resolve(db, entry, fallback, cache) is None:
            entry.pending_reason = "placeholder_rates"
            continue
        groups.setdefault((entry.payment_id, entry.earning_role), []).append(entry)
    for payment_id, role in groups:
        # Every holder sharing this role on this payment, rated together so the shares
        # add up to the rate exactly.
        siblings = list(db.execute(select(CommissionEntry).where(
            CommissionEntry.payment_id == payment_id, CommissionEntry.earning_role == role,
            CommissionEntry.kind == CommissionEntryKind.earn, CommissionEntry.amount.is_(None))
            .order_by(CommissionEntry.associate_id)).scalars())
        by_holders: dict[int, list[CommissionEntry]] = defaultdict(list)
        for e in siblings:
            by_holders[e.holders].append(e)
        for holders, members in by_holders.items():
            found = [(e, _resolve(db, e, fallback, cache)) for e in members]
            found = [(e, r) for e, r in found if r is not None]
            if not found:
                continue
            members, resolved = [e for e, _ in found], [r for _, r in found]
            if holders > 1:
                # A shared territory rate (one rate key per territory role, never an
                # individual rate): the holders' shares add up to basis × rate exactly.
                total = commission_of(members[0].basis_amount, resolved[0].percent)
                amounts = equal_parts(total, holders)[:len(members)]
            else:
                amounts = [commission_of(e.basis_amount, r.percent) for e, r in zip(members, resolved)]
            for e, r, amount in zip(members, resolved, amounts):
                e.rate_percent, e.amount, e.pending_reason = r.percent, amount, None
                e.schedule_id = r.schedule.id if r.schedule is not None else None
                e.individual_rate_id = r.individual.id if r.individual is not None else None
                e.rated_by, e.rated_at = actor, _now()
                rated.append(e)
    db.flush()
    for e in rated:
        _mirror_reversal(db, e)
    return rated


def _resolve(db: Session, entry: CommissionEntry, fallback: Optional[CommissionRateSchedule],
             cache: dict) -> Optional[rates.Resolved]:
    if entry.agreement_date is None:
        return None
    return rates.resolve(db, db.get(Associate, entry.associate_id), entry.earning_role, entry.agreement_date,
                         fallback=fallback, cache=cache)


def _mirror_reversal(db: Session, earn: CommissionEntry) -> None:
    """A reversal written while its earn entry was unrated takes the amount now."""
    counter = db.execute(select(CommissionEntry).where(CommissionEntry.reverses_entry_id == earn.id)).scalar_one_or_none()
    if counter is not None and counter.amount is None:
        counter.amount, counter.pending_reason = -earn.amount, None
        counter.rated_by, counter.rated_at = earn.rated_by, earn.rated_at


def re_rate(db: Session, schedule_id: int, actor: str, reason: str) -> dict:
    """Admin, audited: rate every entry pending under placeholder rates, using the
    schedule in force on its agreement date where that schedule is real, else
    `schedule_id` (a published, non-placeholder schedule) — unless the Associate has
    an individual rate on that date, which wins (rates.resolve)."""
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the pending entries are being rated.", code="reason_required",
                             status_code=422, field="reason")
    schedule = db.get(CommissionRateSchedule, schedule_id)
    if schedule is None:
        raise AssociateError("Rate schedule not found.", code="not_found", status_code=404)
    if schedule.is_placeholder:
        raise AssociateError("A placeholder schedule cannot be used to rate entries.",
                             code="schedule_is_placeholder", status_code=409)
    pending = list(db.execute(select(CommissionEntry).where(
        CommissionEntry.kind == CommissionEntryKind.earn, CommissionEntry.pending_reason == "placeholder_rates")
        .order_by(CommissionEntry.id).with_for_update()).scalars())
    done = rate(db, pending, actor, fallback=schedule)
    total = sum((e.amount for e in done), Decimal(0))
    _audit(db, schedule.id, "re_rated", actor, entity_type="commission_rate_schedule",
           after={"reason": reason, "entries": [e.id for e in done], "total": _s(total),
                  "still_pending": len(pending) - len(done)})
    return {"schedule_id": schedule.id, "rated": len(done), "total_amount": _s(total),
            "still_pending": len(pending) - len(done)}


# ── Reversal ─────────────────────────────────────────────────────────────────────

def _counter(db: Session, entry: CommissionEntry) -> Optional[CommissionEntry]:
    return db.execute(select(CommissionEntry).where(CommissionEntry.reverses_entry_id == entry.id)).scalar_one_or_none()


def _reverse(db: Session, earn: CommissionEntry, actor: str, reason: str) -> CommissionEntry:
    if earn.batch_id is not None:
        batch = db.execute(select(PayoutBatch).where(PayoutBatch.id == earn.batch_id).with_for_update()).scalar_one()
        if batch.status == PayoutBatchStatus.draft:
            from app.commissions import payouts
            payouts.cancel(db, batch.id, actor, f"Cancelled automatically: commission entry {earn.id} was reversed "
                                                f"({reason})")
            db.refresh(earn)
    now = _now()
    common = dict(associate_id=earn.associate_id, earning_role=earn.earning_role, onboarding_id=earn.onboarding_id,
                  project_id=earn.project_id, payment_id=earn.payment_id, holders=earn.holders,
                  reverses_entry_id=earn.id, reason=reason, created_by=actor, created_at=now)
    if earn.batch_id is None:
        counter = CommissionEntry(kind=CommissionEntryKind.reversal, status=CommissionEntryStatus.reversed,
                                  amount=-earn.amount if earn.amount is not None else None,
                                  pending_reason=None if earn.amount is not None else earn.pending_reason,
                                  **common)
        earn.status = CommissionEntryStatus.reversed
    else:
        # Already committed to a payout: recovered from the associate's next payouts.
        counter = CommissionEntry(kind=CommissionEntryKind.recovery, status=CommissionEntryStatus.approved,
                                  amount=-earn.amount, approved_by=actor, approved_at=now, **common)
    db.add(counter)
    db.flush()
    _audit(db, earn.id, "reversed", actor,
           after={"counter_entry_id": counter.id, "kind": counter.kind.value, "amount": _s(counter.amount),
                  "reason": reason})
    return counter


def on_payment_reversed(db: Session, payment: Payment, actor: str, reason: str) -> list[CommissionEntry]:
    """Hook in payments.dishonour for a verified payment — same transaction as the
    ledger reversal and the receipt being voided; the payment row is locked."""
    earns = list(db.execute(select(CommissionEntry).where(
        CommissionEntry.payment_id == payment.id, CommissionEntry.kind == CommissionEntryKind.earn)
        .order_by(CommissionEntry.id).with_for_update()).scalars())
    return [_reverse(db, e, actor, reason) for e in earns if _counter(db, e) is None]


def reverse_entry(db: Session, entry_id: int, actor: str, reason: str) -> CommissionEntry:
    """Admin, 4.7: commission obtained through false information or a breach."""
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why this commission is being reversed.", code="reason_required",
                             status_code=422, field="reason")
    entry = db.execute(select(CommissionEntry).where(CommissionEntry.id == entry_id).with_for_update()
                       ).scalar_one_or_none()
    if entry is None:
        raise AssociateError("Commission entry not found.", code="not_found", status_code=404)
    if entry.kind != CommissionEntryKind.earn or _counter(db, entry) is not None:
        raise AssociateError("Only an earn entry that has not been reversed can be reversed.",
                             code="not_reversible", status_code=409)
    return _reverse(db, entry, actor, reason)


# ── Approval ─────────────────────────────────────────────────────────────────────

MAX_BULK = 500


def approve(db: Session, entry_ids: list[int], actor: str) -> list[CommissionEntry]:
    """Accounts/Admin: earned → approved, all or nothing. Only a rated earn entry that
    has not been reversed can be approved."""
    ids = sorted(set(entry_ids))
    if not ids or len(ids) > MAX_BULK:
        raise AssociateError(f"Give between 1 and {MAX_BULK} entry ids.", code="invalid_entry_ids",
                             status_code=422, field="entry_ids")
    rows = {e.id: e for e in db.execute(select(CommissionEntry).where(CommissionEntry.id.in_(ids))
                                        .with_for_update()).scalars()}
    refused = []
    for i in ids:
        e = rows.get(i)
        why = ("not_found" if e is None else
               "not_an_earn_entry" if e.kind != CommissionEntryKind.earn else
               "not_earned" if e.status != CommissionEntryStatus.earned else
               "not_rated" if e.amount is None else None)
        if why:
            refused.append({"entry_id": i, "reason": why})
    if refused:
        raise AssociateError("Some entries cannot be approved; nothing was approved.",
                             code="entries_not_approvable", status_code=409, extra={"entries": refused})
    now = _now()
    for i in ids:
        rows[i].status, rows[i].approved_by, rows[i].approved_at = CommissionEntryStatus.approved, actor, now
    db.flush()
    _audit(db, ",".join(map(str, ids))[:200], "approved", actor, after={"entry_ids": ids})
    return [rows[i] for i in ids]


def _s(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(Decimal(value).quantize(CENT))
