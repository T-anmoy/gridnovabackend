"""Live and archived projects as ONE SQL selectable, so Nova Records filters, counts and
sums them in the database — never in Python, never in the model.

Each column mirrors what app/projects/view.py shows for the same field, from the same
tables (BE-8):

  live     ref = projects.reference_id; state/district from the onboarding's territory
           (else projects.state); commissioning_date = the UTC date `commissioned` was
           recorded; makes from the installed-unit register; contract value = the first
           lead's latest accepted proposal; received = the payment ledger (associate-
           sourced projects); stage = the furthest recorded stage; block = the
           onboarding's territory block (null when the onboarding has none).
  archive  the archived_projects row as imported; state/district/block names from the
           territory master where they matched, else the text as sent. Received and
           outstanding are not on record for archived projects.

The caller's row scoping is part of the live half's WHERE (Sales: projects whose first
lead is theirs or unassigned; Operations: projects in execution — view.can_view_live),
so a count can never include a project the caller could not open.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, Integer, Numeric, String, and_, case, cast, exists, false, func, literal, or_, select, \
    type_coerce, union_all
from sqlalchemy.orm import Session

from app.memory.models import (
    ArchivedProject, Associate, Customer, CustomerOnboarding, Equipment, InstalledUnit,
    InstalledUnitCategory, InstalledUnitStatus, Lead, PaymentLedgerEntry, Project, ProjectExecution, ProjectStage,
    ProjectStageEvent, ProjectWarranty, Proposal, ProposalInputs, ProposalStatus, TerritoryBlock, TerritoryDistrict,
    TerritoryState, WarrantyItem,
)
from app.onboarding.progress import ORDER as STAGE_ORDER
from app.auth.access import live_project_rule
from app.records import access

KW = Numeric(12, 3)
MONEY = Numeric(14, 2)

# Archived projects are completed: these stages are on record for them through a date.
ARCHIVE_STAGE_DATES = {ProjectStage.agreement_signed: "order_date", ProjectStage.installed: "installation_date",
                       ProjectStage.inspection_net_meter: "net_meter_date",
                       ProjectStage.commissioned: "commissioning_date"}


def _pg(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _utc_date(column, pg: bool):
    if pg:
        return cast(func.timezone("UTC", column), Date)
    return type_coerce(func.date(column), Date)


def _makes(project_id, category: InstalledUnitCategory, pg: bool):
    """(first make alphabetically — the group key, every make joined — the search text)."""
    make = func.coalesce(Equipment.brand, InstalledUnit.brand_as_printed)
    base = (select(func.min(make)).select_from(InstalledUnit)
            .outerjoin(Equipment, Equipment.id == InstalledUnit.equipment_id)
            .where(InstalledUnit.project_id == project_id, InstalledUnit.category == category,
                   InstalledUnit.status != InstalledUnitStatus.voided))
    joined = func.string_agg(make, literal(",")) if pg else func.group_concat(make, ",")
    every = base.with_only_columns(joined)
    return base.scalar_subquery(), every.scalar_subquery()


def _live(db: Session, viewer):
    pg = _pg(db)
    P, O, PI = Project, CustomerOnboarding, ProposalInputs
    first_lead = (select(Lead.id).where(Lead.project_id == P.id).order_by(Lead.id).limit(1)
                  .correlate(P).scalar_subquery())
    first_assignee = (select(Lead.assigned_employee_id).where(Lead.project_id == P.id).order_by(Lead.id).limit(1)
                      .correlate(P).scalar_subquery())
    commissioned = (select(func.min(ProjectStageEvent.recorded_at))
                    .where(ProjectStageEvent.project_id == P.id, ProjectStageEvent.stage == ProjectStage.commissioned)
                    .scalar_subquery())
    rank = case(*[(ProjectStageEvent.stage == s, i) for i, s in enumerate(STAGE_ORDER)], else_=-1)
    furthest = select(func.max(rank)).where(ProjectStageEvent.project_id == P.id).scalar_subquery()
    stage = case(*[(furthest == i, s.value) for i, s in enumerate(STAGE_ORDER)], else_=None)
    module_make, module_makes = _makes(P.id, InstalledUnitCategory.module, pg)
    inverter_make, inverter_makes = _makes(P.id, InstalledUnitCategory.inverter, pg)
    contract = (select(Proposal.total_value).where(Proposal.lead_id == first_lead,
                                                   Proposal.status == ProposalStatus.accepted)
                .order_by(Proposal.version.desc(), Proposal.id.desc()).limit(1).scalar_subquery())
    contract = case((contract > 0, cast(contract, MONEY)), else_=None)
    ledger = select(func.coalesce(func.sum(PaymentLedgerEntry.amount), 0)).where(
        PaymentLedgerEntry.project_id == P.id).scalar_subquery()
    received = case((O.id.isnot(None), cast(ledger, MONEY)), else_=None)

    def warranty_end(category: str):
        return (select(func.max(WarrantyItem.warranty_end)).join(ProjectWarranty,
                                                                  ProjectWarranty.id == WarrantyItem.warranty_id)
                .where(ProjectWarranty.project_id == P.id, WarrantyItem.category == category).scalar_subquery())

    scheme_name = PI.scheme_name
    family = P.project_family
    scheme = case(
        (scheme_name == "pm_surya_ghar", "pm_surya_ghar_residential"),
        (scheme_name.in_(("pm_kusum_a", "pm_kusum_b", "pm_kusum_c")), "pm_kusum_pump"),
        (scheme_name == "wbreda", "wbreda_school"),
        (and_(family == "rooftop_solar", PI.site_type == "rooftop_commercial"), "commercial"),
        (family == "ground_mounted", "ground_mounted"),
        (family == "government_institutional", "institutional_govt"),
        (family == "street_lighting", "street_lighting"),
        else_=None)
    has_execution = exists().where(ProjectExecution.project_id == P.id)
    query = (
        select(
            literal("live").label("source"), P.id.label("id"), P.reference_id.label("ref"),
            type_coerce(scheme, String).label("scheme"),
            cast(P.capacity_kw, KW).label("capacity_kw"),
            func.coalesce(TerritoryState.name, P.state).label("state"), P.state.label("state_raw"),
            TerritoryDistrict.name.label("district"), cast(literal(None), String).label("district_raw"),
            TerritoryBlock.name.label("block"),
            func.coalesce(PI.full_address, P.location).label("address"),
            Customer.name.label("customer_name"),
            O.site_latitude.label("latitude"), O.site_longitude.label("longitude"),
            _utc_date(commissioned, pg).label("commissioning_date"),
            _utc_date(func.coalesce(O.created_at, P.created_at), pg).label("created_on"),
            module_make.label("module_make"), module_makes.label("module_makes"),
            inverter_make.label("inverter_make"), inverter_makes.label("inverter_makes"),
            Associate.associate_id.label("associate_code"), Associate.full_name.label("associate_name"),
            contract.label("contract_value"), received.label("received"),
            (contract - received).label("outstanding"),
            type_coerce(stage, String).label("stage"),
            warranty_end("module").label("warranty_end_modules"),
            warranty_end("inverter").label("warranty_end_inverter"),
            O.id.label("onboarding_id"), first_lead.label("lead_id"),
        )
        .select_from(P)
        .outerjoin(O, O.project_id == P.id)
        .outerjoin(PI, PI.id == O.proposal_inputs_id)
        .outerjoin(TerritoryState, TerritoryState.id == O.state_id)
        .outerjoin(TerritoryDistrict, TerritoryDistrict.id == O.district_id)
        .outerjoin(TerritoryBlock, TerritoryBlock.id == O.block_id)
        .outerjoin(Customer, Customer.id == P.customer_id)
        .outerjoin(Associate, Associate.id == O.owner_associate_id)
    )
    # Every lead has a projects row from its first message (MemoryManager.create_lead);
    # it is a project on record once it has an onboarding, a recorded stage or an
    # accepted proposal. Earlier than that it is a lead (lead_lookup), not a project.
    accepted_any = exists().where(Proposal.lead_id == Lead.id, Lead.project_id == P.id,
                                  Proposal.status == ProposalStatus.accepted)
    query = query.where(or_(O.id.isnot(None), exists().where(ProjectStageEvent.project_id == P.id), accepted_any))
    rule = live_project_rule(viewer)
    if rule == "assigned":
        query = query.where(first_lead.isnot(None), or_(first_assignee.is_(None),
                                                        first_assignee == getattr(viewer, "employee_id", None)))
    elif rule == "execution":
        query = query.where(has_execution)
    elif rule == "none":
        query = query.where(false())
    return query


def _archive():
    A = ArchivedProject
    return (
        select(
            literal("archive").label("source"), A.id.label("id"),
            func.coalesce(A.grid_nova_ref, literal("ARC-") + cast(A.id, String)).label("ref"),
            cast(A.scheme_or_segment, String).label("scheme"),
            cast(A.capacity_kw, KW).label("capacity_kw"),
            func.coalesce(TerritoryState.name, A.state).label("state"), A.state.label("state_raw"),
            func.coalesce(TerritoryDistrict.name, A.district).label("district"), A.district.label("district_raw"),
            func.coalesce(TerritoryBlock.name, A.block).label("block"),
            A.address_text.label("address"), A.customer_name.label("customer_name"),
            A.latitude.label("latitude"), A.longitude.label("longitude"),
            A.commissioning_date.label("commissioning_date"),
            cast(literal(None), Date).label("created_on"),
            A.module_make.label("module_make"), A.module_make.label("module_makes"),
            A.inverter_make.label("inverter_make"), A.inverter_make.label("inverter_makes"),
            cast(literal(None), String).label("associate_code"), cast(literal(None), String).label("associate_name"),
            cast(A.contract_value_inr, MONEY).label("contract_value"), cast(literal(None), MONEY).label("received"),
            cast(literal(None), MONEY).label("outstanding"),
            cast(literal(ProjectStage.commissioned.value), String).label("stage"),
            A.warranty_end_modules.label("warranty_end_modules"),
            A.warranty_end_inverter.label("warranty_end_inverter"),
            cast(literal(None), Integer).label("onboarding_id"), cast(literal(None), Integer).label("lead_id"),
        )
        .select_from(A)
        .outerjoin(TerritoryState, TerritoryState.id == A.state_id)
        .outerjoin(TerritoryDistrict, TerritoryDistrict.id == A.district_id)
        .outerjoin(TerritoryBlock, TerritoryBlock.id == A.block_id)
        .where(A.voided_at.is_(None))
    )


def unified(db: Session, viewer, source: str = "both"):
    """The subquery every project tool reads: one row per project the viewer may see."""
    parts = []
    if source in ("live", "both"):
        parts.append(_live(db, viewer))
    if source in ("archive", "both"):
        parts.append(_archive())
    return (union_all(*parts) if len(parts) > 1 else parts[0]).subquery("records_projects")


# ── Filters ──────────────────────────────────────────────────────────────────────

def _like(column, term: str):
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.lower(column).like(f"%{escaped}%", escape="\\")


def _named(column, raw, name: str):
    return or_(func.lower(column) == name.lower(), func.lower(raw) == name.lower()) if raw is not None \
        else func.lower(column) == name.lower()


def conditions(u, f, viewer) -> list:
    """WHERE parts for ProjectFilters `f` over subquery `u`. Field permission is checked
    by the caller (tools.check_filters) before this runs."""
    c = u.c
    out = []
    if f.ref:
        out.append(func.upper(c.ref) == f.ref.strip().upper())
    if f.state:
        out.append(_named(c.state, c.state_raw, f.state))
    if f.district:
        out.append(_named(c.district, c.district_raw, f.district))
    if f.block:
        out.append(_like(c.block, f.block))
    if f.scheme:
        out.append(c.scheme == f.scheme)
    if f.capacity_min_kw is not None:
        out.append(c.capacity_kw >= f.capacity_min_kw)
    if f.capacity_max_kw is not None:
        out.append(c.capacity_kw <= f.capacity_max_kw)
    if f.commissioned_from:
        out.append(c.commissioning_date >= f.commissioned_from)
    if f.commissioned_to:
        out.append(c.commissioning_date <= f.commissioned_to)
    if f.created_from:
        out.append(c.created_on >= f.created_from)
    if f.created_to:
        out.append(c.created_on <= f.created_to)
    if f.module_make:
        out.append(_like(c.module_makes, f.module_make))
    if f.inverter_make:
        out.append(_like(c.inverter_makes, f.inverter_make))
    if f.associate:
        out.append(or_(func.upper(c.associate_code) == f.associate.strip().upper(), _like(c.associate_name, f.associate)))
    if f.customer_name:
        out.append(_like(c.customer_name, f.customer_name))
    if f.stage:
        out.append(_reached(u, ProjectStage(f.stage)))
    if f.text:
        seen = access.fields(viewer)
        parts = [_like(c.ref, f.text)]
        for name, column in (("customer_name", c.customer_name), ("address_text", c.address), ("block", c.block),
                             ("district", c.district), ("state", c.state)):
            if name in seen:
                parts.append(_like(column, f.text))
        out.append(or_(*parts))
    return out


def _reached(u, stage: ProjectStage):
    """Live: the stage is recorded. Archive: the stage's date is on record (an archived
    project is commissioned by definition; other stages are not on record for it)."""
    live = and_(u.c.source == "live", exists().where(ProjectStageEvent.project_id == u.c.id,
                                                     ProjectStageEvent.stage == stage))
    column = ARCHIVE_STAGE_DATES.get(stage)
    if column is None:
        return live
    archived = and_(u.c.source == "archive", exists().where(ArchivedProject.id == u.c.id,
                                                            getattr(ArchivedProject, column).isnot(None)))
    return or_(live, archived)


# ── Queries ──────────────────────────────────────────────────────────────────────

def ordered(u):
    return (u.c.commissioning_date.is_(None), u.c.commissioning_date.desc(), u.c.source, u.c.id.desc())


def search(db: Session, u, where: list, limit: int) -> tuple[list, int]:
    total = db.execute(select(func.count()).select_from(u).where(*where)).scalar_one()
    rows = db.execute(select(u).where(*where).order_by(*ordered(u)).limit(limit)).mappings().all()
    return [dict(r) for r in rows], int(total)


def with_coordinates(db: Session, u, where: list, cap: int) -> list:
    rows = db.execute(select(u).where(*where, u.c.latitude.isnot(None), u.c.longitude.isnot(None))
                      .order_by(*ordered(u)).limit(cap)).mappings().all()
    return [dict(r) for r in rows]


def group_key(db: Session, u, group_by: str):
    c = u.c
    if group_by == "month":
        d = c.commissioning_date
        return func.to_char(d, "YYYY-MM") if _pg(db) else func.strftime("%Y-%m", d)
    if group_by == "year":
        return cast(func.extract("year", c.commissioning_date), String) if _pg(db) else \
            func.strftime("%Y", c.commissioning_date)
    return {"state": c.state, "district": c.district, "block": c.block, "scheme": c.scheme, "associate": c.associate_code,
            "stage": c.stage, "module_make": c.module_make, "inverter_make": c.inverter_make}[group_by]


def metric_expr(u, metric: str):
    c = u.c
    return {"count": func.count(), "sum_capacity_kw": func.sum(c.capacity_kw),
            "avg_capacity_kw": func.avg(c.capacity_kw), "sum_contract_value": func.sum(c.contract_value),
            "sum_received": func.sum(c.received), "sum_outstanding": func.sum(c.outstanding)}[metric]


def plain_number(value, metric: str) -> Optional[str]:
    """SQL's value as a decimal string: money to the paisa, kW to the watt, counts whole."""
    if value is None:
        return None
    if metric == "count":
        return str(int(value))
    quantum = Decimal("0.01") if metric in ("sum_contract_value", "sum_received", "sum_outstanding") else Decimal("0.001")
    return str(Decimal(str(value)).quantize(quantum))


def aggregate(db: Session, u, where: list, metric: str, group_by: Optional[str], limit: int) -> dict:
    value = metric_expr(u, metric)
    total = db.execute(select(value, func.count()).select_from(u).where(*where)).one()
    out = {"metric": metric, "group_by": group_by, "total": plain_number(total[0], metric),
           "projects": int(total[1]), "groups": []}
    if group_by:
        key = group_key(db, u, group_by).label("k")
        q = (select(key, value.label("v"), func.count().label("n")).select_from(u).where(*where)
             .group_by(key).order_by(value.desc(), key).limit(limit))
        if group_by == "associate":
            q = q.add_columns(func.min(u.c.associate_name).label("name"))
        for r in db.execute(q).mappings():
            item = {"group": None if r["k"] is None else str(r["k"]), "value": plain_number(r["v"], metric),
                    "projects": int(r["n"])}
            if group_by == "associate":
                item["associate_name"] = r["name"]
            out["groups"].append(item)
    return out

