"""Commission entries as API JSON — one page of rows in a fixed number of queries.

Amounts are decimal strings; rates are percentage strings ("2.5000"); dates ISO.
`pending_message_key` is for the associate app to translate (never English).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commissions import config
from app.memory.models import (
    Associate, CommissionEntry, CommissionQuery, CommissionQueryStatus, CommissionRateSchedule, Customer,
    PayoutBatch, Project,
)

CENT = Decimal("0.01")


def s(value) -> Optional[str]:
    return None if value is None else str(Decimal(value).quantize(CENT))


def pct(value) -> Optional[str]:
    return None if value is None else str(Decimal(value).quantize(Decimal("0.0001")))


def iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _by_id(db: Session, model, ids) -> dict:
    ids = {i for i in ids if i is not None}
    return {r.id: r for r in db.execute(select(model).where(model.id.in_(ids))).scalars()} if ids else {}


def entries_json(db: Session, entries: list[CommissionEntry], *, staff: bool) -> list[dict]:
    associates = _by_id(db, Associate, (e.associate_id for e in entries))
    projects = _by_id(db, Project, (e.project_id for e in entries))
    customers = _by_id(db, Customer, (p.customer_id for p in projects.values()))
    batches = _by_id(db, PayoutBatch, (e.batch_id for e in entries))
    schedules = _by_id(db, CommissionRateSchedule, (e.schedule_id for e in entries))
    ids = [e.id for e in entries]
    counters = dict(db.execute(select(CommissionEntry.reverses_entry_id, CommissionEntry.id).where(
        CommissionEntry.reverses_entry_id.in_(ids))).all()) if ids else {}
    open_queries = set(db.execute(select(CommissionQuery.entry_id).where(
        CommissionQuery.entry_id.in_(ids), CommissionQuery.status == CommissionQueryStatus.open)).scalars()) \
        if ids else set()
    window = timedelta(days=config.queries()["window_days"])
    out = []
    for e in entries:
        a, p = associates.get(e.associate_id), projects.get(e.project_id)
        c = customers.get(p.customer_id) if p else None
        b, sch = batches.get(e.batch_id), schedules.get(e.schedule_id)
        row = {
            "entry_id": e.id,
            "associate": {"associate_id": a.associate_id, "full_name": a.full_name} if a else None,
            "earning_role": e.earning_role.value, "kind": e.kind.value, "status": e.status.value,
            "customer_name": c.name if c else None, "project_reference_id": p.reference_id if p else None,
            "onboarding_id": e.onboarding_id, "payment_id": e.payment_id,
            "basis_amount": s(e.basis_amount), "rate_percent": pct(e.rate_percent), "holders": e.holders,
            "amount": s(e.amount), "pending_reason": e.pending_reason,
            "pending_message_key": f"commission.pending.{e.pending_reason}" if e.pending_reason else None,
            "schedule_version": sch.version if sch else None, "agreement_date": iso(e.agreement_date),
            "reverses_entry_id": e.reverses_entry_id, "reversed_by_entry_id": counters.get(e.id),
            "reason": e.reason,
            "batch": {"batch_id": b.id, "period": b.period, "status": b.status.value} if b else None,
            "created_at": iso(e.created_at), "approved_at": iso(e.approved_at), "paid_at": iso(e.paid_at),
        }
        if staff:
            row.update(created_by=e.created_by, approved_by=e.approved_by, rated_by=e.rated_by,
                       rated_at=iso(e.rated_at))
        else:
            closes = e.created_at + window
            row["query"] = {"open": e.id in open_queries, "window_closes_at": iso(closes)}
        out.append(row)
    return out


def query_json(q: CommissionQuery, *, staff: bool, associate: Optional[Associate] = None) -> dict:
    row = {"query_id": q.id, "entry_id": q.entry_id, "message": q.message, "status": q.status.value,
           "response": q.response, "responded_at": iso(q.responded_at), "due_at": iso(q.due_at),
           "created_at": iso(q.created_at)}
    if staff:
        row.update(responded_by=q.responded_by,
                   associate={"associate_id": associate.associate_id, "full_name": associate.full_name}
                   if associate else None)
    return row
