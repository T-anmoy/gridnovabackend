"""Payments for associates — associate-authenticated. Only the OWNING associate sees or
records a customer's payments: a Preferred Associate never gets payment amounts for a
downline customer (their territory view shows stage progress only).

The associate records what the customer paid (never cash), attaches proof, and may
cancel a payment they recorded while it is still `recorded`. Grid Nova's Accounts team
verifies it; the receipt then goes to the customer.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import can_edit_onboarding, require_onboarding_view
from app.execution import config
from app.memory.db import get_db
from app.memory.models import Associate, CustomerOnboarding, Payment
from app.onboarding import service as onboarding_service
from app.payments import service as payments

router = APIRouter(prefix="/api/v1/associate")

MAX_PROOF_BYTES = 10 * 1024 * 1024
OWNER_ONLY = "Only the associate who onboarded this customer can see or record its payments."


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _owner(db: Session, current: AuthenticatedAssociate, onboarding_id: int) -> tuple[Associate, CustomerOnboarding]:
    associate = db.get(Associate, current.associate_pk)
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    require_onboarding_view(associate, onboarding)
    if not can_edit_onboarding(associate, onboarding):
        raise HTTPException(status_code=403, detail=OWNER_ONLY)
    return associate, onboarding


def _payment(db: Session, onboarding: CustomerOnboarding, payment_id: int) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None or payment.onboarding_id != onboarding.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


def overview(db: Session, onboarding: CustomerOnboarding) -> dict:
    """Milestones (with due status), payments and totals — amounts as strings."""
    rows = db.execute(select(Payment).where(Payment.onboarding_id == onboarding.id)
                      .order_by(Payment.recorded_at.desc(), Payment.id.desc())).scalars()
    return {"onboarding_id": onboarding.id, "totals": payments.totals(db, onboarding),
            "milestones": payments.milestone_status(db, onboarding),
            "payments": [payments.payment_json(db, p, staff=False) for p in rows],
            "enabled_modes": list(config.payments()["enabled_modes"]),
            "proof_required": bool(config.payments()["proof_required_for_associates"])}


@router.get("/payments/instructions")
def payment_instructions(db: Session = Depends(get_db),
                         current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Grid Nova's own account, for the "pay only here" reminder. Withheld while the
    account details are still placeholders — never show a customer a dummy account."""
    from app.payments.receipts import hold_reason

    company = config.payments()["receipt"]["company"]
    if hold_reason(db):
        return {"available": False, "reason": "placeholder_content", "account": None,
                "message_key": "payments.instructions.not_ready"}
    return {"available": True, "reason": None, "message_key": "payments.instructions.pay_only_here",
            "account": {"payee": company["legal_name"], "bank_name": company["bank_name"],
                        "account_number": company["account_number"], "ifsc": company["ifsc"]},
            "cash_accepted": False}


@router.get("/onboardings/{onboarding_id}/payments")
def list_payments(onboarding_id: int, db: Session = Depends(get_db),
                  current: AuthenticatedAssociate = Depends(get_current_associate)):
    _, onboarding = _owner(db, current, onboarding_id)
    payments.ensure_schedule(db, onboarding, "system")
    db.commit()
    return overview(db, onboarding)


class PaymentIn(BaseModel):
    amount: str | float | int
    mode: str = Field(max_length=20)
    reference: str = Field(max_length=100)
    bank_name: Optional[str] = Field(default=None, max_length=120)
    instrument_date: Optional[str] = Field(default=None, max_length=10)
    payer_name: Optional[str] = Field(default=None, max_length=200)
    milestone_id: Optional[int] = None


@router.post("/onboardings/{onboarding_id}/payments", status_code=201)
def record_payment(onboarding_id: int, body: PaymentIn, db: Session = Depends(get_db),
                   current: AuthenticatedAssociate = Depends(get_current_associate)):
    associate, onboarding = _owner(db, current, onboarding_id)
    try:
        payment = payments.record(db, onboarding, body.model_dump(), onboarding_service.actor(associate),
                                  by_associate=True)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {"payment": payments.payment_json(db, payment, staff=False), **overview(db, onboarding)}


def store_proof(db: Session, payment: Payment, file: UploadFile, actor: str) -> Payment:
    import uuid

    from app.storage import r2

    try:
        document = uploads.read_document_sync(file, "proof", allowed=uploads.DOCUMENT_TYPES,
                                              max_bytes=MAX_PROOF_BYTES)
    except uploads.UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    key = f"payments/{payment.project_id}/{payment.id}/{uuid.uuid4().hex}_{document.filename}"
    try:
        r2.upload_bytes(key, document.content, document.content_type)
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    try:
        payments.attach_proof(db, payment, key=key, filename=document.filename,
                              content_type=document.content_type, actor=actor)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return payment


@router.post("/onboardings/{onboarding_id}/payments/{payment_id}/proof")
def upload_proof(onboarding_id: int, payment_id: int, file: UploadFile = File(...),
                 db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A screenshot or scan (JPEG/PNG/WEBP/PDF, ≤ 10 MB) proving the payment."""
    associate, onboarding = _owner(db, current, onboarding_id)
    payment = _payment(db, onboarding, payment_id)
    store_proof(db, payment, file, onboarding_service.actor(associate))
    return payments.payment_json(db, payment, staff=False)


def proof_link(payment: Payment) -> dict:
    from app.storage import r2

    if not payment.proof_key:
        raise HTTPException(status_code=404, detail="No proof attached")
    try:
        url, expires_at = r2.signed_get_url(payment.proof_key, filename=payment.proof_filename)
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": payment.proof_filename}


@router.get("/onboardings/{onboarding_id}/payments/{payment_id}/proof")
def get_proof(onboarding_id: int, payment_id: int, db: Session = Depends(get_db),
              current: AuthenticatedAssociate = Depends(get_current_associate)):
    _, onboarding = _owner(db, current, onboarding_id)
    return proof_link(_payment(db, onboarding, payment_id))


class ReasonIn(BaseModel):
    reason: str = Field(max_length=2000)


@router.post("/onboardings/{onboarding_id}/payments/{payment_id}/cancel")
def cancel_payment(onboarding_id: int, payment_id: int, body: ReasonIn, db: Session = Depends(get_db),
                   current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Cancel a payment you recorded, while it is still `recorded`."""
    associate, onboarding = _owner(db, current, onboarding_id)
    _payment(db, onboarding, payment_id)
    try:
        payment = payments.cancel(db, payment_id, onboarding_service.actor(associate), body.reason,
                                  by_associate=True)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return payments.payment_json(db, payment, staff=False)
