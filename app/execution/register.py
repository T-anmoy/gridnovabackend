"""The installed-equipment serial register — every module, inverter and other unit
installed on a project, by serial number.

Serial numbers are recorded as printed. For uniqueness they are compared in a
normalised form (normalize_serial): Unicode NFKC (so a full-width "ＡＢ１" from a phone
keyboard is "AB1"), zero-width characters removed, all whitespace removed, uppercase.
Every other character is kept — hyphens and slashes are part of many serials — and
look-alikes (O/0, I/1) are NOT folded, since that would merge genuinely different
serials. The database refuses a normalised serial already held anywhere in the system
(installed_units.serial_normalized is unique), which is also what settles two people
entering the same serial at the same moment.

Rules (config/execution_payments.yaml execution.register.sanity, placeholder E7):
  * serial length, warranty maximum and bulk size are hard limits (422);
  * module Wp and inverter kW outside their ranges are warnings only;
  * a unit's is_dcr must match its catalogue item (422 dcr_mismatch).
Project flags (visible, non-blocking): dcr_mismatch (a PM Surya Ghar project with any
non-DCR module — emailed once, and subsidy_claimed is refused until an Admin records
why it may proceed) and capacity_mismatch (module Wp total vs the requested capacity
beyond the tolerance).

Once Operations verifies the register it is locked: only an Admin may change it, with
a reason, audited with before and after. Nothing is deleted — a mistaken unit is voided.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.execution import config
from app.memory.models import (
    AuditLog, CustomerOnboarding, Equipment, EquipmentCategory, GovernmentScheme, InstalledUnit,
    InstalledUnitCategory, InstalledUnitStatus, Project, ProjectExecution, ProjectStage, ProposalInputs,
)

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_WHITESPACE = re.compile(r"\s+", re.UNICODE)
DUPLICATE_MESSAGE = "This serial number is already registered on another project."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_serial(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = _ZERO_WIDTH.sub("", text)
    return _WHITESPACE.sub("", text).upper()


def _sanity() -> dict:
    return config.execution()["register"]["sanity"]


# ── Who is asking (for the duplicate-serial privacy rule) ────────────────────────

class Viewer:
    """Staff are told which project holds a serial; an associate only when that
    project is inside their own scope (OnboardingScope)."""

    def __init__(self, *, employee=None, associate=None):
        self.employee, self.associate = employee, associate

    @property
    def actor(self) -> str:
        if self.employee is not None:
            return self.employee.employee_id
        from app.onboarding.service import actor
        return actor(self.associate)

    @property
    def is_admin(self) -> bool:
        return self.employee is not None and self.employee.is_admin


def _holder(db: Session, unit: InstalledUnit, viewer: Viewer) -> Optional[dict]:
    from app.auth.scoping import can_view_onboarding

    onboarding = db.get(CustomerOnboarding, unit.onboarding_id)
    if viewer.employee is not None:
        project = db.get(Project, unit.project_id)
        return {"onboarding_id": unit.onboarding_id, "project_reference_id": project.reference_id if project else None}
    if can_view_onboarding(viewer.associate, onboarding):
        return {"onboarding_id": unit.onboarding_id}
    return None


def _held_by(db: Session, normalized: str) -> Optional[InstalledUnit]:
    return db.execute(select(InstalledUnit).where(InstalledUnit.serial_normalized == normalized)).scalar_one_or_none()


def check_serial(db: Session, raw: str, viewer: Viewer, project_id: Optional[int] = None) -> dict:
    normalized = normalize_serial(raw)
    unit = _held_by(db, normalized) if normalized else None
    if unit is None:
        return {"serial_normalized": normalized, "available": True, "held_by": None}
    if project_id is not None and unit.project_id == project_id:
        return {"serial_normalized": normalized, "available": False, "on_this_project": True,
                "held_by": {"onboarding_id": unit.onboarding_id, "unit_id": unit.id}}
    return {"serial_normalized": normalized, "available": False, "on_this_project": False,
            "message": DUPLICATE_MESSAGE, "held_by": _holder(db, unit, viewer)}


# ── Validation ───────────────────────────────────────────────────────────────────

def _decimal(value, field: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise AssociateError(f"{field} must be a number.", code="invalid_number", status_code=422, field=field)
    if not d.is_finite() or d <= 0:
        raise AssociateError(f"{field} must be a positive number.", code="invalid_number", status_code=422,
                             field=field)
    return d


def _date(value, field: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise AssociateError(f"{field} must be a date (YYYY-MM-DD).", code="invalid_date", status_code=422,
                             field=field)


def validate(db: Session, data: dict) -> tuple[dict, list[dict]]:
    """Clean one unit's fields. Returns (values, warnings); raises AssociateError."""
    sanity = _sanity()
    try:
        category = InstalledUnitCategory(data.get("category"))
    except ValueError:
        raise AssociateError("category must be module, inverter or other.", code="invalid_category", status_code=422,
                             field="category")
    serial = (data.get("serial_number") or "").strip()
    normalized = normalize_serial(serial)
    if not (sanity["serial_min_length"] <= len(normalized) <= sanity["serial_max_length"]):
        raise AssociateError(f"Serial numbers are {sanity['serial_min_length']}–{sanity['serial_max_length']} "
                             "characters long.", code="invalid_serial", status_code=422, field="serial_number")
    equipment = None
    if data.get("equipment_id") is not None:
        equipment = db.get(Equipment, data["equipment_id"])
        if equipment is None:
            raise AssociateError("No such catalogue item.", code="unknown_equipment", status_code=422,
                                 field="equipment_id")
    if category != InstalledUnitCategory.other:
        if equipment is None:
            raise AssociateError("Choose the catalogue item for this unit.", code="equipment_required",
                                 status_code=422, field="equipment_id")
        if equipment.category != EquipmentCategory(category.value):
            raise AssociateError(f"That catalogue item is not a {category.value}.",
                                 code="equipment_category_mismatch", status_code=422, field="equipment_id")
    rating_wp = _decimal(data.get("rating_wp"), "rating_wp")
    rating_kw = _decimal(data.get("rating_kw"), "rating_kw")
    if category == InstalledUnitCategory.module and rating_wp is None:
        raise AssociateError("Give the module's rating in Wp.", code="rating_required", status_code=422,
                             field="rating_wp")
    if category == InstalledUnitCategory.inverter and rating_kw is None:
        raise AssociateError("Give the inverter's rating in kW.", code="rating_required", status_code=422,
                             field="rating_kw")
    is_dcr = data.get("is_dcr")
    if category == InstalledUnitCategory.module and is_dcr is None:
        raise AssociateError("Say whether the module is DCR.", code="dcr_required", status_code=422, field="is_dcr")
    if equipment is not None:
        if is_dcr is None:
            is_dcr = equipment.is_dcr_compliant
        elif bool(is_dcr) != equipment.is_dcr_compliant:
            raise AssociateError("DCR status does not match the catalogue item "
                                 f"({'DCR' if equipment.is_dcr_compliant else 'non-DCR'}).",
                                 code="dcr_mismatch", status_code=422, field="is_dcr")
    start, end = _date(data.get("warranty_start"), "warranty_start"), _date(data.get("warranty_end"), "warranty_end")
    if (start is None) != (end is None):
        raise AssociateError("Give both warranty dates, or neither.", code="warranty_pair", status_code=422,
                             field="warranty_end")
    if start is not None:
        if end <= start:
            raise AssociateError("The warranty must end after it starts.", code="warranty_order", status_code=422,
                                 field="warranty_end")
        years = sanity["warranty_max_years"]
        try:
            limit = start.replace(year=start.year + years)
        except ValueError:   # 29 Feb
            limit = start.replace(year=start.year + years, day=28)
        if end > limit:
            raise AssociateError(f"A warranty longer than {years} years is not plausible.", code="warranty_too_long",
                                 status_code=422, field="warranty_end")
    warnings = []
    if rating_wp is not None and category == InstalledUnitCategory.module and not (
            sanity["module_wp_min"] <= rating_wp <= sanity["module_wp_max"]):
        warnings.append({"code": "rating_out_of_range", "field": "rating_wp",
                         "message": f"Modules are usually {sanity['module_wp_min']}–{sanity['module_wp_max']} Wp."})
    if rating_kw is not None and category == InstalledUnitCategory.inverter and not (
            Decimal(str(sanity["inverter_kw_min"])) <= rating_kw <= Decimal(str(sanity["inverter_kw_max"]))):
        warnings.append({"code": "rating_out_of_range", "field": "rating_kw",
                         "message": f"Inverters are usually {sanity['inverter_kw_min']}–{sanity['inverter_kw_max']} kW."})
    return ({"category": category, "equipment_id": equipment.id if equipment else None, "serial_number": serial,
             "serial_normalized": normalized, "rating_wp": rating_wp, "rating_kw": rating_kw,
             "is_dcr": bool(is_dcr), "warranty_start": start, "warranty_end": end}, warnings)


# ── Changes ──────────────────────────────────────────────────────────────────────

def _locked_guard(execution: ProjectExecution, viewer: Viewer, reason: Optional[str]) -> Optional[str]:
    """Before verification anyone allowed may change the register. After it, only an
    Admin with a reason. Returns the reason to audit (None when unlocked)."""
    if execution.register_verified_at is None:
        return None
    if not viewer.is_admin:
        raise AssociateError("The register has been verified; only an Admin can change it now.",
                             code="register_locked", status_code=409)
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the verified register is being changed.", code="reason_required",
                             status_code=422, field="reason")
    return reason


def _json(unit: InstalledUnit) -> dict:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else str(v) if isinstance(v, Decimal)
                else v.value if hasattr(v, "value") else v)
            for k, v in (("category", unit.category), ("equipment_id", unit.equipment_id),
                         ("serial_number", unit.serial_number), ("rating_wp", unit.rating_wp),
                         ("rating_kw", unit.rating_kw), ("is_dcr", unit.is_dcr),
                         ("warranty_start", unit.warranty_start), ("warranty_end", unit.warranty_end),
                         ("status", unit.status))}


def _insert(db: Session, unit: InstalledUnit, viewer: Viewer) -> None:
    """Flush in a savepoint so a serial taken by someone else a moment ago surfaces as
    409 duplicate_serial, not as a broken transaction."""
    try:
        with db.begin_nested():
            db.add(unit)
            db.flush()
    except IntegrityError:
        held = _held_by(db, unit.serial_normalized)
        raise AssociateError(DUPLICATE_MESSAGE, code="duplicate_serial", status_code=409, field="serial_number",
                             extra={"held_by": _holder(db, held, viewer) if held else None})


def _duplicate(db: Session, normalized: str, viewer: Viewer) -> Optional[dict]:
    held = _held_by(db, normalized)
    if held is None:
        return None
    return {"code": "duplicate_serial", "message": DUPLICATE_MESSAGE, "field": "serial_number",
            "held_by": _holder(db, held, viewer)}


def add_units(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution, rows: list[dict],
              viewer: Viewer, reason: Optional[str] = None) -> tuple[list[InstalledUnit], list[dict]]:
    """Add one or many units — all or nothing. Errors come back per row
    (422 register_rows_invalid, or 409 when the only problems are duplicates)."""
    sanity = _sanity()
    if not rows:
        raise AssociateError("Send at least one unit.", code="no_rows", status_code=422)
    if len(rows) > sanity["bulk_max_rows"]:
        raise AssociateError(f"At most {sanity['bulk_max_rows']} units at a time.", code="too_many_rows",
                             status_code=422)
    audit_reason = _locked_guard(execution, viewer, reason)
    cleaned, errors, warnings, seen = [], [], [], {}
    for i, row in enumerate(rows):
        try:
            values, row_warnings = validate(db, row)
        except AssociateError as exc:
            errors.append({"row": i, **exc.body()})
            continue
        if values["serial_normalized"] in seen:
            errors.append({"row": i, "code": "duplicate_in_batch", "field": "serial_number",
                           "message": f"The same serial number is also in row {seen[values['serial_normalized']]}."})
            continue
        seen[values["serial_normalized"]] = i
        dup = _duplicate(db, values["serial_normalized"], viewer)
        if dup:
            errors.append({"row": i, **dup})
            continue
        cleaned.append(values)
        warnings += [{"row": i, **w} for w in row_warnings]
    if errors:
        only_duplicates = all(e["code"] == "duplicate_serial" for e in errors)
        raise AssociateError("Nothing was saved: fix the rows listed.", code="register_rows_invalid",
                             status_code=409 if only_duplicates else 422, extra={"errors": errors})
    locked = execution.register_verified_at is not None
    units = []
    for values in cleaned:
        unit = InstalledUnit(project_id=onboarding.project_id, onboarding_id=onboarding.id, created_by=viewer.actor,
                             created_at=_now(), **values,
                             status=InstalledUnitStatus.verified if locked else InstalledUnitStatus.entered,
                             verified_by=viewer.actor if locked else None, verified_at=_now() if locked else None)
        _insert(db, unit, viewer)
        units.append(unit)
        db.add(AuditLog(entity_type="installed_unit", entity_id=str(unit.id), action="added",
                        performed_by=viewer.actor, after_value={**_json(unit), "reason": audit_reason}))
    _after_change(db, onboarding, execution)
    return units, warnings


def update_unit(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution, unit: InstalledUnit,
                changes: dict, viewer: Viewer, reason: Optional[str] = None) -> tuple[InstalledUnit, list[dict]]:
    if unit.status == InstalledUnitStatus.voided:
        raise AssociateError("This unit was voided.", code="unit_voided", status_code=409)
    audit_reason = _locked_guard(execution, viewer, reason)
    before = _json(unit)
    merged = {**{k: getattr(unit, k) for k in ("category", "equipment_id", "serial_number", "rating_wp", "rating_kw",
                                               "is_dcr", "warranty_start", "warranty_end")}, **changes}
    merged["category"] = merged["category"].value if hasattr(merged["category"], "value") else merged["category"]
    values, warnings = validate(db, merged)
    if values["serial_normalized"] != unit.serial_normalized:
        dup = _duplicate(db, values["serial_normalized"], viewer)
        if dup:
            raise AssociateError(DUPLICATE_MESSAGE, code="duplicate_serial", status_code=409, field="serial_number",
                                 extra={"held_by": dup["held_by"]})
    for k, v in values.items():
        setattr(unit, k, v)
    unit.updated_at = _now()
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise AssociateError(DUPLICATE_MESSAGE, code="duplicate_serial", status_code=409, field="serial_number")
    db.add(AuditLog(entity_type="installed_unit", entity_id=str(unit.id), action="updated", performed_by=viewer.actor,
                    before_value=before, after_value={**_json(unit), "reason": audit_reason}))
    _after_change(db, onboarding, execution)
    return unit, warnings


def void_unit(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution, unit: InstalledUnit,
              viewer: Viewer, reason: str) -> InstalledUnit:
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why this unit is being voided.", code="reason_required", status_code=422,
                             field="reason")
    if unit.status == InstalledUnitStatus.voided:
        raise AssociateError("This unit is already voided.", code="unit_voided", status_code=409)
    _locked_guard(execution, viewer, reason)
    before = _json(unit)
    unit.status, unit.void_reason, unit.serial_normalized, unit.updated_at = (
        InstalledUnitStatus.voided, reason, None, _now())
    db.flush()
    db.add(AuditLog(entity_type="installed_unit", entity_id=str(unit.id), action="voided", performed_by=viewer.actor,
                    before_value=before, after_value={"status": "voided", "reason": reason}))
    _after_change(db, onboarding, execution)
    return unit


def verify_register(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution,
                    verified_by: str) -> ProjectExecution:
    """Operations confirm the register matches what was installed. Needs at least one
    module and one inverter; locks the register."""
    if execution.register_verified_at is not None:
        raise AssociateError("The register is already verified.", code="register_already_verified", status_code=409)
    units = _active_units(db, onboarding.project_id)
    categories = {u.category for u in units}
    missing = [c.value for c in (InstalledUnitCategory.module, InstalledUnitCategory.inverter) if c not in categories]
    if missing:
        raise AssociateError("The register needs at least one " + " and one ".join(missing) + ".",
                             code="register_incomplete", status_code=422, extra={"missing": missing})
    now = _now()
    for unit in units:
        unit.status, unit.verified_by, unit.verified_at = InstalledUnitStatus.verified, verified_by, now
    execution.register_verified_at, execution.register_verified_by = now, verified_by
    db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="register_verified",
                    performed_by=verified_by, after_value={"units": len(units)}))
    db.flush()
    return execution


def record_dcr_override(db: Session, execution: ProjectExecution, admin_id: str, reason: str) -> ProjectExecution:
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Record why the project may proceed.", code="reason_required", status_code=422,
                             field="reason")
    if execution.dcr_override_at is not None:
        raise AssociateError("An override is already recorded.", code="override_exists", status_code=409)
    execution.dcr_override_reason, execution.dcr_override_by, execution.dcr_override_at = reason, admin_id, _now()
    db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="dcr_override",
                    performed_by=admin_id, after_value={"reason": reason}))
    db.flush()
    return execution


# ── Flags, blockers, summary ─────────────────────────────────────────────────────

def _active_units(db: Session, project_id: int) -> list[InstalledUnit]:
    return list(db.execute(select(InstalledUnit).where(
        InstalledUnit.project_id == project_id, InstalledUnit.status != InstalledUnitStatus.voided)
        .order_by(InstalledUnit.id)).scalars())


def _pm_surya_ghar(db: Session, onboarding: CustomerOnboarding) -> bool:
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    return inputs is not None and inputs.scheme_name == GovernmentScheme.pm_surya_ghar


def dcr_mismatch(db: Session, onboarding: CustomerOnboarding) -> bool:
    if not _pm_surya_ghar(db, onboarding):
        return False
    return bool(db.execute(select(func.count()).select_from(InstalledUnit).where(
        InstalledUnit.project_id == onboarding.project_id, InstalledUnit.status != InstalledUnitStatus.voided,
        InstalledUnit.category == InstalledUnitCategory.module, InstalledUnit.is_dcr.is_(False))).scalar_one())


def capacity(db: Session, onboarding: CustomerOnboarding, units: Optional[list] = None) -> dict:
    units = _active_units(db, onboarding.project_id) if units is None else units
    module_wp = sum((u.rating_wp or Decimal(0) for u in units if u.category == InstalledUnitCategory.module),
                    Decimal(0))
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    requested = Decimal(str(inputs.desired_output_kw)) if inputs and inputs.desired_output_kw else None
    tolerance = Decimal(str(_sanity()["capacity_tolerance_percent"]))
    mismatch = False
    if requested and module_wp:
        installed_kw = module_wp / Decimal(1000)
        mismatch = abs(installed_kw - requested) > requested * tolerance / Decimal(100)
    return {"installed_module_kw": str((module_wp / Decimal(1000)).quantize(Decimal("0.001"))),
            "requested_kw": str(requested) if requested is not None else None,
            "tolerance_percent": str(tolerance), "mismatch": mismatch}


def project_flags(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution) -> list[str]:
    flags = []
    if dcr_mismatch(db, onboarding) and execution.dcr_override_at is None:
        flags.append("dcr_mismatch")
    if capacity(db, onboarding)["mismatch"]:
        flags.append("capacity_mismatch")
    return flags


def stage_blockers(db: Session, onboarding: CustomerOnboarding, stage: ProjectStage) -> list[dict]:
    if stage != ProjectStage.subsidy_claimed:
        return []
    execution = db.execute(select(ProjectExecution).where(
        ProjectExecution.onboarding_id == onboarding.id)).scalar_one_or_none()
    if execution is not None and execution.dcr_override_at is None and dcr_mismatch(db, onboarding):
        return [{"code": "dcr_mismatch", "message": "A non-DCR module is registered on this PM Surya Ghar project; "
                                                    "an Admin must record why the subsidy claim may proceed."}]
    return []


def _after_change(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution) -> None:
    """Email Operations and Admin the first time a PM Surya Ghar project gets a
    non-DCR module (post-commit; never again for the same project)."""
    if execution.dcr_alerted_at is not None or not dcr_mismatch(db, onboarding):
        return
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Customer
    from app.notifications import notifier as notifier_module

    execution.dcr_alerted_at = _now()
    customer = db.get(Customer, onboarding.customer_id)
    payload = {"onboarding_id": onboarding.id, "customer_name": customer.name if customer else None,
               "project_reference_id": db.get(Project, onboarding.project_id).reference_id}
    MemoryManager(db).notify_after_commit(lambda: notifier_module.notifier.notify("dcr_mismatch", payload),
                                          "dcr_mismatch")


def unit_json(db: Session, unit: InstalledUnit) -> dict:
    equipment = db.get(Equipment, unit.equipment_id) if unit.equipment_id else None
    return {
        "unit_id": unit.id, "category": unit.category.value, "serial_number": unit.serial_number,
        "serial_normalized": unit.serial_normalized,
        "equipment": None if equipment is None else {
            "id": equipment.id, "brand": equipment.brand, "model_name": equipment.model_name,
            "is_dcr_compliant": equipment.is_dcr_compliant, "is_placeholder": bool(equipment.is_placeholder)},
        "rating_wp": str(unit.rating_wp) if unit.rating_wp is not None else None,
        "rating_kw": str(unit.rating_kw) if unit.rating_kw is not None else None,
        "is_dcr": unit.is_dcr,
        "warranty_start": unit.warranty_start.isoformat() if unit.warranty_start else None,
        "warranty_end": unit.warranty_end.isoformat() if unit.warranty_end else None,
        "status": unit.status.value, "created_by": unit.created_by, "created_at": unit.created_at.isoformat(),
        "verified_by": unit.verified_by, "verified_at": unit.verified_at.isoformat() if unit.verified_at else None,
        "void_reason": unit.void_reason,
    }


def summary(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution) -> dict:
    """The register block of the execution status."""
    units = list(db.execute(select(InstalledUnit).where(InstalledUnit.project_id == onboarding.project_id)
                            .order_by(InstalledUnit.id)).scalars())
    active = [u for u in units if u.status != InstalledUnitStatus.voided]
    inverter_kw = sum((u.rating_kw or Decimal(0) for u in active if u.category == InstalledUnitCategory.inverter),
                      Decimal(0))
    return {"register": {
        "verified_at": execution.register_verified_at.isoformat() if execution.register_verified_at else None,
        "verified_by": execution.register_verified_by,
        "units": [unit_json(db, u) for u in units],
        "totals": {"modules": sum(u.category == InstalledUnitCategory.module for u in active),
                   "inverters": sum(u.category == InstalledUnitCategory.inverter for u in active),
                   "other": sum(u.category == InstalledUnitCategory.other for u in active),
                   "inverter_kw": str(inverter_kw.quantize(Decimal("0.001")))},
        "capacity": capacity(db, onboarding, active),
        "dcr": {"pm_surya_ghar": _pm_surya_ghar(db, onboarding), "mismatch": dcr_mismatch(db, onboarding),
                "override": None if execution.dcr_override_at is None else {
                    "reason": execution.dcr_override_reason, "by": execution.dcr_override_by,
                    "at": execution.dcr_override_at.isoformat()}},
        "flags": project_flags(db, onboarding, execution),
    }}
