"""The installed-equipment serial register for staff (app/execution/register.py).

Included BEFORE app/api/routes/execution_admin.py: its POST .../execution/units and
.../execution/dcr-override would otherwise be taken for a stage by
POST .../execution/{stage}.

Read: anyone who may see the project's execution (Engineer included — this is the
technical record). Change: Operations and Admin; after Operations verifies the
register, Admin only, with a reason. DCR override: Admin only.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.associate_execution import BulkUnitsIn, UnitIn, UnitPatch, VoidIn
from app.api.routes.execution_admin import _detail, _load
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.execution import register
from app.execution import service as execution
from app.memory.db import get_db
from app.memory.models import InstalledUnit, OnboardingStatus

router = APIRouter(prefix="/api/v1/dashboard/onboardings/{onboarding_id}/execution")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _target(db: Session, employee: AuthenticatedEmployee, onboarding_id: int, *, change: bool):
    if change and not employee.can_record_execution():
        raise HTTPException(status_code=403, detail="Only Operations or Admin can change the serial register.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        record = execution.require(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)
    if change and onboarding.status == OnboardingStatus.closed:
        raise HTTPException(status_code=409, detail={"code": "not_editable", "message": "This customer record is closed."})
    return onboarding, record, register.Viewer(employee=employee)


def _write(db: Session, fn):
    try:
        result = fn()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


def _unit(db: Session, onboarding, unit_id: int) -> InstalledUnit:
    unit = db.get(InstalledUnit, unit_id)
    if unit is None or unit.onboarding_id != onboarding.id:
        raise HTTPException(status_code=404, detail="Unit not found")
    return unit


@router.get("/units")
def list_units(onboarding_id: int, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, record, _ = _target(db, employee, onboarding_id, change=False)
    return register.summary(db, onboarding, record, staff=True)["register"]


@router.get("/units/check-serial")
def check_serial(onboarding_id: int, serial: str, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, _, viewer = _target(db, employee, onboarding_id, change=False)
    return register.check_serial(db, serial[:200], viewer, project_id=onboarding.project_id)


@router.post("/units", status_code=201)
def add_unit(onboarding_id: int, body: UnitIn, reason: Optional[str] = None, db: Session = Depends(get_db),
             employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, record, viewer = _target(db, employee, onboarding_id, change=True)
    units, warnings = _write(db, lambda: register.add_units(db, onboarding, record, [body.model_dump()], viewer,
                                                            reason))
    return {"unit": register.unit_json(db, units[0], staff=True), "warnings": warnings,
            "register": register.summary(db, onboarding, record, staff=True)["register"]}


@router.post("/units/bulk", status_code=201)
def add_units_bulk(onboarding_id: int, body: BulkUnitsIn, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, record, viewer = _target(db, employee, onboarding_id, change=True)
    units, warnings = _write(db, lambda: register.add_units(
        db, onboarding, record, [u.model_dump() for u in body.units], viewer, body.reason))
    return {"units": [register.unit_json(db, u, staff=True) for u in units], "warnings": warnings,
            "register": register.summary(db, onboarding, record, staff=True)["register"]}


@router.patch("/units/{unit_id}")
def update_unit(onboarding_id: int, unit_id: int, body: UnitPatch, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, record, viewer = _target(db, employee, onboarding_id, change=True)
    unit = _unit(db, onboarding, unit_id)
    changes = body.model_dump(exclude_unset=True)
    reason = changes.pop("reason", None)
    unit, warnings = _write(db, lambda: register.update_unit(db, onboarding, record, unit, changes, viewer, reason))
    return {"unit": register.unit_json(db, unit, staff=True), "warnings": warnings,
            "register": register.summary(db, onboarding, record, staff=True)["register"]}


@router.post("/units/{unit_id}/void")
def void_unit(onboarding_id: int, unit_id: int, body: VoidIn, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding, record, viewer = _target(db, employee, onboarding_id, change=True)
    unit = _unit(db, onboarding, unit_id)
    _write(db, lambda: register.void_unit(db, onboarding, record, unit, viewer, body.reason))
    return {"unit": register.unit_json(db, unit, staff=True), "register": register.summary(db, onboarding, record, staff=True)["register"]}


@router.post("/register/verify")
def verify_register(onboarding_id: int, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations confirm the register; it is locked from then on."""
    if not employee.can_verify_execution():
        raise HTTPException(status_code=403, detail="Only Operations or Admin can verify the serial register.")
    onboarding, record, _ = _target(db, employee, onboarding_id, change=False)
    _write(db, lambda: register.verify_register(db, onboarding, record, employee.employee_id))
    return _detail(db, onboarding, employee)


class OverrideIn(BaseModel):
    reason: str = Field(max_length=2000)


@router.post("/dcr-override")
def dcr_override(onboarding_id: int, body: OverrideIn, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Admin records why a PM Surya Ghar project with a non-DCR module may still claim
    the subsidy. Clears the dcr_mismatch flag and the subsidy_claimed block."""
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can record a DCR override.")
    onboarding, record, _ = _target(db, employee, onboarding_id, change=False)
    _write(db, lambda: register.record_dcr_override(db, record, employee.employee_id, body.reason))
    return _detail(db, onboarding, employee)


class CatalogueIn(BaseModel):
    category: Literal["module", "inverter", "structure", "bos_component"]
    brand: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=100)        # exactly as printed on the nameplate
    specification: Optional[str] = Field(default=None, max_length=2000)
    is_dcr_compliant: bool                                          # as the OEM declaration states
    equivalent_reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/catalogue", status_code=201)
def catalogue_from_nameplate(onboarding_id: int, body: CatalogueIn, db: Session = Depends(get_db),
                             employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations/Admin add the installed equipment to the catalogue from its nameplate
    (model as printed, DCR from the OEM declaration). The brand must be an approved make
    for the project's package and component (Grid Nova's master data); otherwise
    `equivalent_reason` is required (422 make_not_approved). Returns the catalogue item;
    units then reference it by `equipment_id`."""
    onboarding, _, viewer = _target(db, employee, onboarding_id, change=True)
    item = _write(db, lambda: register.catalogue_from_nameplate(db, onboarding, viewer, body.model_dump()))
    package = register.project_package(db, onboarding)
    component = register.COMPONENT_FOR_CATEGORY.get(body.category)
    return {"equipment": {"id": item.id, "category": item.category.value, "brand": item.brand,
                          "model_name": item.model_name, "specification": item.specification,
                          "is_dcr_compliant": item.is_dcr_compliant, "is_active": item.is_active},
            "package": package, "approved_makes": register.approved_makes(package, component),
            "in_package": register.make_in_package(package, component, item.brand)}


class MatchIn(BaseModel):
    equipment_id: int
    reason: Optional[str] = Field(default=None, max_length=2000)   # required by the lock after verification


@router.post("/units/{unit_id}/match")
def match_unit(onboarding_id: int, unit_id: int, body: MatchIn, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations/Admin match an unmatched unit (registered with the brand/model as
    printed) to a catalogue item — of the unit's category and agreeing with its declared
    DCR status. Create the item first with POST .../execution/catalogue when it does not
    exist. Staff only; associates never see unmatched status."""
    onboarding, record, viewer = _target(db, employee, onboarding_id, change=True)
    unit = _unit(db, onboarding, unit_id)
    _write(db, lambda: register.match_unit(db, onboarding, record, unit, body.equipment_id, viewer, body.reason))
    return {"unit": register.unit_json(db, unit, staff=True),
            "register": register.summary(db, onboarding, record, staff=True)["register"]}
