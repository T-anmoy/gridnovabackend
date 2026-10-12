"""The on-site site survey (S1) — staff side. Readers: Admin, Management, Sales (own or
unassigned leads) and Engineer. Engineers never receive the indicative prices.

The feasibility review (Step 5), deciding survey corrections (Step 6) and generating the
survey report (Step 7) are Engineer and Admin only; clearing a proposal's re-verification flag is Admin, Management and
Engineer — never the person who applied the correction behind it."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.api.routes.onboarding_admin import _error, _load
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import lead_scope, require_lead_access
from app.memory.db import get_db
from app.memory.models import (
    CustomerOnboarding, EmployeeRole, FeasibilityOutcome, Lead, Proposal, ProposalInputs, SurveyCorrectionRequest,
    SurveyCorrectionStatus,
)
from app.survey import results, status

router = APIRouter(prefix="/api/v1/dashboard")


def _require_reader(employee: AuthenticatedEmployee) -> None:
    if not employee.can_view_surveys():
        raise HTTPException(status_code=403, detail="Your role cannot view site surveys.")


def _require_reviewer(employee: AuthenticatedEmployee) -> None:
    if not employee.can_review_surveys():
        raise HTTPException(status_code=403, detail="Only an Engineer or Admin can do this.")


@router.get("/onboardings/{onboarding_id}/survey")
def get_survey(onboarding_id: int, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require_reader(employee)
    onboarding = _load(db, employee, onboarding_id)
    return status.summary(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id), staff=True)


@router.get("/onboardings/{onboarding_id}/survey/results")
def get_survey_results(onboarding_id: int, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require_reader(employee)
    onboarding = _load(db, employee, onboarding_id)
    return results.compute(db, onboarding, include_prices=not employee.is_engineer)


# ── Step 5: the Engineer's feasibility review ────────────────────────────────────

class RevisitItem(BaseModel):
    section: str = Field(max_length=40)
    field: Optional[str] = Field(default=None, max_length=60)
    note: str = Field(max_length=500)


class SurveyReviewRequest(BaseModel):
    outcome: FeasibilityOutcome
    notes: Optional[str] = Field(default=None, max_length=2000)
    conditions: Optional[list[str]] = Field(default=None, max_length=20)
    revisit_items: Optional[list[RevisitItem]] = Field(default=None, max_length=20)


@router.post("/onboardings/{onboarding_id}/survey/review")
def review_survey(onboarding_id: int, body: SurveyReviewRequest, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Record (or replace) the feasibility review of a complete, signed survey."""
    from app.survey import review

    _require_reviewer(employee)
    onboarding = _load(db, employee, onboarding_id)
    if any(len(c) > 500 for c in body.conditions or []):
        raise HTTPException(status_code=422, detail={"code": "too_long", "field": "conditions",
                                                     "message": "Each condition must be 500 characters or fewer."})
    try:
        review.record(db, onboarding, employee.employee_id, outcome=body.outcome, notes=body.notes,
                      conditions=body.conditions,
                      revisit_items=[i.model_dump() for i in body.revisit_items or []])
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return status.summary(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id), staff=True)


# ── Step 7: the survey report PDF ────────────────────────────────────────────────

def _storage_off(exc) -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})


@router.post("/onboardings/{onboarding_id}/survey/report")
def generate_survey_report(onboarding_id: int, db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Render and store the survey report (after the review). A new call stores a new
    file; the earlier one is kept."""
    from app.storage import r2
    from app.survey import report

    _require_reviewer(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        inputs = report.generate(db, onboarding, employee.employee_id)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        db.rollback()
        raise _storage_off(exc)
    db.commit()
    return {"onboarding_id": onboarding.id, "generated_at": inputs.survey_report_generated_at.isoformat(),
            "filename": report.filename(db, onboarding)}


@router.get("/onboardings/{onboarding_id}/survey/report")
def survey_report_link(onboarding_id: int, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A short-lived signed link to the latest survey report. Every link is audited."""
    from app.storage import r2
    from app.survey import report

    _require_reader(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        data = report.link(db, onboarding, employee.employee_id)
    except AssociateError as exc:
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        raise _storage_off(exc)
    db.commit()
    return data


# ── Step 6: corrections and re-verification ───────────────────────────────────────

@router.get("/survey-corrections")
def correction_queue(status_filter: Optional[SurveyCorrectionStatus] = Query(default=None, alias="status"),
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                     db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Correction requests, newest first. Sales sees its own or unassigned leads only."""
    from app.survey import corrections

    _require_reader(employee)
    q = (select(SurveyCorrectionRequest, CustomerOnboarding)
         .join(CustomerOnboarding, CustomerOnboarding.id == SurveyCorrectionRequest.onboarding_id))
    if employee.role == EmployeeRole.sales:
        q = q.join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    if status_filter is not None:
        q = q.where(SurveyCorrectionRequest.status == status_filter)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(SurveyCorrectionRequest.created_at.desc(), SurveyCorrectionRequest.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    from app.onboarding import service
    from app.memory.models import Customer
    items = []
    for request, onboarding in rows:
        customer = db.get(Customer, onboarding.customer_id)
        items.append({**corrections.serialize(request, staff=True),
                      "client_code": service.project_reference(db, onboarding),
                      "customer_name": customer.name if customer else None})
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _correction_onboarding(db: Session, employee: AuthenticatedEmployee, correction_id: int) -> None:
    request = db.get(SurveyCorrectionRequest, correction_id)
    if request is None:
        raise HTTPException(status_code=404, detail={"code": "correction_not_found",
                                                     "message": "Correction not found."})
    _load(db, employee, request.onboarding_id)


class CorrectionDecision(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/survey-corrections/{correction_id}/apply")
def apply_correction(correction_id: int, body: CorrectionDecision = CorrectionDecision(),
                     db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Write the requested survey change. Open proposals touched by it are flagged for
    re-verification (`flagged_proposal_ids`)."""
    from app.survey import corrections

    _require_reviewer(employee)
    _correction_onboarding(db, employee, correction_id)
    try:
        result = corrections.apply(db, correction_id, employee.employee_id, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


@router.post("/survey-corrections/{correction_id}/reject")
def reject_correction(correction_id: int, body: CorrectionDecision, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Refuse the change (`note` required); nothing on the survey changes."""
    from app.survey import corrections

    _require_reviewer(employee)
    _correction_onboarding(db, employee, correction_id)
    try:
        result = corrections.reject(db, correction_id, employee.employee_id, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


class ClearReverification(BaseModel):
    note: str = Field(max_length=2000)


@router.post("/proposals/{proposal_id}/reverification/clear")
def clear_reverification(proposal_id: int, body: ClearReverification, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Confirm a flagged proposal still fits the changed survey, so it can be sent or
    accepted again."""
    from app.memory.memory_manager import MemoryManager
    from app.survey import changes

    if not employee.can_clear_reverification():
        raise HTTPException(status_code=403, detail="Your role cannot clear a proposal's re-verification.")
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    require_lead_access(employee, MemoryManager(db).get_lead(proposal.lead_id),
                        "This proposal is not assigned to you.")
    try:
        proposal = changes.clear(db, proposal, employee.employee_id, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {"proposal_id": proposal.id, **changes.serialize(proposal),
            "reverification_cleared_by": proposal.reverification_cleared_by,
            "reverification_cleared_at": proposal.reverification_cleared_at.isoformat()}


# ── Step 8: the review queue and its overdue alert ───────────────────────────────

@router.get("/survey-reviews")
def survey_review_queue(state: Literal["awaiting", "overdue", "reviewed", "revised"] = "awaiting",
                        page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                        db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Complete, signed surveys of submitted or verified customers, newest wait first
    (app/survey/queue.py). Sales sees its own or unassigned leads only."""
    from app.survey import queue

    _require_reader(employee)
    rows = [r for r in queue.rows(db, employee) if queue.matches(r, state)]
    start = (page - 1) * page_size
    return {"items": [queue.public(r) for r in rows[start:start + page_size]], "state": state,
            "review_target_hours": queue.target_hours(), "page": page, "page_size": page_size, "total": len(rows)}


@router.post("/internal/check-survey-reviews")
def run_survey_review_alerts(db: Session = Depends(get_db),
                             _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY. Emails staff once about each survey that has waited
    longer than review_target_hours for its review (not again for the same wait).
    Idempotent, also when two runs overlap."""
    from datetime import datetime, timezone

    from app.jobs.locks import run_exclusive
    from app.jobs.survey_reviews import check_survey_reviews
    return run_exclusive(db, "check-survey-reviews", lambda: check_survey_reviews(db, datetime.now(timezone.utc)))
