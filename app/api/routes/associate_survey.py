"""The on-site site survey (S1) — associate side. The survey's fields are written
through the onboarding itself (POST/PATCH /api/v1/associate/onboardings); these routes
read where it stands, its live results, and take the two on-screen signatures.

Visibility as for the customer (app/auth/scoping.py): the owner, and read-only a
Preferred Associate whose territory holds the site. Only the owner signs and asks for
corrections (S1 Step 6)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.associate_onboarding import _associate, _error, _load
from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import require_onboarding_documents, require_onboarding_edit, require_onboarding_view
from app.memory.db import get_db
from app.memory.models import ProposalInputs
from app.onboarding import service
from app.survey import results, signatures, status

router = APIRouter(prefix="/api/v1/associate/onboardings")


@router.get("/{onboarding_id}/survey")
def get_survey(onboarding_id: int, db: Session = Depends(get_db),
               current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Which survey sections are complete (and what each still needs), the two
    signatures and the customer's declaration."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return status.summary(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id))


@router.get("/{onboarding_id}/survey/results")
def get_survey_results(onboarding_id: int, db: Session = Depends(get_db),
                       current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Sizes, the suggested size and its limiting factor, the indicative list price for
    both packages with the subsidy, and warning codes — on the saved data."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return results.compute(db, onboarding)


def read_signature(file: UploadFile) -> bytes:
    limit = signatures.max_bytes()
    try:
        document = uploads.read_document_sync(file, "signature", allowed=(signatures.PNG,), max_bytes=limit)
    except uploads.UploadRejected as exc:
        if exc.status_code == 413:
            raise HTTPException(status_code=413, detail={"code": "file_too_large", "max_kb": limit // 1024,
                                                         "message": f"The signature exceeds {limit // 1024} KB."})
        raise HTTPException(status_code=422, detail={"code": "signature_not_png", "field": "file",
                                                     "message": "The signature must be a PNG image."})
    return document.content


@router.post("/{onboarding_id}/survey/signatures/{party}", status_code=201)
def sign_survey(onboarding_id: int, party: Literal["customer", "associate"], file: UploadFile = File(...),
                declaration_name: Optional[str] = Form(default=None, max_length=200),
                customer_confirms_data: Optional[bool] = Form(default=None),
                db: Session = Depends(get_db),
                current: AuthenticatedAssociate = Depends(get_current_associate)):
    """One on-screen signature (multipart: `file`, a PNG). The customer's also needs
    `declaration_name` (the name the customer typed) and `customer_confirms_data=true`.
    Owner only; draft or submitted (a submitted customer returns to draft) — or a
    verified customer whose signature an applied survey correction cleared."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    name = (declaration_name or "").strip()
    if party == "customer":
        if not name:
            raise HTTPException(status_code=422, detail={"code": "declaration_name_required",
                                                         "field": "declaration_name",
                                                         "message": "Enter the customer's name as they declared it."})
        if customer_confirms_data is not True:
            raise HTTPException(status_code=422, detail={"code": "confirmation_required",
                                                         "field": "customer_confirms_data",
                                                         "message": "The customer must confirm the survey data."})
    content = read_signature(file)
    from app.storage import r2
    try:
        signatures.record(db, onboarding, party, content=content, performed_by=service.actor(associate),
                          declaration_name=name if party == "customer" else None)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    db.commit()
    return status.summary(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id))


# ── Step 7: the survey report ────────────────────────────────────────────────────

@router.get("/{onboarding_id}/survey/report")
def survey_report_link(onboarding_id: int, db: Session = Depends(get_db),
                       current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A short-lived signed link to the survey report — a document: the owner (a
    territory viewer only when territory_viewers_see_documents is on). Audited."""
    from app.storage import r2
    from app.survey import report

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_documents(associate, onboarding)
    try:
        data = report.link(db, onboarding, service.actor(associate))
    except AssociateError as exc:
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    db.commit()
    return data


# ── Step 6: corrections after verification ───────────────────────────────────────

class CorrectionRequest(BaseModel):
    changes: dict[str, Any]
    reason: str = Field(max_length=1000)


@router.post("/{onboarding_id}/survey/corrections", status_code=201)
def request_correction(onboarding_id: int, body: CorrectionRequest, db: Session = Depends(get_db),
                       current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Ask Grid Nova to change survey fields of a VERIFIED customer (draft and
    submitted customers are edited directly). Owner only; one pending request at a time."""
    from app.survey import corrections

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    try:
        request = corrections.create(db, onboarding, service.actor(associate), body.changes, body.reason)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return corrections.serialize(request)


@router.get("/{onboarding_id}/survey/corrections")
def list_corrections(onboarding_id: int, db: Session = Depends(get_db),
                     current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Every correction request for this customer, newest first."""
    from app.survey import corrections

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return {"items": [corrections.serialize(r) for r in corrections.for_onboarding(db, onboarding.id)]}
