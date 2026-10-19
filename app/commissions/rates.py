"""Commission rate schedules (Commission & Payout Policy 4.1).

A schedule is a complete set of rates — one percentage per RATE_KEYS key — published
by an Admin with an effective date. It is never edited (a database trigger refuses
it): a change is a new version. Policy 4.1: a change needs commissions.rate_change_notice_days
days' notice (config/commissions.yaml), and "the rate that applies is the one in force
on the date the customer's agreement is signed" — in_force(agreement_date).

A schedule marked is_placeholder carries rates nobody has confirmed: entries under it
are recorded without an amount (pending_reason placeholder_rates) and no payout
includes them. With no schedule at all the same happens.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.commissions import config
from app.memory.models import (
    Associate, AssociateType, AuditLog, CommissionRate, CommissionRateSchedule, CommissionRole,
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


def _rate(raw, key: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        value = Decimal("NaN")
    if not value.is_finite() or value < 0 or value > 100 or value != value.quantize(RATE_QUANTUM):
        raise AssociateError(f"The rate for {key} must be a percentage from 0 to 100 with at most four decimals.",
                             code="invalid_rate", status_code=422, field=f"rates.{key}")
    return value.quantize(RATE_QUANTUM)


def earliest_effective_from(today: date, is_placeholder: bool) -> date:
    return today if is_placeholder else today + timedelta(days=config.commissions()["rate_change_notice_days"])


def publish(db: Session, *, effective_from: date, is_placeholder: bool, rates: dict, note: Optional[str],
            actor: str, today: date) -> CommissionRateSchedule:
    """Publish a new schedule version. Every RATE_KEYS key is required, nothing else
    is accepted. A real (non-placeholder) schedule takes effect no sooner than the
    notice period; none takes effect in the past."""
    unknown = sorted(set(rates) - set(RATE_KEYS))
    missing = [k for k in RATE_KEYS if k not in rates]
    if unknown or missing:
        raise AssociateError("A schedule must set exactly one rate for every rate key.", code="invalid_rate_keys",
                             status_code=422, field="rates", extra={"missing": missing, "unknown": unknown})
    values = {k: _rate(rates[k], k) for k in RATE_KEYS}
    earliest = earliest_effective_from(today, is_placeholder)
    if effective_from < earliest:
        raise AssociateError(
            "A rate change takes effect only after the notice period." if not is_placeholder
            else "A schedule cannot take effect in the past.",
            code="notice_period" if not is_placeholder else "effective_in_past", status_code=422,
            field="effective_from", extra={"earliest_effective_from": earliest.isoformat()})
    version = (db.execute(select(func.max(CommissionRateSchedule.version))).scalar_one() or 0) + 1
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
                                 "is_placeholder": is_placeholder, "rates": {k: str(v) for k, v in values.items()}}))
    return schedule


def schedule_json(schedule: CommissionRateSchedule, rates: dict[str, Decimal], today: date) -> dict:
    return {"schedule_id": schedule.id, "version": schedule.version,
            "effective_from": schedule.effective_from.isoformat(), "is_placeholder": schedule.is_placeholder,
            "note": schedule.note, "published_by": schedule.published_by,
            "published_at": schedule.published_at.isoformat(),
            "rates": {k: str(rates[k]) for k in RATE_KEYS if k in rates},
            "in_effect": schedule.effective_from <= today}
