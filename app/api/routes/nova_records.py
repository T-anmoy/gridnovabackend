"""Nova Records (BE-9): the internal assistant for signed-in staff — grounded, cited,
role-scoped, read-only. Contract: docs/api/nova_records.md.

Who: every staff role may ask; each answer holds only what the asker's role may see (the
tools apply app/auth/scoping.py and config/archive.yaml). Conversations are private to
the employee who had them; Admin may read any conversation for audit (and the read is
audit-logged). Nothing here changes a record.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import AuditLog, NovaConversation, NovaTurn
from app.records import config, store

router = APIRouter(prefix="/api/v1/dashboard/nova")

STATUSES = Literal["answered", "not_on_record", "needs_clarification", "refused"]
_ID = r"^[0-9a-f]{32}$"


def _fail(status: int, code: str, message: str, field: Optional[str] = None, headers=None) -> HTTPException:
    detail = {"code": code, "message": message}
    if field:
        detail["field"] = field
    return HTTPException(status_code=status, detail=detail, headers=headers)


# ── Models ───────────────────────────────────────────────────────────────────────

class AskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: Optional[int] = Field(None, ge=1)
    project_ref: Optional[str] = Field(None, min_length=2, max_length=100)
    customer_id: Optional[int] = Field(None, ge=1)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    conversation_id: Optional[str] = Field(None, pattern=_ID)
    context: Optional[AskContext] = None


class Citation(BaseModel):
    ref: str
    kind: str
    label: str
    path: Optional[str] = None


class TableColumn(BaseModel):
    key: str
    label: str
    type: Literal["text", "ref", "money", "kw", "date", "number", "boolean"]


class Table(BaseModel):
    title: str
    columns: list[TableColumn]
    rows: list[dict[str, Any]]


class Searched(BaseModel):
    tool: str
    filters: dict[str, Any]
    summary: str


class Suggestion(BaseModel):
    label: str
    question: Optional[str] = None
    path: Optional[str] = None


class Answer(BaseModel):
    status: STATUSES
    answer: str
    citations: list[Citation]
    tables: list[Table]
    searched: list[Searched]
    suggestions: list[Suggestion]
    generated_at: str


class AskResponse(Answer):
    conversation_id: str
    turn_id: str


class Feedback(BaseModel):
    helpful: Optional[bool] = None
    note: Optional[str] = None
    at: Optional[str] = None


class TurnOut(Answer):
    turn_id: str
    question: str
    context: Optional[dict[str, Any]] = None
    feedback: Feedback


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    turn_count: int
    created_at: str
    updated_at: str


class ConversationList(BaseModel):
    items: list[ConversationSummary]


class ConversationDetail(ConversationSummary):
    employee_id: str
    role: str
    turns: list[TurnOut]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    helpful: bool
    note: Optional[str] = Field(None, max_length=1000)


class FeedbackResponse(BaseModel):
    turn_id: str
    helpful: bool
    note: Optional[str] = None
    feedback_at: str


class RetentionRun(BaseModel):
    status: str
    conversations_deleted: Optional[int] = None
    turns_deleted: Optional[int] = None
    retention_days: Optional[int] = None
    cutoff: Optional[str] = None
    reason: Optional[str] = None


def _iso(d: Optional[datetime]) -> Optional[str]:
    if d is None:
        return None
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)).isoformat()


def _summary(c: NovaConversation) -> dict:
    return {"conversation_id": c.id, "title": c.title, "turn_count": c.turn_count, "created_at": _iso(c.created_at),
            "updated_at": _iso(c.updated_at)}


# ── Ask ──────────────────────────────────────────────────────────────────────────

@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, db: Session = Depends(get_db),
        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Ask Nova Records a question. `status` is answered | not_on_record |
    needs_clarification | refused; every number in `answer` comes from the records and
    `tables` are built from the rows (docs/api/nova_records.md)."""
    question = body.question.strip()
    if not question:
        raise _fail(422, "empty_question", "Ask a question.", "question")
    if not store.within_rate_limit(employee.employee_id):
        raise _fail(429, "rate_limited", "You're asking faster than Nova Records allows. Wait a minute and try again.",
                    headers={"Retry-After": "60"})
    if store.asked_today(db, employee.employee_id) >= config.limit("daily_questions"):
        raise _fail(429, "daily_limit_reached", "You've reached today's question limit for Nova Records. "
                                                "It resets at midnight (India time).")
    conversation = None
    if body.conversation_id:
        conversation = db.get(NovaConversation, body.conversation_id)
        if conversation is None or conversation.employee_id != employee.employee_id:
            raise _fail(404, "conversation_not_found", "No such conversation.", "conversation_id")
    conversation_id = conversation.id if conversation else store.new_id()
    history = store.history(db, conversation.id) if conversation else []
    context = body.context.model_dump(exclude_none=True) if body.context else {}
    # The agent reads on its own session (app/orchestrator); this request's read
    # transaction ends first.
    db.commit()

    from app.orchestrator.orchestrator import orchestrator
    turn_id = store.new_id()
    payload = orchestrator.answer_records_question(viewer=employee, question=question, context=context,
                                                   history=history, today=store.ist_today(), turn_id=turn_id)
    if payload.get("denied"):
        raise _fail(503, "assistant_disabled", payload.get("answer") or "Nova Records is switched off.")
    if payload.get("status") not in ("answered", "not_on_record", "needs_clarification", "refused"):
        raise _fail(503, "records_unavailable", "I couldn't reach the records just now. Try again shortly.")

    generated_at = datetime.now(timezone.utc).isoformat()
    response = {k: payload[k] for k in ("status", "answer", "citations", "tables", "searched", "suggestions")}
    response["generated_at"] = generated_at
    conversation = db.get(NovaConversation, conversation_id) if conversation else None
    store.save_turn(db, conversation=conversation, conversation_id=conversation_id, turn_id=turn_id,
                    employee=employee, question=question, context=context, payload=payload, response=response)
    db.commit()
    return {**response, "conversation_id": conversation_id, "turn_id": turn_id}


# ── Conversations ────────────────────────────────────────────────────────────────

@router.get("/conversations", response_model=ConversationList)
def list_conversations(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The caller's own conversations, most recent first (at most 50, within retention)."""
    rows = db.execute(select(NovaConversation).where(
        NovaConversation.employee_id == employee.employee_id,
        NovaConversation.updated_at >= store.retention_cutoff())
        .order_by(NovaConversation.updated_at.desc(), NovaConversation.id).limit(50)).scalars()
    return {"items": [_summary(c) for c in rows]}


def _turn_out(t: NovaTurn) -> dict:
    r = t.response or {}
    return {"turn_id": t.id, "question": t.question, "context": t.context, "status": t.status, "answer": t.answer,
            "citations": r.get("citations", []), "tables": r.get("tables", []), "searched": r.get("searched", []),
            "suggestions": r.get("suggestions", []), "generated_at": r.get("generated_at") or _iso(t.created_at),
            "feedback": {"helpful": t.feedback_helpful, "note": t.feedback_note, "at": _iso(t.feedback_at)}}


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """One conversation with every turn as it was shown. Own only; Admin may read any for
    audit (that read is written to the audit log). Anything else is 404."""
    c = db.get(NovaConversation, conversation_id)
    if c is None or (c.employee_id != employee.employee_id and not employee.is_admin):
        raise _fail(404, "conversation_not_found", "No such conversation.")
    if c.employee_id != employee.employee_id:
        db.add(AuditLog(entity_type="nova_conversation", entity_id=c.id, action="nova_conversation_read_for_audit",
                        performed_by=employee.employee_id, after_value={"owner": c.employee_id}))
        db.commit()
    turns = db.execute(select(NovaTurn).where(NovaTurn.conversation_id == c.id)
                       .order_by(NovaTurn.created_at, NovaTurn.id)).scalars()
    return {**_summary(c), "employee_id": c.employee_id, "role": c.role, "turns": [_turn_out(t) for t in turns]}


@router.post("/turns/{turn_id}/feedback", response_model=FeedbackResponse)
def turn_feedback(turn_id: str, body: FeedbackRequest, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Was this answer helpful? On the caller's own turns only; sending again replaces it."""
    t = db.get(NovaTurn, turn_id)
    if t is None or t.employee_id != employee.employee_id:
        raise _fail(404, "turn_not_found", "No such answer.")
    t.feedback_helpful = body.helpful
    t.feedback_note = (body.note or "").strip() or None
    t.feedback_at = datetime.now(timezone.utc)
    db.commit()
    return {"turn_id": t.id, "helpful": t.feedback_helpful, "note": t.feedback_note, "feedback_at": _iso(t.feedback_at)}


# ── Retention job ────────────────────────────────────────────────────────────────

@router.post("/internal/check-nova-records-retention", response_model=RetentionRun)
def run_retention(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY (Cloud Scheduler). Deletes conversations last used more
    than retention.days ago (NR-3). Idempotent; an overlapping run answers
    {"status": "skipped", "reason": "already_running"}."""
    from app.jobs.locks import run_exclusive
    return run_exclusive(db, "check-nova-records-retention", lambda: store.purge_expired(db))
