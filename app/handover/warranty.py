"""The warranty register (BE-5): written when `handover_complete` is recorded.

    system warranty   handover date + warranty.system_warranty_years (confirmed: 5)
    per-unit          the serial register's own warranty dates when it has them; else the
                      configured years for the unit's category; else no end date
    O&M               warranty.om_free_months free from the handover date; the terms after
                      that are config text (a placeholder until Grid Nova supplies them)

Dates are IST calendar dates. Nothing here invents a number: a category with no configured
years simply has no end date, and a panel performance life that Grid Nova has not chosen
is not stated.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.handover import config
from app.memory.models import (
    AuditLog, CustomerOnboarding, InstalledUnit, InstalledUnitStatus, ProjectStage, ProjectStageEvent, ProjectWarranty,
    WarrantyItem,
)


def today() -> date:
    from app.timezones import _as_ist
    return _as_ist(datetime.now(timezone.utc)).date()


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:   # 29 February
        return d.replace(year=d.year + years, day=28)


def add_months(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(d.day, last[month - 1]))


def get(db: Session, project_id: int) -> Optional[ProjectWarranty]:
    return db.execute(select(ProjectWarranty).where(ProjectWarranty.project_id == project_id)).scalar_one_or_none()


def _handover_date(event: ProjectStageEvent) -> date:
    from app.timezones import _as_ist
    return _as_ist(event.recorded_at).date()


def create(db: Session, onboarding: CustomerOnboarding, actor: str,
           event: Optional[ProjectStageEvent] = None) -> Optional[ProjectWarranty]:
    """Idempotent. None when the project has not been handed over."""
    existing = get(db, onboarding.project_id)
    if existing is not None:
        return existing
    event = event or db.execute(select(ProjectStageEvent).where(
        ProjectStageEvent.project_id == onboarding.project_id,
        ProjectStageEvent.stage == ProjectStage.handover_complete)).scalar_one_or_none()
    if event is None:
        return None
    cfg = config.warranty()
    start = _handover_date(event)
    warranty = ProjectWarranty(
        project_id=onboarding.project_id, onboarding_id=onboarding.id, handover_date=start,
        system_warranty_years=cfg["system_warranty_years"],
        system_warranty_end=add_years(start, cfg["system_warranty_years"]),
        panel_performance_life_years=cfg["panel_performance_life_years"], om_free_months=cfg["om_free_months"],
        om_free_until=add_months(start, cfg["om_free_months"]), created_by=actor)
    db.add(warranty)
    db.flush()
    units = db.execute(select(InstalledUnit).where(
        InstalledUnit.project_id == onboarding.project_id, InstalledUnit.status != InstalledUnitStatus.voided)
        .order_by(InstalledUnit.id)).scalars()
    defaults = cfg["unit_default_years"]
    for unit in units:
        category = unit.category.value
        if unit.warranty_end is not None:
            item = (unit.warranty_start or start, unit.warranty_end, "register")
        elif defaults.get(category) is not None:
            item = (start, add_years(start, defaults[category]), "default")
        else:
            item = (None, None, "none")
        db.add(WarrantyItem(warranty_id=warranty.id, unit_id=unit.id, category=category, warranty_start=item[0],
                            warranty_end=item[1], source=item[2]))
    db.add(AuditLog(entity_type="project_warranty", entity_id=str(warranty.id), action="created", performed_by=actor,
                    after_value={"project_id": onboarding.project_id, "handover_date": start.isoformat(),
                                 "system_warranty_end": warranty.system_warranty_end.isoformat()}))
    db.flush()
    return warranty


def ensure(db: Session, onboarding: CustomerOnboarding, actor: str) -> Optional[ProjectWarranty]:
    """For a project handed over before the register existed: derive it from the recorded
    stage the first time it is needed."""
    return get(db, onboarding.project_id) or create(db, onboarding, actor)


def within_warranty(warranty: Optional[ProjectWarranty], on: Optional[date] = None) -> Optional[bool]:
    if warranty is None:
        return None
    return (on or today()) <= warranty.system_warranty_end


def serialize(db: Session, warranty: Optional[ProjectWarranty]) -> Optional[dict]:
    if warranty is None:
        return None
    now = today()
    items = db.execute(select(WarrantyItem, InstalledUnit).join(InstalledUnit, InstalledUnit.id == WarrantyItem.unit_id)
                       .where(WarrantyItem.warranty_id == warranty.id).order_by(WarrantyItem.id)).all()
    cfg = config.warranty()
    return {
        "handover_date": warranty.handover_date.isoformat(),
        "system": {"years": warranty.system_warranty_years, "starts": warranty.handover_date.isoformat(),
                   "ends": warranty.system_warranty_end.isoformat(), "active": now <= warranty.system_warranty_end},
        "om": {"free_months": warranty.om_free_months, "free_until": warranty.om_free_until.isoformat(),
               "free_period_active": now <= warranty.om_free_until,
               "terms_after_free_period": None if _is_dummy(cfg["om_after_free_terms"]) else cfg["om_after_free_terms"]},
        "panel_performance_life_years": warranty.panel_performance_life_years,
        "units": [{"unit_id": unit.id, "category": item.category, "serial_number": unit.serial_number,
                   "make": unit.brand_as_printed, "model": unit.model_as_printed, "equipment_id": unit.equipment_id,
                   "warranty_start": item.warranty_start.isoformat() if item.warranty_start else None,
                   "warranty_end": item.warranty_end.isoformat() if item.warranty_end else None,
                   "source": item.source,
                   "active": bool(item.warranty_end and now <= item.warranty_end)} for item, unit in items],
    }


def _is_dummy(text: str) -> bool:
    from app.placeholders.registry import MARKERS
    return bool(MARKERS.search(text))
