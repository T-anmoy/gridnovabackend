"""Payments for staff — the Accounts queue, one payment, verifying/depositing/
dishonouring/cancelling, the per-project schedule, receipts and exports.

Who sees what: app/auth/scoping.py require_payment_access (Admin, Management, Accounts:
all; Sales: own or unassigned leads; Operations, Engineer: none). Who acts:
can_record_payment (Admin, Accounts, Sales) and can_process_payments (Admin, Accounts).
Amounts are decimal strings.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.associate_payments import PaymentIn, ReasonIn, proof_link, store_proof
from app.api.routes.dashboard import _require_admin_or_internal_key
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import lead_scope, require_payment_access
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    CustomerOnboarding, EmployeeRole, Lead, Payment, PaymentMode, PaymentStatus,
)
from app.payments import receipts
from app.payments import service as payments

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _onboarding(db: Session, employee: AuthenticatedEmployee, onboarding_id: int) -> CustomerOnboarding:
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    require_payment_access(employee, MemoryManager(db).get_lead(onboarding.lead_id))
    return onboarding


def _payment(db: Session, employee: AuthenticatedEmployee, payment_id: int) -> Payment:
    if not employee.can_view_payments():
        raise HTTPException(status_code=403, detail="Your role cannot view payments.")
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.onboarding_id is not None:
        _onboarding(db, employee, payment.onboarding_id)
    elif employee.role == EmployeeRole.sales:   # no lead to scope it by: not Sales' to see
        raise HTTPException(status_code=403, detail="Your role cannot view this payment.")
    return payment


def _processor(employee: AuthenticatedEmployee) -> None:
    if not employee.can_process_payments():
        raise HTTPException(status_code=403, detail="Only Accounts or Admin can do this.")


def _detail(db: Session, payment: Payment) -> dict:
    return {**payments.payment_json(db, payment), "project": payments.payment_project_summary(db, payment)}


def _act(db: Session, fn):
    try:
        result = fn()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


# ── Queue ────────────────────────────────────────────────────────────────────────

def _visible(db: Session, employee: AuthenticatedEmployee, q):
    if employee.role == EmployeeRole.sales:
        q = q.join(CustomerOnboarding, CustomerOnboarding.id == Payment.onboarding_id) \
             .join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    return q


@router.get("/payments")
def list_payments(status: Optional[PaymentStatus] = None, mode: Optional[PaymentMode] = None,
                  needs_attention: Optional[bool] = None, onboarding_id: Optional[int] = None,
                  date_from: Optional[date] = None, date_to: Optional[date] = None,
                  page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                  db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """`needs_attention=true`: awaiting verification past the threshold, a cheque past
    its hold, proof missing, or a receipt held / not emailed / failed.
    date_from/date_to filter on recorded_at (inclusive, UTC dates)."""
    if not employee.can_view_payments():
        raise HTTPException(status_code=403, detail="Your role cannot view payments.")
    q = _visible(db, employee, select(Payment))
    if status:
        q = q.where(Payment.status == status)
    if mode:
        q = q.where(Payment.mode == mode)
    if onboarding_id:
        q = q.where(Payment.onboarding_id == onboarding_id)
    if date_from:
        q = q.where(Payment.recorded_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        q = q.where(Payment.recorded_at <= datetime.combine(date_to, time.max, tzinfo=timezone.utc))
    rows = list(db.execute(q.order_by(Payment.recorded_at.desc(), Payment.id.desc())).scalars())
    items = [payments.payment_json(db, p) for p in rows]
    if needs_attention is not None:
        items = [i for i in items if i["needs_attention"] == needs_attention]
    return {"items": items[(page - 1) * page_size: page * page_size], "page": page, "page_size": page_size,
            "total": len(items)}


@router.get("/payment-milestones")
def list_milestones(needs_attention: Optional[bool] = None, page: int = Query(default=1, ge=1),
                    page_size: int = Query(default=25, ge=1, le=100), db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Every scheduled milestone with its computed due status. `needs_attention=true`:
    due and unpaid past payments.thresholds.milestone_overdue_days."""
    if not employee.can_view_payments():
        raise HTTPException(status_code=403, detail="Your role cannot view payments.")
    from app.memory.models import PaymentMilestone
    q = select(CustomerOnboarding).where(CustomerOnboarding.id.in_(select(PaymentMilestone.onboarding_id)))
    if employee.role == EmployeeRole.sales:
        q = q.join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    items = []
    for onboarding in db.execute(q.order_by(CustomerOnboarding.id)).scalars():
        for m in payments.milestone_status(db, onboarding):
            items.append({**m, "onboarding_id": onboarding.id, "needs_attention": m["overdue"]})
    if needs_attention is not None:
        items = [i for i in items if i["needs_attention"] == needs_attention]
    return {"items": items[(page - 1) * page_size: page * page_size], "page": page, "page_size": page_size,
            "total": len(items)}


@router.get("/payments/{payment_id}")
def get_payment(payment_id: int, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    return _detail(db, _payment(db, employee, payment_id))


@router.get("/payments/{payment_id}/proof")
def get_proof(payment_id: int, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    return proof_link(_payment(db, employee, payment_id))


@router.post("/payments/{payment_id}/proof")
async def upload_proof(payment_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.can_record_payment():
        raise HTTPException(status_code=403, detail="Your role cannot record payments.")
    payment = _payment(db, employee, payment_id)
    await store_proof(db, payment, file, employee.employee_id)
    return _detail(db, payment)


@router.post("/payments/{payment_id}/deposit")
def deposit(payment_id: int, db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _processor(employee)
    _payment(db, employee, payment_id)
    return _detail(db, _act(db, lambda: payments.deposit(db, payment_id, employee.employee_id)))


class VerifyIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/payments/{payment_id}/verify")
def verify(payment_id: int, body: Optional[VerifyIn] = None, db: Session = Depends(get_db),
           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Funds confirmed in Grid Nova's bank. Writes the ledger credit; the receipt is
    numbered now and rendered/emailed after the commit. Not by whoever recorded the
    payment (409 self_verification) — except an Admin, who must give `reason`
    (audited as segregation_override)."""
    _processor(employee)
    _payment(db, employee, payment_id)
    return _detail(db, _act(db, lambda: payments.verify(db, payment_id, employee.employee_id,
                                                        is_admin=employee.is_admin,
                                                        reason=body.reason if body else None)))


@router.post("/payments/{payment_id}/dishonour")
def dishonour(payment_id: int, body: ReasonIn, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _processor(employee)
    _payment(db, employee, payment_id)
    return _detail(db, _act(db, lambda: payments.dishonour(db, payment_id, employee.employee_id, body.reason)))


@router.post("/payments/{payment_id}/cancel")
def cancel(payment_id: int, body: ReasonIn, db: Session = Depends(get_db),
           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _processor(employee)
    _payment(db, employee, payment_id)
    return _detail(db, _act(db, lambda: payments.cancel(db, payment_id, employee.employee_id, body.reason,
                                                         by_associate=False)))


@router.post("/payments/{payment_id}/receipt/resend")
def resend_receipt(payment_id: int, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Re-render if needed and email the receipt again (at most once per 10 minutes)."""
    _processor(employee)
    payment = _payment(db, employee, payment_id)
    _act(db, lambda: receipts.resend(db, payment_id, employee.employee_id))
    return _detail(db, payment)


@router.post("/payment-receipts/release-held")
def release_held(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Send every held receipt once its placeholder content has been resolved."""
    _processor(employee)
    return receipts.release_held(db, employee.employee_id)


# ── One customer ─────────────────────────────────────────────────────────────────

@router.get("/onboardings/{onboarding_id}/payments")
def customer_payments(onboarding_id: int, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.api.routes.associate_payments import overview

    onboarding = _onboarding(db, employee, onboarding_id)
    payments.ensure_schedule(db, onboarding, "system")
    db.commit()
    body = overview(db, onboarding)
    body["payments"] = [payments.payment_json(db, db.get(Payment, p["payment_id"])) for p in body["payments"]]
    return {**body, "project": payments.project_summary(db, onboarding)}


@router.post("/onboardings/{onboarding_id}/payments", status_code=201)
def record_payment(onboarding_id: int, body: PaymentIn, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.can_record_payment():
        raise HTTPException(status_code=403, detail="Your role cannot record payments.")
    onboarding = _onboarding(db, employee, onboarding_id)
    payment = _act(db, lambda: payments.record(db, onboarding, body.model_dump(), employee.employee_id,
                                               by_associate=False))
    return _detail(db, payment)


@router.get("/onboardings/{onboarding_id}/payment-schedule")
def get_schedule(onboarding_id: int, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    onboarding = _onboarding(db, employee, onboarding_id)
    payments.ensure_schedule(db, onboarding, "system")
    db.commit()
    return {"onboarding_id": onboarding.id, "totals": payments.totals(db, onboarding),
            "milestones": payments.milestone_status(db, onboarding)}


class MilestoneIn(BaseModel):
    label: str = Field(max_length=200)
    percent: str | float | int
    trigger: str = Field(max_length=40)


class ScheduleIn(BaseModel):
    milestones: list[MilestoneIn]


@router.put("/onboardings/{onboarding_id}/payment-schedule")
def put_schedule(onboarding_id: int, body: ScheduleIn, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _processor(employee)
    onboarding = _onboarding(db, employee, onboarding_id)
    _act(db, lambda: payments.replace_schedule(db, onboarding, [m.model_dump() for m in body.milestones],
                                               employee.employee_id))
    return {"onboarding_id": onboarding.id, "totals": payments.totals(db, onboarding),
            "milestones": payments.milestone_status(db, onboarding)}


# ── Alerts (Cloud Scheduler) ─────────────────────────────────────────────────────

@router.post("/internal/check-payment-alerts")
def run_payment_alerts(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY. Emails Accounts once per overdue milestone and once
    per payment left unverified past the threshold. Idempotent."""
    from app.jobs.payment_alerts import check_payment_alerts
    return check_payment_alerts(db, datetime.now(timezone.utc))


# ── CSV exports (Accounts, Admin) ────────────────────────────────────────────────

def _export(name: str, body: str):
    from fastapi.responses import Response
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'})


@router.get("/exports/payments.csv")
def export_payments(date_from: Optional[date] = None, date_to: Optional[date] = None, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.payments import exports
    _processor(employee)
    return _export("payments", exports.payments_csv(db, date_from, date_to))


@router.get("/exports/receipts.csv")
def export_receipts(date_from: Optional[date] = None, date_to: Optional[date] = None, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.payments import exports
    _processor(employee)
    return _export("receipts", exports.receipts_csv(db, date_from, date_to))


@router.get("/exports/ledger.csv")
def export_ledger(date_from: Optional[date] = None, date_to: Optional[date] = None, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.payments import exports
    _processor(employee)
    return _export("ledger", exports.ledger_csv(db, date_from, date_to))


# ── Work queues (nav badge counts) ───────────────────────────────────────────────

@router.get("/work-queues")
def work_queues(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Counts for the dashboard's navigation badges, scoped to the caller's role: an
    area the role cannot see is absent (not zero). Sales counts only its own or
    unassigned customers."""
    from app.memory.models import PaymentReceipt, ReceiptEmailStatus

    out: dict = {}
    if employee.can_view_execution():
        from app.api.routes.execution_admin import queue_rows
        rows = queue_rows(db, employee)
        out["execution"] = {
            "pending_stage_verifications": sum(1 for r in rows if r["pending_submission"]),
            "stuck_projects": sum(1 for r in rows if r["is_stuck"]),
            "dcr_mismatch": sum(1 for r in rows if "dcr_mismatch" in r["flags"]),
        }
    if employee.can_view_payments():
        now = datetime.now(timezone.utc)
        visible = list(db.execute(_visible(db, employee, select(Payment))).scalars())
        ids = {p.id for p in visible}
        receipts_rows = list(db.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id.in_(ids))).scalars()) \
            if ids else []
        overdue = 0
        for onboarding_id in {p.onboarding_id for p in visible} | _scheduled_onboardings(db, employee):
            overdue += sum(1 for m in payments.milestone_status(db, db.get(CustomerOnboarding, onboarding_id), now)
                           if m["overdue"])
        out["payments"] = {
            "awaiting_verification": sum(1 for p in visible if "awaiting_verification" in payments.attention(p, now)),
            "dishonoured": sum(1 for p in visible if p.status == PaymentStatus.dishonoured),
            "receipts_held": sum(1 for r in receipts_rows if r.email_status == ReceiptEmailStatus.held),
            "receipts_without_email": sum(1 for r in receipts_rows if r.email_status == ReceiptEmailStatus.skipped),
            "overdue_milestones": overdue,
        }
    return out


def _scheduled_onboardings(db: Session, employee: AuthenticatedEmployee) -> set[int]:
    from app.memory.models import PaymentMilestone
    q = select(CustomerOnboarding.id).where(CustomerOnboarding.id.in_(select(PaymentMilestone.onboarding_id)))
    if employee.role == EmployeeRole.sales:
        q = q.join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    return set(db.execute(q).scalars())
