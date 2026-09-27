"""The one lead-visibility rule, used by every lead-, proposal- and site-assessment
endpoint:

  * Admin, Management — every lead.
  * Engineer          — every lead, through the technical-only views each endpoint
                        already limits them to (unchanged here).
  * Sales             — leads assigned to them, or not assigned to anyone.
  * Operations,
    Accounts          — none (both roles are deliberately maximally restricted; see
                        AuthenticatedEmployee.can_view_assigned_work()).

Single records go through require_lead_access(); list queries through LeadScope,
which becomes a WHERE clause so filtering never happens after the fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import false, or_, true

from app.memory.models import EmployeeRole, Lead

ASSIGNED_FILTERS = ("me", "unassigned", "any")
NOT_YOURS = "This lead is not assigned to you."


@dataclass(frozen=True)
class LeadScope:
    everything: bool = False
    employee_id: Optional[str] = None  # include leads assigned to this employee
    unassigned: bool = False           # include leads with no assignee

    def condition(self):
        if self.everything:
            return true()
        parts = []
        if self.employee_id:
            parts.append(Lead.assigned_employee_id == self.employee_id)
        if self.unassigned:
            parts.append(Lead.assigned_employee_id.is_(None))
        return or_(*parts) if parts else false()


def _sees_everything(employee) -> bool:
    return employee.role in (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.engineer)


def can_access_lead(employee, lead) -> bool:
    if lead is None:
        return False
    if _sees_everything(employee):
        return True
    if employee.role == EmployeeRole.sales:
        return lead.assigned_employee_id in (None, employee.employee_id)
    return False


def require_lead_access(employee, lead, detail: str = NOT_YOURS) -> None:
    if not can_access_lead(employee, lead):
        raise HTTPException(status_code=403, detail=detail)


def lead_scope(employee, assigned: Optional[str] = None) -> LeadScope:
    """The leads a list endpoint returns. `assigned` narrows it (me / unassigned /
    any); for Sales, "any" still means own + unassigned."""
    if assigned is not None and assigned not in ASSIGNED_FILTERS:
        raise HTTPException(
            status_code=422, detail=f"assigned must be one of: {', '.join(ASSIGNED_FILTERS)}",
        )
    if assigned == "me":
        return LeadScope(employee_id=employee.employee_id)
    if assigned == "unassigned":
        if _sees_everything(employee) or employee.role == EmployeeRole.sales:
            return LeadScope(unassigned=True)
        return LeadScope()
    if _sees_everything(employee):
        return LeadScope(everything=True)
    if employee.role == EmployeeRole.sales:
        return LeadScope(employee_id=employee.employee_id, unassigned=True)
    return LeadScope()
