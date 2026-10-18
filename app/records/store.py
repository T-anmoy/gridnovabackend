"""Nova Records conversations: limits, persistence, history, feedback and retention.

  * Rate limit (NR-1): requests per minute per employee, held in this app instance's
    memory (like every other rate limit here — BE-3: per-instance).
  * Daily cap (NR-2): questions per employee per IST day, counted from nova_turns, so it
    holds across instances and restarts.
  * Retention (NR-3): conversations whose last turn is older than retention.days are
    deleted by POST /internal/check-nova-records-retention; until then they are hidden
    from the conversation list once past retention.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.models import NovaConversation, NovaTurn
from app.records import config
from app.security.rate_limit import RateLimitLog

_minute_log = RateLimitLog(60)


def new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ist_today(now: Optional[datetime] = None) -> date:
    return (now or _now()).astimezone(ZoneInfo(settings.business_timezone)).date()


def within_rate_limit(employee_id: str) -> bool:
    return _minute_log.hit(employee_id, config.limit("requests_per_minute"))


def asked_today(db: Session, employee_id: str, now: Optional[datetime] = None) -> int:
    start = datetime.combine(ist_today(now), time.min, tzinfo=ZoneInfo(settings.business_timezone))
    return db.execute(select(func.count(NovaTurn.id)).where(
        NovaTurn.employee_id == employee_id, NovaTurn.created_at >= start.astimezone(timezone.utc))).scalar_one()


def retention_cutoff(now: Optional[datetime] = None) -> datetime:
    return (now or _now()) - timedelta(days=config.retention_days())


def history(db: Session, conversation_id: str) -> list[dict]:
    """The last turns, oldest first, as the planner sees them: question, status, refs."""
    n = config.limit("history_turns")
    turns = list(db.execute(select(NovaTurn).where(NovaTurn.conversation_id == conversation_id)
                            .order_by(NovaTurn.created_at.desc(), NovaTurn.id.desc()).limit(n)).scalars())
    return [{"question": t.question, "status": t.status, "refs": list(t.refs or [])[:10]} for t in reversed(turns)]


def save_turn(db: Session, *, conversation: Optional[NovaConversation], conversation_id: str, turn_id: str,
              employee, question: str, context: Optional[dict], payload: dict, response: dict) -> NovaConversation:
    now = _now()
    role = getattr(employee.role, "value", employee.role)
    if conversation is None:
        conversation = NovaConversation(id=conversation_id, employee_id=employee.employee_id, role=role,
                                        title=question[:120], turn_count=0, created_at=now, updated_at=now)
        db.add(conversation)
        db.flush()
    conversation.turn_count = (conversation.turn_count or 0) + 1
    conversation.updated_at = now
    db.add(NovaTurn(
        id=turn_id, conversation_id=conversation.id, employee_id=employee.employee_id, role=role, question=question,
        context=context or None, status=payload["status"], answer=payload["answer"], response=response,
        intent=(payload.get("intent") or None) and str(payload["intent"])[:64], planner=payload.get("planner"),
        composer=payload.get("composer"), tool_calls=payload.get("tool_calls") or [], refs=payload.get("refs") or [],
        verifier=payload.get("verifier"), model=payload.get("model"), prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"), llm_calls=payload.get("llm_calls") or 0,
        latency_ms=payload.get("latency_ms") or 0, run_id=payload.get("agent_run_id"), created_at=now,
    ))
    return conversation


def purge_expired(db: Session, now: Optional[datetime] = None) -> dict:
    """Delete every conversation (and its turns) last updated before the retention cutoff."""
    cutoff = retention_cutoff(now)
    ids = [i for (i,) in db.execute(select(NovaConversation.id).where(NovaConversation.updated_at < cutoff))]
    turns = 0
    if ids:
        turns = db.execute(delete(NovaTurn).where(NovaTurn.conversation_id.in_(ids))).rowcount or 0
        db.execute(delete(NovaConversation).where(NovaConversation.id.in_(ids)))
    db.commit()
    return {"status": "ok", "conversations_deleted": len(ids), "turns_deleted": int(turns),
            "retention_days": config.retention_days(), "cutoff": cutoff.isoformat()}
