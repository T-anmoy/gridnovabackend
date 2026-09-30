"""The placeholder registry for staff: what Grid Nova still has to supply, grouped by
area, with whether each dummy is still in place (config/placeholder_registry.yaml).

Admin and Management read it; only Admin marks a `manual` entry resolved (with a
note, audited, append-only). Probed entries resolve by editing the value itself.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import AuditLog, EmployeeRole, PlaceholderResolution
from app.placeholders import registry

router = APIRouter(prefix="/api/v1/dashboard/placeholders")


def _item(entry: registry.Entry, state: dict) -> dict:
    return {
        "id": entry.id,
        "area": entry.area,
        "location": entry.location_label(),
        "description": entry.description,
        "dummy_value": entry.dummy_value,
        "needed_from_grid_nova": entry.needed_from_grid_nova,
        "blocking": entry.blocking,
        "probe": entry.probe,
        "resolved": state["resolved"] is True,
        "probe_error": state["error"],
        "resolution": state["resolution"],
    }


@router.get("")
def list_placeholders(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if employee.role not in (EmployeeRole.admin, EmployeeRole.management):
        raise HTTPException(status_code=403, detail="Only Admin and Management can view the placeholder registry.")
    items = [_item(e, s) for e, s in registry.evaluate_all(db)]
    by_blocking = {level: {"total": 0, "unresolved": 0} for level in registry.BLOCKING}
    for item in items:
        by_blocking[item["blocking"]]["total"] += 1
        by_blocking[item["blocking"]]["unresolved"] += 0 if item["resolved"] else 1
    areas = [{"area": area, "items": [i for i in items if i["area"] == area]} for area in registry.AREAS]
    return {
        "summary": {"total": len(items), "unresolved": sum(1 for i in items if not i["resolved"]),
                    "by_blocking": by_blocking},
        "areas": [a for a in areas if a["items"]],
    }


class ResolveRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@router.post("/{entry_id}/resolve")
def resolve_placeholder(entry_id: str, body: ResolveRequest, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can resolve a placeholder.")
    entry = registry.load().by_id.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such placeholder.")
    if entry.probe != "manual":
        raise HTTPException(status_code=409, detail={
            "code": "not_manual",
            "message": f"{entry_id} resolves itself when its value is changed ({entry.location_label()})."})
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=422, detail={"code": "note_required", "field": "note",
                                                     "message": "Say what was confirmed."})
    if db.execute(select(PlaceholderResolution).where(PlaceholderResolution.entry_id == entry_id)).first():
        raise HTTPException(status_code=409, detail={"code": "already_resolved",
                                                     "message": f"{entry_id} is already resolved."})
    now = datetime.now(timezone.utc)
    db.add(PlaceholderResolution(entry_id=entry_id, resolved_by=employee.employee_id, resolved_at=now, note=note))
    db.add(AuditLog(entity_type="placeholder", entity_id=entry_id, action="resolved",
                    performed_by=employee.employee_id, after_value={"note": note}))
    db.commit()
    return _item(entry, registry.evaluate(entry, db))
