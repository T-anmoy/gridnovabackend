"""Commission queries — Commission & Payout Policy 4.8.

An associate may query one of their own entries within queries.window_days of it
appearing; Grid Nova answers within queries.response_working_days working days
(config/commissions.yaml). One open query per entry. Accounts/Admin answer; the
scheduled job (POST /api/v1/dashboard/internal/check-commission-queries) alerts Accounts
once per query that passes its due time unanswered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.commissions import config, periods
from app.memory.models import (
    Associate, AuditLog, CommissionEntry, CommissionQuery, CommissionQueryStatus,
)

MAX_MESSAGE = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due_at(raised: datetime) -> datetime:
    """The end (IST) of the working day `response_working_days` after the day raised."""
    cfg = config.queries()
    day = periods.add_working_days(periods.ist_date(raised), cfg["response_working_days"], cfg["working_weekdays"])
    return periods.start_utc(day + timedelta(days=1))


def raise_query(db: Session, associate: Associate, entry_id: int, message: str, actor: str) -> CommissionQuery:
    entry = db.get(CommissionEntry, entry_id)
    if entry is None or entry.associate_id != associate.id:
        raise AssociateError("Commission entry not found.", code="not_found", status_code=404)
    text = (message or "").strip()
    if not text or len(text) > MAX_MESSAGE:
        raise AssociateError(f"Write your query (1–{MAX_MESSAGE} characters).", code="invalid_message",
                             status_code=422, field="message")
    now = _now()
    window = timedelta(days=config.queries()["window_days"])
    if now > entry.created_at + window:
        raise AssociateError("The time to query this entry has passed.", code="query_window_closed",
                             status_code=409, extra={"window_closed_at": (entry.created_at + window).isoformat()})
    query = CommissionQuery(associate_id=associate.id, entry_id=entry.id, message=text,
                            status=CommissionQueryStatus.open, due_at=due_at(now), created_at=now)
    try:
        with db.begin_nested():
            db.add(query)
            db.flush()
    except IntegrityError:
        raise AssociateError("There is already an open query on this entry.", code="query_already_open",
                             status_code=409)
    db.add(AuditLog(entity_type="commission_query", entity_id=str(query.id), action="raised", performed_by=actor,
                    after_value={"entry_id": entry.id, "due_at": query.due_at.isoformat()}))
    return query


def respond(db: Session, query_id: int, response: str, actor: str) -> CommissionQuery:
    query = db.execute(select(CommissionQuery).where(CommissionQuery.id == query_id).with_for_update()
                       ).scalar_one_or_none()
    if query is None:
        raise AssociateError("Query not found.", code="not_found", status_code=404)
    if query.status != CommissionQueryStatus.open:
        raise AssociateError("This query has already been answered.", code="already_answered", status_code=409)
    text = (response or "").strip()
    if not text or len(text) > MAX_MESSAGE:
        raise AssociateError(f"Write the response (1–{MAX_MESSAGE} characters).", code="invalid_response",
                             status_code=422, field="response")
    query.status, query.response, query.responded_by, query.responded_at = (
        CommissionQueryStatus.answered, text, actor, _now())
    db.flush()
    db.add(AuditLog(entity_type="commission_query", entity_id=str(query.id), action="answered", performed_by=actor,
                    after_value={"entry_id": query.entry_id, "late": query.responded_at > query.due_at}))
    return query


def check_overdue(db: Session, now: datetime) -> dict:
    """Open queries past their due time, alerted once each (overdue_alerted_at); one
    Accounts email per run. Idempotent."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module

    rows = list(db.execute(select(CommissionQuery, Associate).join(Associate, Associate.id == CommissionQuery.associate_id)
                           .where(CommissionQuery.status == CommissionQueryStatus.open,
                                  CommissionQuery.due_at < now, CommissionQuery.overdue_alerted_at.is_(None))
                           .order_by(CommissionQuery.due_at)).all())
    items = []
    for query, associate in rows:
        query.overdue_alerted_at = now
        items.append({"query_id": query.id, "entry_id": query.entry_id, "associate_id": associate.associate_id,
                      "associate_name": associate.full_name, "due_at": query.due_at.isoformat()})
    if items:
        payload = {"queries": items}
        MemoryManager(db).notify_after_commit(
            lambda: notifier_module.notifier.notify("commission_queries_overdue", payload),
            "commission_queries_overdue")
    db.commit()
    return {"overdue_queries": len(items)}
