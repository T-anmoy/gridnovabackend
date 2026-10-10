"""The on-site site survey (S1) — staff side. Readers: Admin, Management, Sales (own or
unassigned leads) and Engineer. Engineers never receive the indicative prices."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes.onboarding_admin import _load
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import ProposalInputs
from app.survey import results, status

router = APIRouter(prefix="/api/v1/dashboard")


def _require_reader(employee: AuthenticatedEmployee) -> None:
    if not employee.can_view_surveys():
        raise HTTPException(status_code=403, detail="Your role cannot view site surveys.")


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
