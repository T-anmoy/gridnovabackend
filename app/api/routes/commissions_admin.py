"""Commissions, rate schedules, payout batches and exports for staff (BE-4).

Who: can_view_commissions (Admin, Management, Accounts) reads; can_process_commissions
(Admin, Accounts) approves entries, prepares/approves/pays batches and exports;
can_manage_commission_rates (Admin) publishes schedules, re-rates and reverses entries;
the payout bank file (full account numbers) is Accounts only. Sales, Engineer and
Operations: none. Amounts are decimal strings.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.commissions import payouts, periods, rates, serialize
from app.commissions import service as commissions
from app.memory.db import get_db
from app.memory.models import (
    Associate, CommissionEntry, CommissionEntryKind, CommissionEntryStatus, CommissionQuery, CommissionQueryStatus,
    CommissionRateSchedule, CommissionRole, PayoutBatch, PayoutBatchStatus,
)

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _act(db: Session, fn):
    try:
        result = fn()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


def _reader(employee: AuthenticatedEmployee) -> None:
    if not employee.can_view_commissions():
        raise HTTPException(status_code=403, detail="Your role cannot view commissions.")


def _processor(employee: AuthenticatedEmployee) -> None:
    if not employee.can_process_commissions():
        raise HTTPException(status_code=403, detail="Only Accounts or Admin can do this.")


def _rates_admin(employee: AuthenticatedEmployee) -> None:
    if not employee.can_manage_commission_rates():
        raise HTTPException(status_code=403, detail="Only Admin can do this.")


# ── Entries ──────────────────────────────────────────────────────────────────────

def _day_start(d: date) -> datetime:
    return periods.start_utc(d)


def _filtered(q, *, associate_pk, status, kind, earning_role, pending, onboarding_id, payment_id, batch_id,
              date_from, date_to):
    if associate_pk is not None:
        q = q.where(CommissionEntry.associate_id == associate_pk)
    if status:
        q = q.where(CommissionEntry.status == status)
    if kind:
        q = q.where(CommissionEntry.kind == kind)
    if earning_role:
        q = q.where(CommissionEntry.earning_role == earning_role)
    if pending is not None:
        q = q.where(CommissionEntry.amount.is_(None) if pending else CommissionEntry.amount.is_not(None))
    if onboarding_id is not None:
        q = q.where(CommissionEntry.onboarding_id == onboarding_id)
    if payment_id is not None:
        q = q.where(CommissionEntry.payment_id == payment_id)
    if batch_id is not None:
        q = q.where(CommissionEntry.batch_id == batch_id)
    if date_from:
        q = q.where(CommissionEntry.created_at >= _day_start(date_from))
    if date_to:
        q = q.where(CommissionEntry.created_at < _day_start(date_to + timedelta(days=1)))
    return q


@router.get("/commissions")
def list_commissions(associate_id: Optional[str] = None, status: Optional[CommissionEntryStatus] = None,
                     kind: Optional[CommissionEntryKind] = None, earning_role: Optional[CommissionRole] = None,
                     pending: Optional[bool] = None, onboarding_id: Optional[int] = None,
                     payment_id: Optional[int] = None, batch_id: Optional[int] = None,
                     date_from: Optional[date] = None, date_to: Optional[date] = None,
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
                     db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The commission ledger, newest first. Dates are IST days on created_at."""
    _reader(employee)
    associate_pk = None
    if associate_id:
        associate_pk = db.execute(select(Associate.id).where(Associate.associate_id == associate_id)).scalar()
        if associate_pk is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "totals": _totals_json({})}
    filters = dict(associate_pk=associate_pk, status=status, kind=kind, earning_role=earning_role, pending=pending,
                   onboarding_id=onboarding_id, payment_id=payment_id, batch_id=batch_id, date_from=date_from,
                   date_to=date_to)
    q = _filtered(select(CommissionEntry), **filters)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = list(db.execute(q.order_by(CommissionEntry.created_at.desc(), CommissionEntry.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)).scalars())
    sums = dict(db.execute(_filtered(select(CommissionEntry.status, func.sum(CommissionEntry.amount)),
                                     **filters).group_by(CommissionEntry.status)).all())
    pending_count = db.execute(_filtered(select(func.count()).select_from(CommissionEntry), **filters)
                               .where(CommissionEntry.amount.is_(None),
                                      CommissionEntry.status != CommissionEntryStatus.reversed)).scalar_one()
    return {"items": serialize.entries_json(db, rows, staff=True), "page": page, "page_size": page_size,
            "total": total, "totals": {**_totals_json(sums), "pending_count": pending_count}}


def _totals_json(sums: dict) -> dict:
    from decimal import Decimal
    out = {k: serialize.s(Decimal(sums.get(CommissionEntryStatus(k)) or 0)) for k in ("earned", "approved", "paid")}
    out.setdefault("pending_count", 0)
    return out


class ApproveEntriesIn(BaseModel):
    entry_ids: list[int] = Field(min_length=1, max_length=commissions.MAX_BULK)


@router.post("/commissions/approve")
def approve_commissions(body: ApproveEntriesIn, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """earned → approved, all or nothing (409 entries_not_approvable lists why each
    refused entry cannot be approved)."""
    _processor(employee)
    rows = _act(db, lambda: commissions.approve(db, body.entry_ids, employee.employee_id))
    return {"approved": len(rows), "items": serialize.entries_json(db, rows, staff=True)}


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/commissions/{entry_id}/reverse")
def reverse_commission(entry_id: int, body: ReasonIn, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Admin (policy 4.7): reverse commission obtained through false information or a
    breach of the Network Terms. Not yet paid out → a reversal; already in an
    approved or paid batch → a recovery from the next payouts."""
    _rates_admin(employee)
    counter = _act(db, lambda: commissions.reverse_entry(db, entry_id, employee.employee_id, body.reason))
    return {"entry": serialize.entries_json(db, [counter], staff=True)[0]}


# ── Rate schedules ───────────────────────────────────────────────────────────────

@router.get("/commission-rate-schedules")
def list_rate_schedules(db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Every published version, newest first, and which is in force today."""
    _reader(employee)
    today = periods.today_ist()
    rows = list(db.execute(select(CommissionRateSchedule).order_by(CommissionRateSchedule.version.desc())).scalars())
    current = rates.in_force(db, today)
    return {"items": [rates.schedule_json(r, rates.rates_of(db, r.id), today) for r in rows],
            "in_force_schedule_id": current.id if current else None,
            "rate_keys": list(rates.RATE_KEYS),
            "earliest_effective_from": rates.earliest_effective_from(today, False).isoformat()}


class ScheduleIn(BaseModel):
    effective_from: date
    is_placeholder: bool = False
    rates: dict[str, str]
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/commission-rate-schedules", status_code=201)
def publish_rate_schedule(body: ScheduleIn, db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Admin: publish a new version (never an edit). Every rate key is required."""
    _rates_admin(employee)
    today = periods.today_ist()
    schedule = _act(db, lambda: rates.publish(db, effective_from=body.effective_from,
                                              is_placeholder=body.is_placeholder, rates=body.rates,
                                              note=body.note, actor=employee.employee_id, today=today))
    return rates.schedule_json(schedule, rates.rates_of(db, schedule.id), today)


@router.post("/commission-rate-schedules/{schedule_id}/re-rate")
def re_rate(schedule_id: int, body: ReasonIn, db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Admin, audited: compute every entry still waiting for confirmed rates — at the
    schedule in force on its agreement date where that is a real schedule, otherwise
    at this (real) schedule."""
    _rates_admin(employee)
    return _act(db, lambda: commissions.re_rate(db, schedule_id, employee.employee_id, body.reason))


# ── Payout batches ───────────────────────────────────────────────────────────────

@router.get("/payout-batches")
def list_payout_batches(status: Optional[PayoutBatchStatus] = None, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _reader(employee)
    q = select(PayoutBatch)
    if status:
        q = q.where(PayoutBatch.status == status)
    rows = list(db.execute(q.order_by(PayoutBatch.period.desc(), PayoutBatch.id.desc())).scalars())
    return {"items": [payouts.batch_json(db, b, with_lines=False) for b in rows]}


class BatchIn(BaseModel):
    period: str = Field(min_length=7, max_length=7)


@router.post("/payout-batches", status_code=201)
def create_payout_batch(body: BatchIn, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts/Admin: prepare the batch for a month that has ended."""
    _processor(employee)
    batch = _act(db, lambda: payouts.create(db, body.period, employee.employee_id, periods.today_ist()))
    return payouts.batch_json(db, batch, with_lines=True)


def _batch(db: Session, batch_id: int) -> PayoutBatch:
    batch = db.get(PayoutBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Payout batch not found")
    return batch


@router.get("/payout-batches/{batch_id}")
def get_payout_batch(batch_id: int, db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _reader(employee)
    return payouts.batch_json(db, _batch(db, batch_id), with_lines=True)


class ApproveBatchIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/payout-batches/{batch_id}/approve")
def approve_payout_batch(batch_id: int, body: Optional[ApproveBatchIn] = None, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts/Admin, never the preparer (an Admin may, with a reason)."""
    _processor(employee)
    _batch(db, batch_id)
    batch = _act(db, lambda: payouts.approve(db, batch_id, employee.employee_id, is_admin=employee.is_admin,
                                             reason=body.reason if body else None))
    return payouts.batch_json(db, batch, with_lines=True)


class PaidLineIn(BaseModel):
    associate_id: str = Field(min_length=1, max_length=40)
    bank_reference: str = Field(default="", max_length=100)


class MarkPaidIn(BaseModel):
    lines: list[PaidLineIn] = Field(max_length=5000)


@router.post("/payout-batches/{batch_id}/mark-paid")
def mark_payout_batch_paid(batch_id: int, body: MarkPaidIn, db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts/Admin: the bank reference of every transfer, in one call."""
    _processor(employee)
    _batch(db, batch_id)
    refs = {ln.associate_id: ln.bank_reference for ln in body.lines}
    batch = _act(db, lambda: payouts.mark_paid(db, batch_id, employee.employee_id, refs))
    return payouts.batch_json(db, batch, with_lines=True)


@router.post("/payout-batches/{batch_id}/cancel")
def cancel_payout_batch(batch_id: int, body: ReasonIn, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts/Admin: discard a draft (its entries can be batched again)."""
    _processor(employee)
    _batch(db, batch_id)
    batch = _act(db, lambda: payouts.cancel(db, batch_id, employee.employee_id, body.reason))
    return payouts.batch_json(db, batch, with_lines=True)


# ── Exports ──────────────────────────────────────────────────────────────────────

def _export(name: str, body: str):
    from fastapi.responses import Response
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'})


@router.get("/exports/commissions.csv")
def export_commissions(date_from: Optional[date] = None, date_to: Optional[date] = None,
                       db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.commissions import exports
    _processor(employee)
    return _export("commissions", exports.commissions_csv(db, date_from, date_to))


@router.get("/exports/tds.csv")
def export_tds(financial_year: Optional[str] = None, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """One row per payout line of the financial year (default: the current one), with
    the associate's PAN — decrypted through the audited helper."""
    from app.commissions import exports
    _processor(employee)
    fy = financial_year or periods.financial_year(periods.today_ist())
    if not periods.valid_fy(fy):
        raise HTTPException(status_code=422, detail={"code": "invalid_financial_year", "field": "financial_year",
                                                     "message": "financial_year must look like 2026-27."})
    body = exports.tds_csv(db, fy, employee.employee_id)
    db.commit()   # the audit rows
    return _export(f"tds-{fy}", body)


@router.get("/exports/payout-bank-{batch_id}.csv")
def export_payout_bank(batch_id: int, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts only: the bank upload file for an approved (or paid) batch — the only
    place a full account number leaves the system. Every export is audited."""
    from app.commissions import exports
    if not employee.can_export_payout_bank_file():
        raise HTTPException(status_code=403, detail="Only Accounts can export the payout bank file.")
    batch = _batch(db, batch_id)
    if batch.status not in (PayoutBatchStatus.approved, PayoutBatchStatus.paid):
        raise HTTPException(status_code=409, detail={"code": "batch_not_approved",
                                                     "message": "Only an approved batch can be exported for payment."})
    body = exports.payout_bank_csv(db, batch, employee.employee_id)
    db.commit()   # the audit rows
    return _export(f"payout-bank-{batch.period}", body)


# ── Queries (policy 4.8) ─────────────────────────────────────────────────────────

@router.get("/commission-queries")
def list_commission_queries(status: Optional[CommissionQueryStatus] = None, overdue: Optional[bool] = None,
                            page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
                            db: Session = Depends(get_db),
                            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Associate queries, oldest due first. `overdue=true`: open and past due."""
    _reader(employee)
    now = datetime.now(timezone.utc)
    q = select(CommissionQuery)
    if status:
        q = q.where(CommissionQuery.status == status)
    if overdue is not None:
        late = (CommissionQuery.status == CommissionQueryStatus.open) & (CommissionQuery.due_at < now)
        q = q.where(late if overdue else ~late)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = list(db.execute(q.order_by(CommissionQuery.due_at, CommissionQuery.id)
                           .offset((page - 1) * page_size).limit(page_size)).scalars())
    associates = {a.id: a for a in db.execute(select(Associate).where(
        Associate.id.in_({r.associate_id for r in rows}))).scalars()} if rows else {}
    entry_rows = list(db.execute(select(CommissionEntry).where(
        CommissionEntry.id.in_({r.entry_id for r in rows}))).scalars()) if rows else []
    entry_json = {e["entry_id"]: e for e in serialize.entries_json(db, entry_rows, staff=True)}
    return {"items": [{**serialize.query_json(r, staff=True, associate=associates.get(r.associate_id)),
                       "overdue": r.status == CommissionQueryStatus.open and r.due_at < now,
                       "entry": entry_json.get(r.entry_id)} for r in rows],
            "page": page, "page_size": page_size, "total": total}


class RespondIn(BaseModel):
    response: str = Field(min_length=1, max_length=2000)


@router.post("/commission-queries/{query_id}/respond")
def respond_to_commission_query(query_id: int, body: RespondIn, db: Session = Depends(get_db),
                                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accounts/Admin answer a query (once). The associate sees it on their home feed."""
    from app.commissions import queries
    _processor(employee)
    query = _act(db, lambda: queries.respond(db, query_id, body.response, employee.employee_id))
    return serialize.query_json(query, staff=True, associate=db.get(Associate, query.associate_id))


@router.post("/internal/check-commission-queries")
def run_check_commission_queries(db: Session = Depends(get_db),
                                 _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY (Cloud Scheduler). Emails Accounts once about each open
    query past its due time. Idempotent; an overlapping run answers
    {"status": "skipped", "reason": "already_running"}."""
    from app.commissions import queries
    from app.jobs.locks import run_exclusive
    return run_exclusive(db, "check-commission-queries",
                         lambda: queries.check_overdue(db, datetime.now(timezone.utc)))
