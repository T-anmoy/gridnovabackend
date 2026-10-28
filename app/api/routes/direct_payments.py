"""Payments for a direct customer — a lead with no associate (BE-20; app/payments/direct.py).

The same shapes as the associate customer's staff routes (/dashboard/onboardings/{id}/
payments and /payment-schedule), keyed by lead. Acting on one payment — deposit, verify
(not by whoever recorded it), dishonour, cancel, proof, receipt resend — is the shared
/dashboard/payments/{payment_id}/… routes.

Who: Accounts and Admin record (payments.process; Sales does not record for a direct
customer). Reading — Accounts, Admin, Management (payments.view_all) and the lead's Sales
owner or Sales for an unassigned lead (payments.view on a lead they can open); the lead's
region applies (BE-19). Operations, Engineer, Network Manager and Proposal Desk: never.
A lead brought by an associate answers 409 associate_customer with its onboarding_id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes.associate_payments import PaymentIn
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import require_lead_region, require_payment_access
from app.memory.db import get_db
from app.memory.models import Lead, Payment
from app.payments import direct, receipts
from app.payments import service as payments

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def lead_for_payments(db: Session, employee: AuthenticatedEmployee, lead_id: int) -> Lead:
    """The lead, when the caller may see its payments (role, ownership, region)."""
    if not employee.can_view_payments():
        raise HTTPException(status_code=403, detail="Your role cannot view payments.")
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    require_payment_access(employee, lead)
    require_lead_region(db, employee, lead)
    return lead


def _direct_lead(db: Session, employee: AuthenticatedEmployee, lead_id: int) -> Lead:
    lead = lead_for_payments(db, employee, lead_id)
    try:
        direct.require_direct(db, lead)
    except AssociateError as exc:
        raise _error(exc)
    return lead


def _schedule(db: Session, lead: Lead) -> None:
    direct.ensure_schedule(db, lead, "system")
    db.commit()


@router.get("/leads/{lead_id}/payments")
def lead_payments(lead_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Milestones, payments (staff view, newest first), totals and the project summary —
    the shape of GET /onboardings/{id}/payments, with lead_id. Before acceptance the
    schedule is empty and the totals null."""
    lead = _direct_lead(db, employee, lead_id)
    _schedule(db, lead)
    return {**direct.overview(db, lead), "project": direct.project_summary(db, lead)}


@router.post("/leads/{lead_id}/payments", status_code=201)
def record_lead_payment(lead_id: int, body: PaymentIn, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts / Admin. 409 not_accepted before the customer accepted; 409
    exceeds_outstanding above the accepted total (cumulative, partial payments allowed);
    422 date_in_future, mode_not_enabled, reference_required, instrument_details_required."""
    if not employee.can_process_payments():
        raise HTTPException(status_code=403, detail="Only Accounts or Admin record a direct customer's payment.")
    lead = _direct_lead(db, employee, lead_id)
    try:
        payment = direct.record(db, lead, body.model_dump(), employee.employee_id)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {**payments.payment_json(db, payment), "project": payments.payment_project_summary(db, payment)}


@router.get("/leads/{lead_id}/payment-schedule")
def lead_schedule(lead_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The shape of GET /onboardings/{id}/payment-schedule, with lead_id."""
    lead = _direct_lead(db, employee, lead_id)
    _schedule(db, lead)
    return {"onboarding_id": None, "lead_id": lead.id, "totals": direct.totals(db, lead),
            "milestones": direct.milestone_status(db, lead)}


@router.get("/leads/{lead_id}/payments/{payment_id}/receipt")
def lead_receipt(lead_id: int, payment_id: int, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The receipt of one verified payment (staff view) with a short-lived link to its PDF
    (`pdf_url`, null while it is held or not rendered yet). The customer's own link is
    `url` (the public token route); it stays null while the receipt is held or void."""
    lead = _direct_lead(db, employee, lead_id)
    payment = db.get(Payment, payment_id)
    if payment is None or payment.lead_id != lead.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    receipt = receipts.for_payment(db, payment.id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="No receipt: the payment is not verified.")
    pdf_url = expires_at = None
    if receipt.pdf_key:
        from app.storage import r2
        try:
            pdf_url, expires = r2.signed_get_url(receipt.pdf_key, filename=f"{receipt.receipt_number}.pdf")
            expires_at = expires.isoformat()
        except r2.StorageNotConfiguredError:
            pdf_url = None
    return {"payment_id": payment.id, **receipts.receipt_json(receipt, staff=True), "pdf_url": pdf_url,
            "pdf_url_expires_at": expires_at}
