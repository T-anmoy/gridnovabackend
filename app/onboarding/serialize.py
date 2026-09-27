"""Response shapes for onboarded customers — one for associates, a fuller one for staff."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.memory.memory_manager import is_placeholder_email
from app.memory.models import (
    Associate, Customer, CustomerOnboarding, Project, ProposalInputs, TerritoryBlock, TerritoryDistrict,
    TerritoryState,
)
from app.onboarding import formal, scoring, service

_INPUT_FIELDS = tuple(f for f in service.INPUT_FIELDS)


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _val(v):
    return v.value if hasattr(v, "value") else v


def _territory(db: Session, o: CustomerOnboarding) -> dict:
    def one(model, pk):
        if pk is None:
            return None
        row = db.get(model, pk)
        return {"lgd_code": row.lgd_code, "name": row.name}
    return {"state": one(TerritoryState, o.state_id), "district": one(TerritoryDistrict, o.district_id),
            "block": one(TerritoryBlock, o.block_id)}


def _files(inputs: ProposalInputs) -> tuple[list, list]:
    from app.api.routes.proposal_inputs import uploaded_files

    documents, photos = [], []
    for f in uploaded_files(inputs):
        ref = next((r for r in (inputs.document_refs or []) if isinstance(r, dict)
                    and r.get("storage_key") == f["storage_key"]), None) \
            or (inputs.installation_photo_refs or {}).get(f.get("photo_slot") or "", {})
        item = {"file_ref": f["file_ref"], "filename": f["filename"], "received_at": ref.get("received_at"),
                "latitude": ref.get("latitude"), "longitude": ref.get("longitude"),
                "client_captured_at": ref.get("client_captured_at")}
        if f["target"] == "document":
            documents.append({"document_type": f.get("document_type"), **item})
        elif f["target"] == "installation_photo":
            photos.append({"photo_slot": f.get("photo_slot"), **item})
    return documents, photos


def onboarding(db: Session, o: CustomerOnboarding, *, viewer_is_owner: Optional[bool] = None,
               staff: bool = False) -> dict:
    inputs = db.get(ProposalInputs, o.proposal_inputs_id)
    customer = db.get(Customer, o.customer_id)
    owner = db.get(Associate, o.owner_associate_id)
    documents, photos = _files(inputs)
    flags = service.open_flags(db, o.id)
    data = {
        "onboarding_id": o.id,
        "status": o.status.value,
        "lead_id": o.lead_id,
        "project_reference_id": db.get(Project, o.project_id).reference_id,
        "owner": {"associate_id": owner.associate_id, "full_name": owner.full_name,
                  "associate_type": owner.associate_type.value},
        "customer": {"name": customer.name, "mobile": o.customer_mobile,
                     "email": None if is_placeholder_email(customer.email) else customer.email},
        "territory": _territory(db, o),
        "site_latitude": o.site_latitude,
        "site_longitude": o.site_longitude,
        "connection_in_customer_name": o.connection_in_customer_name,
        "property_in_customer_name": o.property_in_customer_name,
        "customer_timeline": _val(o.customer_timeline),
        "site_assessment": {"proposal_inputs_id": inputs.id, "status": inputs.status.value,
                            **{f: _val(getattr(inputs, f)) for f in _INPUT_FIELDS}},
        "documents": documents,
        "photos": photos,
        "checklist": service.checklist(db, o),
        "notice": {"version": o.notice_version, "acknowledged_at": _iso(o.notice_acknowledged_at),
                   "recorded_by": o.notice_recorded_by},
        "submitted_at": _iso(o.submitted_at),
        "verification": {"completed": o.verification_call_completed, "verified_at": _iso(o.verified_at)},
        "attribution": {"locked_until": _iso(o.attribution_locked_until), "under_review": bool(flags)},
        "closed_reason": o.closed_reason if staff else None,
        "score": scoring.serialize(o),
        # The formal proposal was requested but cannot be prepared yet — and why.
        "formal_proposal_blocked": formal.blocked(db, o),
        "warnings": service.warnings_for(inputs, db.get(TerritoryDistrict, o.district_id).lgd_code),
        "created_at": _iso(o.created_at),
        "updated_at": _iso(o.updated_at),
    }
    if viewer_is_owner is not None:
        data["can_edit"] = bool(viewer_is_owner) and o.status in service.EDITABLE_STATUSES
    if staff:
        data["verification"].update(note=o.verification_call_note, verified_by_employee_id=o.verified_by_employee_id)
        data["attribution"]["open_flags"] = [flag(f) for f in flags]
    return data


def flag(f) -> dict:
    return {"flag_id": f.id, "onboarding_id": f.onboarding_id, "existing_onboarding_id": f.existing_onboarding_id,
            "matched_on": f.matched_on, "existing_was_locked": f.existing_was_locked, "status": f.status.value,
            "resolution": _val(f.resolution), "resolution_note": f.resolution_note,
            "resolved_by_employee_id": f.resolved_by_employee_id, "resolved_at": _iso(f.resolved_at),
            "created_at": _iso(f.created_at)}
