"""
Agent harness — the one execution wrapper every agent runs inside.

WHY
Before this, each agent was invoked ad-hoc. Every call site repeated its own
try/except, governance was called separately inside each agent (and could simply be
forgotten in a fourth one), a turn had no identity you could trace through the logs,
and nothing measured how long an agent took or how many model calls it made. The
behaviour was fine; the scaffolding around it was different in each place.

WHAT EVERY RUN NOW GETS
- Governance, applied here rather than trusted to each agent. An agent cannot skip
  authorisation by forgetting to call it, because it never calls it.
- A run id, stamped into every log line for that turn, so one conversation turn can
  be followed end to end across three modules.
- Timing, with a warning when a run crosses the slow threshold.
- Model-call and estimated-cost accounting for the turn. Counted per model
  OPERATION the agent invoked, not per HTTP call: in mock mode several operations
  never reach the transport at all, and a count that changed shape depending on
  whether a key was configured would be worse than no count.
- Exception containment: a raised agent becomes a failed run with a caller-supplied
  fallback payload, never an exception escaping into a customer's conversation.
- A durable record of the run in the audit trail.

WHAT IT DELIBERATELY DOES NOT DO
It does not enforce a wall-clock timeout on the agent body. A Python thread cannot
be killed, and each agent holds a database session that is not safe to use from
another thread, so a "timeout" here would be theatre — it would return early while
the work carried on underneath. Time is bounded where it is actually spent instead:
LLM_REQUEST_TIMEOUT_SECONDS on the model client. What this measures, it measures
honestly.

It also does not retry. A retried customer reply is a second message to a real
person, and a retried booking is a second calendar entry; neither is safe to repeat
blindly. The model client's own transport-level retries cover the transient case.
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.config import settings
from app.governance import policy

logger = logging.getLogger(__name__)

OK = "ok"
DENIED = "denied"
FAILED = "failed"

# The id of the run currently executing, so any module can stamp it into a log line
# without it being threaded through every function signature.
current_run_id: contextvars.ContextVar = contextvars.ContextVar("agent_run_id", default="")

# Model calls made during the current run. app/llm/client.py increments this; the
# harness reads it either side of the agent body to attribute calls to a turn.
llm_call_count: contextvars.ContextVar = contextvars.ContextVar("agent_llm_calls", default=0)


# Model calls the agent deliberately did NOT make this run, because a deterministic
# gate found nothing for the model to do. Counted so the saving is visible rather
# than being an invisible optimisation nobody can verify.
llm_skip_count: contextvars.ContextVar = contextvars.ContextVar("agent_llm_skips", default=0)


def note_llm_call() -> None:
    """Called by the LLM client on every real or mocked model call."""
    try:
        llm_call_count.set(llm_call_count.get() + 1)
    except Exception:  # pragma: no cover — a counter must never break a call
        pass


def note_llm_skip() -> None:
    """Called by app/agents/need.py when a gate skips a model call."""
    try:
        llm_skip_count.set(llm_skip_count.get() + 1)
    except Exception:  # pragma: no cover
        pass


@dataclass
class AgentRun:
    """The uniform envelope every agent invocation returns."""
    run_id: str
    agent_id: str
    action: str
    code: str                       # ok | denied | failed
    result: dict = field(default_factory=dict)
    error: Optional[str] = None
    governance_code: str = policy.OK
    duration_ms: float = 0.0
    llm_calls: int = 0
    llm_calls_skipped: int = 0
    estimated_cost_usd: float = 0.0
    slow: bool = False
    started_at: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return self.code == OK

    def summary(self) -> dict:
        """The record written to the audit trail — everything except the payload,
        which may carry customer text and belongs in the conversation log instead."""
        return {
            "run_id": self.run_id,
            "agent": self.agent_id,
            "action": self.action,
            "code": self.code,
            "governance_code": self.governance_code,
            "duration_ms": round(self.duration_ms, 1),
            "llm_calls": self.llm_calls,
            "llm_calls_skipped": self.llm_calls_skipped,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "slow": self.slow,
            "error": self.error,
        }


def run(
    agent_id: str,
    action: str,
    fn: Callable[[], dict],
    *,
    memory: Any = None,
    lead_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    on_denied: Optional[Callable[[Any], dict]] = None,
    on_error: Optional[Callable[[Exception], dict]] = None,
) -> AgentRun:
    """Execute one agent action under governance, timing and accounting.

    `fn` is the agent's own work, taking nothing and returning its payload dict.
    `on_denied` and `on_error` build the payload for those paths — each agent knows
    what a safe answer looks like for its own audience far better than this module
    does, so neither is invented here.
    """
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    token_run = current_run_id.set(run_id)
    token_calls = llm_call_count.set(0)
    token_skips = llm_skip_count.set(0)
    clock = time.perf_counter()

    try:
        decision = policy.authorize(
            agent_id, action, memory=memory, lead_id=lead_id
        )
        if not decision.allowed:
            agent_run = _finish(
                run_id, agent_id, action, DENIED, clock, started,
                result=on_denied(decision) if on_denied else {"error": decision.reason},
                error=decision.reason, governance_code=decision.code,
            )
            _record(agent_run, memory, lead_id, customer_id, durable=True)
            return agent_run

        try:
            payload = fn() or {}
        except Exception as exc:
            logger.error(
                "Agent run failed", exc_info=True,
                extra={"run_id": run_id, "agent": agent_id, "action": action,
                       "lead_id": lead_id},
            )
            agent_run = _finish(
                run_id, agent_id, action, FAILED, clock, started,
                result=on_error(exc) if on_error else {"error": str(exc)},
                error=f"{type(exc).__name__}: {exc}",
            )
            # A failed run is written on its own session: the caller is about to roll
            # back, and the runs most worth having a record of are the failures.
            _record(agent_run, memory, lead_id, customer_id, durable=True)
            return agent_run

        agent_run = _finish(run_id, agent_id, action, OK, clock, started, result=payload)
        logger.info(
            "Agent run completed",
            extra={"run_id": run_id, "agent": agent_id, "action": action,
                   "duration_ms": round(agent_run.duration_ms, 1),
                   "llm_calls": agent_run.llm_calls, "lead_id": lead_id},
        )
        _record(agent_run, memory, lead_id, customer_id, durable=False)
        return agent_run
    finally:
        current_run_id.reset(token_run)
        llm_call_count.reset(token_calls)
        llm_skip_count.reset(token_skips)


def _finish(
    run_id, agent_id, action, code, clock, started,
    result=None, error=None, governance_code=policy.OK,
) -> AgentRun:
    duration_ms = (time.perf_counter() - clock) * 1000
    calls = llm_call_count.get()
    skips = llm_skip_count.get()
    slow = duration_ms > settings.agent_slow_run_ms > 0
    if slow:
        logger.warning(
            "Agent run was slow",
            extra={"run_id": run_id, "agent": agent_id, "action": action,
                   "duration_ms": round(duration_ms, 1),
                   "threshold_ms": settings.agent_slow_run_ms},
        )
    return AgentRun(
        run_id=run_id, agent_id=agent_id, action=action, code=code,
        result=result or {}, error=error, governance_code=governance_code,
        duration_ms=duration_ms, llm_calls=calls, llm_calls_skipped=skips,
        estimated_cost_usd=calls * settings.ai_estimated_cost_per_call,
        slow=slow, started_at=started,
    )


def _record(agent_run: AgentRun, memory, lead_id, customer_id, durable: bool) -> None:
    """Write the run to the audit trail.

    `durable` uses a fresh session, because a denial or a failure is usually about to
    be rolled back by the caller along with everything else in the transaction.
    """
    try:
        if durable:
            from app.memory.db import SessionLocal
            from app.memory.memory_manager import MemoryManager

            db = SessionLocal()
            try:
                MemoryManager(db).record_agent_run(
                    agent_run.summary(), lead_id=lead_id, customer_id=customer_id
                )
                db.commit()
            finally:
                db.close()
        elif memory is not None:
            memory.record_agent_run(
                agent_run.summary(), lead_id=lead_id, customer_id=customer_id
            )
    except Exception:
        logger.exception("Could not record agent run %s", agent_run.run_id)
