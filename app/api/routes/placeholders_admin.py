"""The placeholder registry for staff: what Grid Nova still has to supply, grouped by
area, with whether each dummy is still in place (config/placeholder_registry.yaml).

Admin and Management read it; only Admin marks a `manual` entry resolved, and only Admin
approves or revokes an `approved_text` entry's wording (BE-22) — each with a note,
audited, append-only, rate-limited per Admin. An approval is of the exact text the Admin
saw (its SHA-256); editing the text lapses it. Other probed entries resolve by editing
the value itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import AuditLog, PlaceholderResolution
from app.placeholders import registry
from app.security.rate_limit import RateLimitLog

router = APIRouter(prefix="/api/v1/dashboard/placeholders")

# Approvals and revocations per Admin: generous for a person reading and approving texts,
# tight enough to stop a runaway client.
DECISION_WINDOW_SECONDS = 60
DECISION_LIMIT = 30
_decision_log = RateLimitLog(DECISION_WINDOW_SECONDS)


def _item(entry: registry.Entry, state: dict, approval: Optional[dict] = None) -> dict:
    item = {
        "id": entry.id,
        "area": entry.area,
        "location": entry.location_label(),
        "description": entry.description,
        "dummy_value": entry.dummy_value,
        "needed_from_grid_nova": entry.needed_from_grid_nova,
        "blocking": entry.blocking,
        "probe": entry.probe,
        "resolved": state["resolved"] is True,
        "status": entry.status,
        "status_note": entry.status_note,
        "probe_error": state["error"],
        "resolution": state["resolution"],
        "legal_review_required": entry.legal_review_required,
        "current_text": None,
        "current_sha256": None,
        "approval": None,
        "approval_state": None,
    }
    if approval is not None:
        item.update(current_text=approval["text"], current_sha256=approval["sha256"],
                    approval=approval["approval"], approval_state=approval["state"])
    return item


def _approval(entry: registry.Entry, db: Session, rows: Optional[list] = None) -> Optional[dict]:
    if entry.probe != "approved_text":
        return None
    try:
        state = registry.approval_state(entry, db, rows)
        return {**state, "text": registry.current_text(entry)}
    except Exception:   # the probe error already shows on the item
        return {"state": "not_approved", "approval": None, "sha256": None, "text": None}


@router.get("")
def list_placeholders(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.has("placeholders.view"):
        raise HTTPException(status_code=403, detail="Only Admin and Management can view the placeholder registry.")
    rows = registry._resolutions(db)
    items = [_item(e, s, _approval(e, db, rows.get(e.id, []))) for e, s in registry.evaluate_all(db)]
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
    if not employee.has("placeholders.resolve"):
        raise HTTPException(status_code=403, detail="Only Admin can resolve a placeholder.")
    entry = registry.load().by_id.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such placeholder.")
    if entry.probe != "manual":
        raise HTTPException(status_code=409, detail={
            "code": "not_manual",
            "message": f"{entry_id} resolves itself when its value is changed ({entry.location_label()})."
                       if entry.probe != "approved_text" else
                       f"{entry_id} is wording: approve it (POST …/{entry_id}/approve) instead."})
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=422, detail={"code": "note_required", "field": "note",
                                                     "message": "Say what was confirmed."})
    if db.execute(select(PlaceholderResolution).where(PlaceholderResolution.entry_id == entry_id,
                                                      PlaceholderResolution.action == registry.RESOLVED)).first():
        raise HTTPException(status_code=409, detail={"code": "already_resolved",
                                                     "message": f"{entry_id} is already resolved."})
    now = datetime.now(timezone.utc)
    db.add(PlaceholderResolution(entry_id=entry_id, action=registry.RESOLVED, resolved_by=employee.employee_id,
                                 resolved_at=now, note=note))
    db.add(AuditLog(entity_type="placeholder", entity_id=entry_id, action="resolved",
                    performed_by=employee.employee_id, after_value={"note": note}))
    db.commit()
    return _item(entry, registry.evaluate(entry, db))


def _decision_entry(entry_id: str, employee: AuthenticatedEmployee) -> registry.Entry:
    if not employee.has("placeholders.resolve"):
        raise HTTPException(status_code=403, detail="Only Admin can approve or revoke wording.")
    entry = registry.load().by_id.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such placeholder.")
    if entry.probe != "approved_text":
        raise HTTPException(status_code=409, detail={"code": "not_approved_text",
                                                     "message": f"{entry_id} is not wording that needs approval."})
    if not _decision_log.hit(employee.employee_id, DECISION_LIMIT):
        raise HTTPException(status_code=429, detail={"code": "rate_limited",
                                                     "message": "Too many approvals in a minute. Wait and try again."})
    return entry


class ApproveRequest(BaseModel):
    content_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    note: Optional[str] = Field(default=None, max_length=2000)
    legal_review_confirmed: bool = False


@router.post("/{entry_id}/approve")
def approve_wording(entry_id: str, body: ApproveRequest, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Approve the wording exactly as the Admin read it (content_sha256 from the listing).
    409 text_changed if the text is no longer that one; 409 still_placeholder while it
    carries a marker; 422 legal_review_required without the legal adviser's tick where
    the entry needs it."""
    entry = _decision_entry(entry_id, employee)
    if registry.has_marker(entry):
        raise HTTPException(status_code=409, detail={
            "code": "still_placeholder",
            "message": f"{entry_id} still contains placeholder text and cannot be approved."})
    current = registry.sha256(registry.current_text(entry))
    if body.content_sha256 != current:
        raise HTTPException(status_code=409, detail={
            "code": "text_changed", "current_sha256": current,
            "message": "The wording has changed since you read it. Reload and read it again before approving."})
    if entry.legal_review_required and not body.legal_review_confirmed:
        raise HTTPException(status_code=422, detail={
            "code": "legal_review_required", "field": "legal_review_confirmed",
            "message": "Confirm that Grid Nova's legal adviser has reviewed this wording."})
    note = (body.note or "").strip()
    now = datetime.now(timezone.utc)
    legal = bool(body.legal_review_confirmed)
    db.add(PlaceholderResolution(entry_id=entry_id, action=registry.APPROVED, resolved_by=employee.employee_id,
                                 resolved_at=now, note=note, content_sha256=current, legal_review_confirmed=legal))
    db.add(AuditLog(entity_type="placeholder", entity_id=entry_id, action="wording_approved",
                    performed_by=employee.employee_id,
                    after_value={"content_sha256": current, "note": note, "legal_review_confirmed": legal}))
    db.commit()
    return _item(entry, registry.evaluate(entry, db), _approval(entry, db))


class RevokeRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@router.post("/{entry_id}/revoke")
def revoke_wording(entry_id: str, body: RevokeRequest, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Withdraw the approval: the wording is unapproved from now on (documents using it
    are held again). 409 not_approved when there is no standing approval."""
    entry = _decision_entry(entry_id, employee)
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=422, detail={"code": "note_required", "field": "note",
                                                     "message": "Say why the approval is withdrawn."})
    before = registry.approval_state(entry, db)
    if before["approval"] is None:
        raise HTTPException(status_code=409, detail={"code": "not_approved",
                                                     "message": f"{entry_id} has no approval to revoke."})
    db.add(PlaceholderResolution(entry_id=entry_id, action=registry.REVOKED, resolved_by=employee.employee_id,
                                 resolved_at=datetime.now(timezone.utc), note=note))
    db.add(AuditLog(entity_type="placeholder", entity_id=entry_id, action="wording_revoked",
                    performed_by=employee.employee_id, before_value=before["approval"], after_value={"note": note}))
    db.commit()
    return _item(entry, registry.evaluate(entry, db), _approval(entry, db))
