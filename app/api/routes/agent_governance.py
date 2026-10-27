"""
Agent governance admin API — runtime control over the kill switch declared in
app/governance/registry.py and enforced in app/governance/policy.py.

DISABLED_AGENTS (an env var) has always been the kill switch, but changing it
needed a redeploy — exactly the wrong tool at 2am when an agent is misbehaving.
A row in agent_governance_overrides now takes precedence over the env var, so
PATCH here is genuinely runtime-effective: the very next authorize() call reads
it, no restart required (see app/governance/policy.py::_disabled).

This is a sensitive surface — disabling the wrong agent live can stop real
customer conversations — so every change is Admin-only and audit-logged with an
explicit before/after enabled state.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.config import settings
from app.governance import policy as gov_policy, registry as gov_registry
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager

router = APIRouter(prefix="/api/v1/governance")


def _require_admin(employee: AuthenticatedEmployee) -> None:
    if not employee.has("governance.manage"):
        raise HTTPException(status_code=403, detail="Only Admin can manage agent governance.")


@router.get("/agents")
def list_agents(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Every governed agent's current enabled/disabled status and action budget.

    Read from the same registry + policy the enforcement layer itself uses, so
    this can never drift from what the agents are really allowed to do.
    """
    _require_admin(employee)

    memory = MemoryManager(db)
    agents = []
    for spec in gov_registry.all_specs():
        override = memory.get_agent_override(spec.id)
        agents.append({
            "id": spec.id,
            "name": spec.name,
            "enabled": not gov_policy._disabled(spec.id, memory=memory),
            "override_source": "database" if override is not None else "env_var",
            "daily_action_limit_per_lead": settings.agent_daily_action_limit_per_lead,
        })

    return {
        "agents": agents,
        # There is no dollar-denominated spend budget in this system today — the
        # only enforced budget is the 24h action-count limit below, scoped to
        # reply_to_customer (see app/governance/policy.py::_over_budget).
        "action_budget": {
            "replies_per_lead_per_24h": settings.agent_daily_action_limit_per_lead,
            "scope": "reply_to_customer",
        },
    }


class AgentToggleRequest(BaseModel):
    enabled: bool


@router.patch("/agents/{agent_id}")
def toggle_agent(
    agent_id: str,
    body: AgentToggleRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Flip an agent's kill switch. Takes effect immediately — the next
    authorize() call for this agent reads the override written here, with no
    redeploy and no process restart."""
    _require_admin(employee)

    if gov_registry.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"No registered agent {agent_id!r}.")

    memory = MemoryManager(db)
    override = memory.set_agent_override(agent_id, enabled=body.enabled, performed_by=employee.employee_id)
    db.commit()

    return {"agent_id": agent_id, "enabled": override.enabled, "updated_by": override.updated_by}


@router.get("/agents/{agent_id}/audit")
def agent_audit_history(
    agent_id: str,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Recent denial and kill-switch history for one agent — who changed what,
    when, and every time governance refused it something."""
    _require_admin(employee)

    if gov_registry.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"No registered agent {agent_id!r}.")

    memory = MemoryManager(db)
    entries = memory.list_agent_governance_audit(agent_id)
    return {
        "agent_id": agent_id,
        "items": [
            {
                "action": e.action,
                "performed_by": e.performed_by,
                "before_value": e.before_value,
                "after_value": e.after_value,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ],
    }
