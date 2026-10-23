"""Commission rate schedules (Commission & Payout Policy 4.1).

A schedule is a complete set of rates — one percentage per RATE_KEYS key — published
by an Admin with an effective date. It is never edited (a database trigger refuses
it): a change is a new version. Policy 4.1: a change needs commissions.rate_change_notice_days
days' notice (config/commissions.yaml), and "the rate that applies is the one in force
on the date the customer's agreement is signed" — in_force(agreement_date).

A schedule marked is_placeholder carries rates nobody has confirmed: entries under it
are recorded without an amount (pending_reason placeholder_rates) and no payout
includes them. With no schedule at all the same happens.

The first real schedule (BE-12): while no non-placeholder schedule has ever been
published, nobody has relied on a confirmed rate, so the first real one may take effect
today. Every later real schedule needs the notice period.

Individual rates (BE-12). An Admin may set one Associate's personal-sale rate above or
below the standard schedule (AssociateCommissionRate rows, append-only; NULL = back to
the standard schedule). resolve() is the one rate lookup for an Associate, a role and a
date — the ledger (service.rate), the estimate and every rate display call it:

  * personal_sale: the Associate's latest individual row with effective_from on or
    before the date wins (the later row when two share a date); a NULL row means the
    standard schedule;
  * otherwise (and for territory roles, which have no individual rates) the rate for
    rate_key in the confirmed schedule in force on that date (service.schedule_on);
  * neither → None (the ledger's placeholder_rates).

The date is the one the ledger uses: the customer's agreement date (today until it is
signed, for the estimate). An entry once rated never changes (the ledger's triggers).
A reduction needs commissions.rate_change_notice_days' notice; an increase may take
effect today (commissions.individual_rate_increase_needs_notice, C5-IND-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.commissions import config
from app.commissions.serialize import pct
from app.memory.models import (
    Associate, AssociateCommissionRate, AssociateType, AuditLog, CommissionRate, CommissionRateSchedule,
    CommissionRole,
)

# Every key a schedule must set. personal_sale is per associate type and capability
# (Associates) or level (Preferred Associates); territory roles are one rate each.
RATE_KEYS = (
    "personal_sale.associate.sales",
    "personal_sale.associate.execution",
    "personal_sale.preferred_associate.state_head",
    "personal_sale.preferred_associate.district_head",
    "personal_sale.preferred_associate.block",
    CommissionRole.territory_state.value,
    CommissionRole.territory_district.value,
    CommissionRole.territory_block.value,
)
RATE_QUANTUM = Decimal("0.0001")


def rate_key(role: CommissionRole, associate: Associate) -> str:
    if role != CommissionRole.personal_sale:
        return role.value
    if associate.associate_type == AssociateType.associate:
        return f"personal_sale.associate.{associate.capability.value}"
    return f"personal_sale.preferred_associate.{associate.level.value}"


def in_force(db: Session, on: date) -> Optional[CommissionRateSchedule]:
    """The schedule in force on a date: the latest effective_from on or before it
    (the higher version when two share a date)."""
    return db.execute(select(CommissionRateSchedule).where(CommissionRateSchedule.effective_from <= on)
                      .order_by(CommissionRateSchedule.effective_from.desc(), CommissionRateSchedule.version.desc())
                      .limit(1)).scalar_one_or_none()


def rates_of(db: Session, schedule_id: int) -> dict[str, Decimal]:
    return {r.rate_key: r.rate_percent for r in db.execute(
        select(CommissionRate).where(CommissionRate.schedule_id == schedule_id)).scalars()}


def _rate(raw, key: str, field: Optional[str] = None) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        value = Decimal("NaN")
    if not value.is_finite() or value < 0 or value > 100 or value != value.quantize(RATE_QUANTUM):
        raise AssociateError(f"The rate for {key} must be a percentage from 0 to 100 with at most four decimals.",
                             code="invalid_rate", status_code=422, field=field or f"rates.{key}")
    return value.quantize(RATE_QUANTUM)


def _notice_from(today: date) -> date:
    return today + timedelta(days=config.commissions()["rate_change_notice_days"])


def earliest_effective_from(today: date, is_placeholder: bool, first_real: bool = False) -> date:
    """The earliest effective_from a schedule may have: today for a placeholder or the
    first real schedule (first_real), else today + the notice period."""
    return today if is_placeholder or first_real else _notice_from(today)


def no_real_schedule_yet(db: Session) -> bool:
    """True while no non-placeholder schedule has ever been published (in force or not)."""
    return db.execute(select(CommissionRateSchedule.id).where(CommissionRateSchedule.is_placeholder.is_(False))
                      .limit(1)).first() is None


def publish(db: Session, *, effective_from: date, is_placeholder: bool, rates: dict, note: Optional[str],
            actor: str, today: date) -> CommissionRateSchedule:
    """Publish a new schedule version. Every RATE_KEYS key is required, nothing else
    is accepted. A real (non-placeholder) schedule takes effect no sooner than the
    notice period — except the first one ever published, which may take effect today;
    none takes effect in the past."""
    unknown = sorted(set(rates) - set(RATE_KEYS))
    missing = [k for k in RATE_KEYS if k not in rates]
    if unknown or missing:
        raise AssociateError("A schedule must set exactly one rate for every rate key.", code="invalid_rate_keys",
                             status_code=422, field="rates", extra={"missing": missing, "unknown": unknown})
    values = {k: _rate(rates[k], k) for k in RATE_KEYS}
    # One statement, so the "first real schedule" answer and the version come from the
    # same snapshot: two concurrent first publishes get the same version and the second
    # is refused (uq version) — only one of them can skip the notice.
    latest, real = db.execute(select(func.max(CommissionRateSchedule.version),
                                     func.count(case((CommissionRateSchedule.is_placeholder.is_(False), 1))))).one()
    first_real = not is_placeholder and not real
    earliest = earliest_effective_from(today, is_placeholder, first_real)
    if effective_from < earliest:
        notice = earliest > today
        raise AssociateError(
            "A rate change takes effect only after the notice period." if notice
            else "A schedule cannot take effect in the past.",
            code="notice_period" if notice else "effective_in_past", status_code=422,
            field="effective_from", extra={"earliest_effective_from": earliest.isoformat()})
    version = (latest or 0) + 1
    schedule = CommissionRateSchedule(version=version, effective_from=effective_from, is_placeholder=is_placeholder,
                                      note=(note or "").strip() or None, published_by=actor,
                                      published_at=datetime.now(timezone.utc))
    try:
        with db.begin_nested():
            db.add(schedule)
            db.flush()
    except IntegrityError:
        raise AssociateError("Another schedule was published at the same moment; reload and try again.",
                             code="concurrent_publish", status_code=409)
    for key, value in values.items():
        db.add(CommissionRate(schedule_id=schedule.id, rate_key=key, rate_percent=value))
    db.flush()
    db.add(AuditLog(entity_type="commission_rate_schedule", entity_id=str(schedule.id), action="published",
                    performed_by=actor,
                    after_value={"version": version, "effective_from": effective_from.isoformat(),
                                 "is_placeholder": is_placeholder, "first_real_schedule": first_real,
                                 "rates": {k: str(v) for k, v in values.items()}}))
    return schedule


def schedule_json(schedule: CommissionRateSchedule, rates: dict[str, Decimal], today: date) -> dict:
    return {"schedule_id": schedule.id, "version": schedule.version,
            "effective_from": schedule.effective_from.isoformat(), "is_placeholder": schedule.is_placeholder,
            "note": schedule.note, "published_by": schedule.published_by,
            "published_at": schedule.published_at.isoformat(),
            "rates": {k: str(rates[k]) for k in RATE_KEYS if k in rates},
            "in_effect": schedule.effective_from <= today}


# ── One rate lookup for an Associate (BE-12) ─────────────────────────────────────

@dataclass(frozen=True)
class Resolved:
    """The rate that applies, and where it came from: source "individual" (the row) or
    "standard" (the confirmed schedule)."""
    percent: Decimal
    source: str
    schedule: Optional[CommissionRateSchedule] = None
    individual: Optional[AssociateCommissionRate] = None

    @property
    def since(self) -> date:
        return (self.individual or self.schedule).effective_from


def confirmed_on(db: Session, day: date, fallback: Optional[CommissionRateSchedule] = None,
                 cache: Optional[dict] = None) -> Optional[CommissionRateSchedule]:
    """The confirmed schedule a commission dated `day` is rated with: the one in force
    on that day unless it is a placeholder (or there is none), else `fallback`."""
    key = ("schedule", day)
    if cache is not None and key in cache:
        schedule = cache[key]
    else:
        schedule = in_force(db, day)
        if cache is not None:
            cache[key] = schedule
    if schedule is not None and not schedule.is_placeholder:
        return schedule
    return fallback


def _table(db: Session, schedule_id: int, cache: Optional[dict]) -> dict[str, Decimal]:
    if cache is None:
        return rates_of(db, schedule_id)
    if schedule_id not in cache:
        cache[schedule_id] = rates_of(db, schedule_id)
    return cache[schedule_id]


def individual_on(db: Session, associate_id: int, key: str, day: date, *,
                  cache: Optional[dict] = None) -> Optional[AssociateCommissionRate]:
    """The Associate's individual row in force on `day` for `key` (it may carry NULL =
    the standard schedule), or None when they have never had one by then."""
    ck = ("individual", associate_id, key, day)
    if cache is not None and ck in cache:
        return cache[ck]
    row = db.execute(select(AssociateCommissionRate).where(
        AssociateCommissionRate.associate_id == associate_id, AssociateCommissionRate.rate_key == key,
        AssociateCommissionRate.effective_from <= day)
        .order_by(AssociateCommissionRate.effective_from.desc(), AssociateCommissionRate.id.desc())
        .limit(1)).scalar_one_or_none()
    if cache is not None:
        cache[ck] = row
    return row


def standard(db: Session, associate: Associate, role: CommissionRole, day: date, *,
             fallback: Optional[CommissionRateSchedule] = None, cache: Optional[dict] = None) -> Optional[Resolved]:
    """The standard schedule's rate alone (no individual rate)."""
    schedule = confirmed_on(db, day, fallback, cache)
    if schedule is None:
        return None
    percent = _table(db, schedule.id, cache).get(rate_key(role, associate))
    return None if percent is None else Resolved(percent, "standard", schedule=schedule)


def resolve(db: Session, associate: Associate, role: CommissionRole, day: date, *,
            fallback: Optional[CommissionRateSchedule] = None, cache: Optional[dict] = None) -> Optional[Resolved]:
    """THE rate for this Associate, role and date (module docstring). `fallback` is the
    real schedule an Admin's re-rate applies where the standard schedule in force is a
    placeholder; `cache` (any dict) saves queries across a page."""
    if role == CommissionRole.personal_sale:
        row = individual_on(db, associate.id, rate_key(role, associate), day, cache=cache)
        if row is not None and row.rate_percent is not None:
            return Resolved(row.rate_percent, "individual", individual=row)
    return standard(db, associate, role, day, fallback=fallback, cache=cache)


def _percent(r: Optional[Resolved]) -> Optional[Decimal]:
    return None if r is None else r.percent


# ── Setting an individual rate (Admin) ───────────────────────────────────────────

MIN_REASON = 10


def bound(name: str) -> Optional[Decimal]:
    raw = config.commissions().get(name)
    return None if raw is None else Decimal(str(raw))


def set_individual(db: Session, associate: Associate, *, rate_percent, effective_from: date, reason: Optional[str],
                   actor: str, today: date) -> AssociateCommissionRate:
    """Admin: give one Associate an individual personal-sale rate from effective_from
    (rate_percent None = back to the standard schedule). Never in the past. A
    reduction — the new rate lower than the rate in force for this Associate on
    effective_from — needs the notice period; an increase may take effect today unless
    individual_rate_increase_needs_notice. With no confirmed rate in force for them on
    that date there is nothing to give notice of. Audited; the Associate is told (home
    feed, and email when configured) — never the reason."""
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise AssociateError(f"Say why the rate is changing (at least {MIN_REASON} characters).",
                             code="reason_required", status_code=422, field="reason")
    key = rate_key(CommissionRole.personal_sale, associate)
    value = None if rate_percent is None else _rate(rate_percent, key, field="rate_percent")
    low, high = bound("individual_rate_min_percent"), bound("individual_rate_max_percent")
    if value is not None and ((low is not None and value < low) or (high is not None and value > high)):
        raise AssociateError("The rate is outside the range Grid Nova allows for individual rates.",
                             code="out_of_bounds", status_code=422, field="rate_percent",
                             extra={"min_percent": None if low is None else str(low.quantize(RATE_QUANTUM)),
                                    "max_percent": None if high is None else str(high.quantize(RATE_QUANTUM))})
    if effective_from < today:
        raise AssociateError("A rate change cannot take effect in the past.", code="effective_in_past",
                             status_code=422, field="effective_from", extra={"earliest_effective_from": today.isoformat()})
    # One change at a time per Associate, so "the rate in force" below is not stale.
    db.execute(select(Associate.id).where(Associate.id == associate.id).with_for_update())
    current = _percent(resolve(db, associate, CommissionRole.personal_sale, effective_from))
    new = value if value is not None else _percent(standard(db, associate, CommissionRole.personal_sale,
                                                            effective_from))
    reduction = current is not None and (new is None or new < current)
    changed = current is not None and new != current
    notice = reduction or (changed and config.commissions()["individual_rate_increase_needs_notice"])
    earliest = _notice_from(today) if notice else today
    if effective_from < earliest:
        raise AssociateError("A lower rate takes effect only after the notice period." if reduction
                             else "A rate change takes effect only after the notice period.",
                             code="notice_period", status_code=422, field="effective_from",
                             extra={"earliest_effective_from": earliest.isoformat()})
    before = _percent(resolve(db, associate, CommissionRole.personal_sale, effective_from - timedelta(days=1)))
    row = AssociateCommissionRate(associate_id=associate.id, rate_key=key, rate_percent=value,
                                  effective_from=effective_from, reason=reason, set_by=actor,
                                  created_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    db.add(AuditLog(entity_type="associate_commission_rate", entity_id=str(row.id),
                    action="individual_rate_set" if value is not None else "individual_rate_cleared",
                    performed_by=actor,
                    before_value={"rate_percent": pct(current), "rate_percent_day_before": pct(before)},
                    after_value={"associate_id": associate.associate_id, "rate_key": key, "rate_percent": pct(value),
                                 "effective_rate_percent": pct(new), "effective_from": effective_from.isoformat(),
                                 "reduction": reduction, "notice_required": notice, "reason": reason}))
    from app.notifications import associate_emails
    associate_emails.commission_rate_changed(db, associate, before, new, effective_from)
    return row


# ── What an Admin and the Associate see ──────────────────────────────────────────

def change_of(db: Session, associate: Associate, row: AssociateCommissionRate,
              cache: Optional[dict] = None) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """(rate the day before row.effective_from, rate from it) — what the Associate is
    told the change is. Later rows on the same date are not this change's business."""
    role = CommissionRole.personal_sale
    before = _percent(resolve(db, associate, role, row.effective_from - timedelta(days=1), cache=cache))
    after = row.rate_percent if row.rate_percent is not None else _percent(
        standard(db, associate, role, row.effective_from, cache=cache))
    return before, after


def scheduled_changes(db: Session, associate: Associate, today: date, cache: Optional[dict] = None) -> list[dict]:
    """Every future date on which this Associate's personal-sale rate will differ from
    the day before, as things stand: individual rows and confirmed schedules dated
    after today, resolved in date order."""
    role, key = CommissionRole.personal_sale, rate_key(CommissionRole.personal_sale, associate)
    days = set(db.execute(select(AssociateCommissionRate.effective_from).where(
        AssociateCommissionRate.associate_id == associate.id, AssociateCommissionRate.rate_key == key,
        AssociateCommissionRate.effective_from > today)).scalars())
    days |= set(db.execute(select(CommissionRateSchedule.effective_from).where(
        CommissionRateSchedule.effective_from > today, CommissionRateSchedule.is_placeholder.is_(False))).scalars())
    out, previous = [], resolve(db, associate, role, today, cache=cache)
    for day in sorted(days):
        now = resolve(db, associate, role, day, cache=cache)
        if _percent(now) != _percent(previous):
            out.append({"effective_from": day.isoformat(), "rate_percent": pct(_percent(now)),
                        "source": now.source if now else None})
        previous = now
    return out


def resolved_json(r: Optional[Resolved]) -> dict:
    return {"rate_percent": pct(_percent(r)), "source": r.source if r else None,
            "effective_from": r.since.isoformat() if r else None,
            "pending_reason": None if r else "placeholder_rates"}
