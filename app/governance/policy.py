"""
Policy enforcement — every governed agent action passes through here first.

WHAT THIS IS FOR
The registry declares what each agent may do. This module is what makes that
declaration bite: an action not in an agent's capabilities, or listed in its
prohibitions, is denied at the moment it is attempted rather than trusted not to
happen. The Proposal Agent cannot send a proposal not merely because no code path
calls send, but because asking would be refused.

FOUR THINGS IT CHECKS
1. Is the agent switched on. DISABLED_AGENTS is a kill switch that takes effect
   through configuration, without a deploy — the thing you want at 2am when an agent
   is misbehaving and the alternative is shipping a hotfix.
2. Is the action within its declared capabilities.
3. Is the action explicitly prohibited. Checked separately from (2) so a denial can
   say which it was: an action nobody granted reads very differently in an incident
   review from one somebody deliberately forbade.
4. Is the agent inside its budget for this lead. A conversation that loops — an
   integration retrying, a script hammering the endpoint — should stop and fetch a
   human rather than run up spend unnoticed.

EVERY DECISION IS RECORDED
Denials are written to the audit log against the agent, and a denial of something
prohibited also raises an internal notification: an agent attempting a forbidden
action is not routine, and nobody should have to go looking for it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.governance import registry

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Denial codes, kept stable so alerting can key off them.
OK = "ok"
UNKNOWN_AGENT = "unknown_agent"
AGENT_DISABLED = "agent_disabled"
NOT_PERMITTED = "action_not_permitted"
PROHIBITED = "action_prohibited"
BUDGET_EXCEEDED = "budget_exceeded"

_BUDGET_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    reason: str
    agent_id: str
    action: str

    def __bool__(self) -> bool:
        return self.allowed


def _disabled(agent_id: str, memory: Optional["MemoryManager"] = None) -> bool:
    """Whether an agent is switched off right now.

    A DB-backed override (set via the /api/v1/governance/agents API, runtime — no
    redeploy) takes precedence when one exists; DISABLED_AGENTS remains the
    deploy-time default for any agent that has never been toggled through the API.
    """
    if memory is not None:
        override = memory.get_agent_override(agent_id)
        if override is not None:
            return not override.enabled
    return agent_id in {a.strip() for a in settings.disabled_agents if a.strip()}


def authorize(
    agent_id: str,
    action: str,
    *,
    memory: Optional["MemoryManager"] = None,
    lead_id: Optional[int] = None,
    record: bool = True,
) -> Decision:
    """Decide whether an agent may take an action right now.

    Never raises. A governance layer that can crash the thing it governs is worse
    than none, so an unexpected failure here allows the action and logs loudly —
    the deterministic business rules and human approval gates are unchanged either
    way, and they are what actually protect the commercial outcome.
    """
    try:
        decision = _evaluate(agent_id, action, memory=memory, lead_id=lead_id)
    except Exception:
        logger.exception(
            "Governance check failed for %s/%s — allowing, other controls still apply",
            agent_id, action,
        )
        return Decision(True, OK, "governance check errored; allowed", agent_id, action)

    if not decision.allowed and record:
        _record_denial(decision, memory=memory, lead_id=lead_id)
    return decision


def _evaluate(
    agent_id: str,
    action: str,
    memory: Optional["MemoryManager"],
    lead_id: Optional[int],
) -> Decision:
    spec = registry.get(agent_id)
    if spec is None:
        return Decision(False, UNKNOWN_AGENT, f"No registered agent {agent_id!r}.",
                        agent_id, action)

    if _disabled(agent_id, memory=memory):
        return Decision(False, AGENT_DISABLED,
                        f"{spec.name} is switched off by configuration.", agent_id, action)

    if action in spec.prohibitions:
        return Decision(False, PROHIBITED,
                        f"{spec.name} is explicitly prohibited from {action}.",
                        agent_id, action)

    if action not in spec.capabilities:
        return Decision(False, NOT_PERMITTED,
                        f"{spec.name} has no capability for {action}.", agent_id, action)

    over, used = _over_budget(spec, action, memory, lead_id)
    if over:
        return Decision(
            False, BUDGET_EXCEEDED,
            f"{spec.name} has already taken {used} actions on lead {lead_id} in the "
            f"last 24h (limit {settings.agent_daily_action_limit_per_lead}).",
            agent_id, action,
        )

    return Decision(True, OK, "permitted", agent_id, action)


def _over_budget(spec, action, memory, lead_id) -> tuple:
    """Runaway protection, scoped to the one action that can loop.

    Only customer replies are counted: drafting and recall are human-triggered, so a
    person is already the rate limit there. A limit of 0 or less turns this off.
    """
    limit = settings.agent_daily_action_limit_per_lead
    if limit <= 0 or action != registry.REPLY_TO_CUSTOMER:
        return False, 0
    if memory is None or lead_id is None:
        return False, 0

    since = datetime.now(timezone.utc) - _BUDGET_WINDOW
    used = memory.count_agent_messages_since(lead_id, since)
    return used >= limit, used


def _record_denial(decision: Decision, memory, lead_id) -> None:
    logger.warning(
        "Governance denied an agent action",
        extra={"agent": decision.agent_id, "action": decision.action,
               "code": decision.code, "lead_id": lead_id},
    )
    if memory is not None:
        try:
            memory.record_agent_action(
                agent_id=decision.agent_id,
                action=decision.action,
                allowed=False,
                code=decision.code,
                reason=decision.reason,
                lead_id=lead_id,
            )
        except Exception:
            logger.exception("Could not record governance denial to the audit log")

    # An agent reaching for something it is forbidden to do is not routine.
    if decision.code == PROHIBITED:
        try:
            from app.notifications.notifier import notifier

            notifier.notify("agent_attempted_prohibited_action", {
                "agent": decision.agent_id, "action": decision.action, "lead_id": lead_id,
            })
        except Exception:
            logger.exception("Could not raise a notification for a prohibited action")
