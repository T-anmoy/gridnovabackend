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

Associates see customers they onboarded through the parallel OnboardingScope below
(own customers; a Preferred Associate also their territory's). Staff scoping above is
unchanged.
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


# ── Associate visibility over onboarded customers ──────────────────────────────
#
#   * Associate (plain)       — the onboardings they own.
#   * Preferred Associate     — their own, plus every onboarding whose SITE lies in
#                               their territory: a State Head's state, a District
#                               Head's district, a block-level Preferred's block.
#                               Never a sibling territory.
#
# Viewing is not editing: only the owner changes an onboarding (can_edit_onboarding).

ONBOARDING_NOT_YOURS = "This customer is not in your portfolio."


@dataclass(frozen=True)
class OnboardingScope:
    associate_pk: int
    level: Optional[str] = None
    territory_id: Optional[int] = None

    def condition(self):
        from app.memory.models import CustomerOnboarding

        parts = [CustomerOnboarding.owner_associate_id == self.associate_pk]
        column = {"state_head": CustomerOnboarding.state_id,
                  "district_head": CustomerOnboarding.district_id,
                  "block": CustomerOnboarding.block_id}.get(self.level or "")
        if column is not None and self.territory_id is not None:
            parts.append(column == self.territory_id)
        return or_(*parts)


def onboarding_scope(associate) -> OnboardingScope:
    """`associate` is the Associate row of the signed-in associate."""
    level = associate.level.value if associate.level is not None else None
    territory_id = {"state_head": associate.territory_state_id,
                    "district_head": associate.territory_district_id,
                    "block": associate.territory_block_id}.get(level or "")
    return OnboardingScope(associate_pk=associate.id, level=level, territory_id=territory_id)


def can_view_onboarding(associate, onboarding) -> bool:
    if onboarding is None:
        return False
    scope = onboarding_scope(associate)
    if onboarding.owner_associate_id == scope.associate_pk:
        return True
    site = {"state_head": onboarding.state_id, "district_head": onboarding.district_id,
            "block": onboarding.block_id}.get(scope.level or "")
    return scope.territory_id is not None and site == scope.territory_id


def can_edit_onboarding(associate, onboarding) -> bool:
    return onboarding is not None and onboarding.owner_associate_id == associate.id


def require_onboarding_view(associate, onboarding) -> None:
    if not can_view_onboarding(associate, onboarding):
        raise HTTPException(status_code=403, detail=ONBOARDING_NOT_YOURS)


def require_onboarding_edit(associate, onboarding) -> None:
    if not can_view_onboarding(associate, onboarding):
        raise HTTPException(status_code=403, detail=ONBOARDING_NOT_YOURS)
    if not can_edit_onboarding(associate, onboarding):
        raise HTTPException(status_code=403, detail="Only the associate who onboarded this customer can change it.")
