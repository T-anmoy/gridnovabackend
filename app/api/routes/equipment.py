"""
Equipment / SKU master — the catalog of modules, inverters, structures and
balance-of-system components Grid Nova actually installs. Empty today,
ready for real data. Read is open to any authenticated employee (an Engineer
or Sales rep needs to see what's available); writes are Admin-only, and
deactivation is the only way to retire an entry — never a hard delete, the
same discipline as Employee.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import EmployeeRole, EquipmentCategory

# Same population that sees proposal pricing elsewhere (dashboard.py's
# `if employee.is_engineer:` restricted-projection pattern) — Admin, Sales and
# Management see commercial figures; Engineer does not. Written as an allow-list
# rather than replicating the `is_engineer` check literally so Operations and
# Accounts (roles that didn't exist when that precedent was set, and which start
# maximally restricted by design) are excluded too, not accidentally included.
_CAN_VIEW_PROCUREMENT_PRICE = (EmployeeRole.admin, EmployeeRole.sales, EmployeeRole.management)

router = APIRouter(prefix="/api/v1/equipment")


class EquipmentCreate(BaseModel):
    category: EquipmentCategory
    brand: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    specification: Optional[str] = None
    is_dcr_compliant: bool = False
    procurement_price_inr: Optional[float] = Field(default=None, ge=0)


class EquipmentUpdate(BaseModel):
    category: Optional[EquipmentCategory] = None
    brand: Optional[str] = Field(default=None, min_length=1)
    model_name: Optional[str] = Field(default=None, min_length=1)
    specification: Optional[str] = None
    is_dcr_compliant: Optional[bool] = None
    procurement_price_inr: Optional[float] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


def _serialize(item, employee: AuthenticatedEmployee) -> dict:
    body = {
        "id": item.id,
        "category": item.category,
        "brand": item.brand,
        "model_name": item.model_name,
        "specification": item.specification,
        "is_dcr_compliant": item.is_dcr_compliant,
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
    # Absent, not null, for a role that can't see it — so the frontend can tell
    # "not permitted" apart from "no price set yet" (which is a real null).
    if employee.role in _CAN_VIEW_PROCUREMENT_PRICE:
        body["procurement_price_inr"] = item.procurement_price_inr
    return body


@router.get("/")
def list_equipment(
    category: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    category_enum: Optional[EquipmentCategory] = None
    if category is not None:
        try:
            category_enum = EquipmentCategory(category)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown category: {category!r}")

    memory = MemoryManager(db)
    items = memory.list_equipment(category=category_enum, is_active=is_active)
    return {"items": [_serialize(i, employee) for i in items]}


@router.post("/")
def create_equipment(
    body: EquipmentCreate,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can add equipment.")

    memory = MemoryManager(db)
    item = memory.create_equipment(
        category=body.category,
        brand=body.brand,
        model_name=body.model_name,
        performed_by=employee.employee_id,
        specification=body.specification,
        is_dcr_compliant=body.is_dcr_compliant,
        procurement_price_inr=body.procurement_price_inr,
    )
    db.commit()
    return _serialize(item, employee)


@router.patch("/{equipment_id}")
def update_equipment(
    equipment_id: int,
    body: EquipmentUpdate,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can edit equipment.")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")

    memory = MemoryManager(db)
    item = memory.update_equipment(equipment_id, performed_by=employee.employee_id, **fields)
    if not item:
        raise HTTPException(status_code=404, detail="Equipment not found")
    db.commit()
    return _serialize(item, employee)
