"""The one lead-visibility rule, used by every lead-, proposal- and site-assessment
endpoint:

  * Admin, Management — every lead.
  * Engineer          — every lead, through the technical-only views each endpoint
                        already limits them to (unchanged here).
  * Sales             — leads assigned to them, or not assigned to anyone.
  * Operations,
    Accounts          — no leads. Operations reaches projects only through execution
                        (require_execution_access, below); Accounts only through
                        payments.

BE-17: these are permissions (config/access_control.yaml), so several roles combine as
a union — `leads.view_all` (Admin, Management, Network Manager, Engineer) beats
`leads.view_assigned` (Sales). A Network Manager holds `leads.view_all` so it can open
any proposal and adjust its price; associate-sourced customers it reaches through
onboarding_lead_scope / require_onboarding_access (`onboardings.view_all`).

Single records go through require_lead_access(); list queries through LeadScope,
which becomes a WHERE clause so filtering never happens after the fact.

Associates see customers they onboarded through the parallel OnboardingScope below
(own customers; a Preferred Associate also their territory's), and a Preferred
Associate sees the associates below them through RosterScope — both built on the one
territory rule, territory_for_level(). Staff scoping above is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, false, func, or_, true

from app.auth.access import viewer_holds
from app.memory.models import Lead

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
    return viewer_holds(employee, "leads.view_all")


def _sees_assigned(employee) -> bool:
    return viewer_holds(employee, "leads.view_assigned")


def narrowed_to_assigned(employee) -> bool:
    """True when this person reads leads only as Sales does (own + unassigned): the
    narrowing every Sales-scoped list applies."""
    return _sees_assigned(employee) and not _sees_everything(employee)


def can_access_lead(employee, lead) -> bool:
    if lead is None:
        return False
    if _sees_everything(employee):
        return True
    if _sees_assigned(employee):
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
        if _sees_everything(employee) or _sees_assigned(employee):
            return LeadScope(unassigned=True)
        return LeadScope()
    if _sees_everything(employee):
        return LeadScope(everything=True)
    if _sees_assigned(employee):
        return LeadScope(employee_id=employee.employee_id, unassigned=True)
    return LeadScope()


def _sees_all_onboardings(employee) -> bool:
    return _sees_everything(employee) or viewer_holds(employee, "onboardings.view_all")


def onboarding_lead_scope(employee) -> LeadScope:
    """Lead scope for associate-sourced customers (onboardings, their surveys): everyone
    lead_scope() allows, plus — for `onboardings.view_all` (Network Manager) — every
    associate-sourced customer."""
    return LeadScope(everything=True) if _sees_all_onboardings(employee) else lead_scope(employee)


def require_onboarding_access(employee, lead, detail: str = NOT_YOURS) -> None:
    if _sees_all_onboardings(employee):
        return
    require_lead_access(employee, lead, detail)


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


def territory_for_level(level: Optional[str], state, district, block):
    """The one territory rule: a State Head's territory is a state, a District Head's
    a district, a block-level Preferred Associate's a block. Picks whichever of the
    three (ids or columns) the level keys on; None for a plain Associate."""
    return {"state_head": state, "district_head": district, "block": block}.get(level or "")


def _own_territory(associate) -> tuple[Optional[str], Optional[int]]:
    level = associate.level.value if associate.level is not None else None
    return level, territory_for_level(level, associate.territory_state_id, associate.territory_district_id,
                                      associate.territory_block_id)


@dataclass(frozen=True)
class OnboardingScope:
    associate_pk: int
    level: Optional[str] = None
    territory_id: Optional[int] = None

    def condition(self):
        from app.memory.models import CustomerOnboarding

        parts = [CustomerOnboarding.owner_associate_id == self.associate_pk]
        column = territory_for_level(self.level, CustomerOnboarding.state_id, CustomerOnboarding.district_id,
                                     CustomerOnboarding.block_id)
        if column is not None and self.territory_id is not None:
            parts.append(column == self.territory_id)
        return or_(*parts)


def onboarding_scope(associate) -> OnboardingScope:
    """`associate` is the Associate row of the signed-in associate."""
    level, territory_id = _own_territory(associate)
    return OnboardingScope(associate_pk=associate.id, level=level, territory_id=territory_id)


def can_view_onboarding(associate, onboarding) -> bool:
    if onboarding is None:
        return False
    scope = onboarding_scope(associate)
    if onboarding.owner_associate_id == scope.associate_pk:
        return True
    site = territory_for_level(scope.level, onboarding.state_id, onboarding.district_id, onboarding.block_id)
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


DOCUMENTS_OWNER_ONLY = "Only the associate who onboarded this customer can open its documents."


def require_onboarding_documents(associate, onboarding) -> None:
    """Uploaded documents, the agreement and execution files: the owner — and a
    territory viewer only when config/associate_onboarding.yaml
    territory_viewers_see_documents is true (an unconfirmed default: false)."""
    from app.onboarding import config as onboarding_config

    require_onboarding_view(associate, onboarding)
    if not can_edit_onboarding(associate, onboarding) and not onboarding_config.territory_viewers_see_documents():
        raise HTTPException(status_code=403, detail=DOCUMENTS_OWNER_ONLY)


# ── Preferred Associate visibility over other associates (territory roster) ─────
#
# The same territory rule applied to associates instead of customer sites: every
# associate located in the caller's territory whose rank is below the caller's
# (State Head > District Head > block-level Preferred > plain Associate). A
# Preferred Associate is located by the territory they hold; a plain Associate —
# who holds none — by the state/district/block of the address they registered with.
# Never a sibling territory, never a peer or superior, never the caller.
#
# Known limitation (README "Known limitations"): home address may not be where a
# plain Associate works. A deliberate business-rule choice, to revisit with real data.

ROSTER_PREFERRED_ONLY = "Only Preferred Associates have a territory roster."

_LEVELS_BELOW = {"state_head": ("district_head", "block"), "district_head": ("block",), "block": ()}


@dataclass(frozen=True)
class RosterScope:
    level: str
    territory_id: int

    def condition(self):
        """A WHERE clause over Associate outer-joined to AssociateApplication (on
        associate_application_id — which only a plain Associate has)."""
        from app.memory.models import Associate, AssociateApplication, AssociateType, TerritoryLevel

        # The join matches only plain Associates, so each coalesce reads territory_*
        # for a Preferred Associate and the registered address for a plain one.
        located = territory_for_level(
            self.level,
            func.coalesce(Associate.territory_state_id, AssociateApplication.state_id),
            func.coalesce(Associate.territory_district_id, AssociateApplication.district_id),
            func.coalesce(Associate.territory_block_id, AssociateApplication.block_id),
        )
        below = [Associate.associate_type == AssociateType.associate]
        if _LEVELS_BELOW[self.level]:
            below.append(and_(Associate.associate_type == AssociateType.preferred_associate,
                              Associate.level.in_([TerritoryLevel(v) for v in _LEVELS_BELOW[self.level]])))
        return and_(located == self.territory_id, or_(*below))


def roster_scope(associate) -> RosterScope:
    """403 for a plain Associate (or a Preferred Associate with no territory, which
    approval never creates)."""
    level, territory_id = _own_territory(associate)
    if level is None or territory_id is None:
        raise HTTPException(status_code=403, detail=ROSTER_PREFERRED_ONLY)
    return RosterScope(level=level, territory_id=territory_id)


# ── Staff visibility over execution (Phase 4A) ────────────────────────────────────
#
#   * Admin, Management, Engineer — every project in execution.
#   * Sales      — its own or unassigned leads (the lead rule above).
#   * Operations — every project at or after agreement_signed (it has an execution
#                  row only from then), no per-person assignment yet. Never money.
#   * Accounts   — none (Accounts reads the payments side instead).

EXECUTION_NOT_VISIBLE = "Your role cannot view this project's execution."


def can_access_execution(employee, lead, has_execution: bool) -> bool:
    if not employee.can_view_execution():
        return False
    if viewer_holds(employee, "execution.view_all"):
        return True
    if viewer_holds(employee, "execution.view_in_progress") and has_execution:
        return True
    return _sees_assigned(employee) and can_access_lead(employee, lead)


def require_execution_access(employee, lead, has_execution: bool) -> None:
    if not can_access_execution(employee, lead, has_execution):
        raise HTTPException(status_code=403, detail=EXECUTION_NOT_VISIBLE)


# ── Staff visibility over payments (Phase 4A) ─────────────────────────────────────
#
#   * Admin, Management, Accounts — every project's payments.
#   * Sales                       — its own or unassigned leads.
#   * Operations, Engineer        — none (no money in their views at all).

PAYMENTS_NOT_VISIBLE = "Your role cannot view this customer's payments."


def require_payment_access(employee, lead) -> None:
    if not employee.can_view_payments():
        raise HTTPException(status_code=403, detail=PAYMENTS_NOT_VISIBLE)
    if not viewer_holds(employee, "payments.view_all") and not can_access_lead(employee, lead):
        raise HTTPException(status_code=403, detail=PAYMENTS_NOT_VISIBLE)


# ── Staff regions (BE-17) ─────────────────────────────────────────────────────────
#
# `employees.regions` lists the states a person works in (empty = every region). It is
# enforced where the record carries a state: associates and associate applications
# (`state_id`), and — BE-19 — leads (list and detail). A lead's state is its associate
# onboarding's site state when it has one, else the project's free-text state when that
# text is exactly a state's name (any case). A lead whose state is unknown (no onboarding,
# and no or unrecognised project state — most chat and web leads) is NOT region-scoped:
# hiding it would hide it from everyone working a region (docs/api/access_control.md).

OUTSIDE_REGION = "This record is outside the regions you work in."


def region_state_ids(db, employee) -> Optional[list[int]]:
    """TerritoryState ids of the employee's regions, or None for every region."""
    from sqlalchemy import select

    from app.memory.models import TerritoryState

    regions = list(getattr(employee, "regions", ()) or ())
    if not regions:
        return None
    return list(db.execute(select(TerritoryState.id).where(TerritoryState.name.in_(regions))).scalars())


def require_region(db, employee, state_id) -> None:
    ids = region_state_ids(db, employee)
    if ids is not None and state_id not in ids:
        raise HTTPException(status_code=403, detail=OUTSIDE_REGION)


def _state_names(db) -> dict[str, int]:
    from sqlalchemy import select

    from app.memory.models import TerritoryState
    return {name.strip().lower(): sid for sid, name in db.execute(select(TerritoryState.id, TerritoryState.name))}


def lead_region_condition(db, employee):
    """SQL condition for the leads inside the employee's regions (unknown-state leads
    included), or None when the employee works every region."""
    from sqlalchemy import select

    from app.memory.models import CustomerOnboarding, Project

    ids = region_state_ids(db, employee)
    if ids is None:
        return None
    names = _state_names(db)
    outside_names = [n for n, sid in names.items() if sid not in ids]
    site_state = (select(CustomerOnboarding.state_id).where(CustomerOnboarding.lead_id == Lead.id)
                  .correlate(Lead).scalar_subquery())
    project_state = (select(func.lower(func.trim(Project.state))).where(Project.id == Lead.project_id)
                     .correlate(Lead).scalar_subquery())
    outside = or_(and_(site_state.is_not(None), site_state.not_in(ids or [-1])),
                  and_(site_state.is_(None), func.coalesce(project_state, "").in_(outside_names or [""])))
    return ~outside


def lead_state_id(db, lead) -> Optional[int]:
    from sqlalchemy import select

    from app.memory.models import CustomerOnboarding, Project
    site = db.execute(select(CustomerOnboarding.state_id).where(CustomerOnboarding.lead_id == lead.id)).scalar()
    if site is not None:
        return site
    project = db.get(Project, lead.project_id) if lead.project_id else None
    text = (project.state or "").strip().lower() if project else ""
    return _state_names(db).get(text) if text else None


def require_lead_region(db, employee, lead) -> None:
    ids = region_state_ids(db, employee)
    if ids is None:
        return
    state_id = lead_state_id(db, lead)
    if state_id is not None and state_id not in ids:
        raise HTTPException(status_code=403, detail=OUTSIDE_REGION)
