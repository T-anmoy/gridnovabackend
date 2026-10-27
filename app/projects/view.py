"""One read layer over every project Grid Nova has done (BE-8): live projects (Project +
execution + payments + handover) and archived ones (ArchivedProject) come out in the SAME
JSON shape, so the assistant (BE-9) and the dashboard need not care which is which.

    {"source": "live" | "archive", "ref": "GNE-…" | grid_nova_ref | "ARC-<id>", "id": int,
     "fields":   {name: {"value", "source_table", "record_id"}},
     "sections": {name: [{"value", "source_table", "record_id"}, …]}}        # dossier only

Every value carries its provenance so that an answer can cite the table and row it came
from. `record_id` is a row id, or a list of ids where the value is an aggregate (units
grouped by make and model). Money is a decimal string; dates are ISO; times are UTC ISO.

Role redaction happens HERE, from config/archive.yaml (app/archive/config.py): a field or
section the role may not see is absent, not null. A live project is additionally subject to
today's scoping — Sales only its own or unassigned leads, Operations only projects in
execution (ViewForbidden otherwise). Callers pass the signed-in employee; they never
redact themselves.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.archive import config
from app.auth import access
from app.memory.models import (
    Associate, ArchivedProject, Customer, CustomerOnboarding, Equipment, InstalledUnit,
    InstalledUnitCategory, InstalledUnitStatus, Lead, Project, ProjectExecution, ProjectStageEvent, ProjectWarranty,
    Proposal, ProposalInputs, ProposalStatus, ServiceRequest, ServiceRequestStatus, TerritoryBlock, TerritoryDistrict,
    TerritoryState, WarrantyItem,
)

CARD_FIELDS = ("scheme_or_segment", "system_type", "capacity_kw", "pump_hp", "street_lamp_count", "state", "district",
               "block", "commissioning_date", "module_make", "inverter_make", "customer_name")
SECTION_GROUPS = {"equipment": "technical", "stages": "dates", "warranty": "dates", "service_requests": "service",
                  "documents": "service"}
OPEN_REQUESTS = (ServiceRequestStatus.open, ServiceRequestStatus.in_progress)
_STAGE_DATES = (("order_date", "agreement_signed"), ("installation_date", "installed"),
                ("commissioning_date", "commissioned"), ("net_meter_date", "inspection_net_meter"))

Subject = Union[Project, ArchivedProject]


class ViewForbidden(PermissionError):
    """The viewer's role may not see this live project (today's scoping rules)."""


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def prov(value: Any, table: str, record_id: Any) -> dict:
    return {"value": _plain(value), "source_table": table, "record_id": record_id}


def ref_of(subject: Subject) -> str:
    if isinstance(subject, Project):
        return subject.reference_id
    return subject.grid_nova_ref or f"ARC-{subject.id}"


def source_of(subject: Subject) -> str:
    return "live" if isinstance(subject, Project) else "archive"


def find_subject(db: Session, ref: str) -> Optional[Subject]:
    """Resolve a reference: GNE-… (live), ARC-<id>, or a Grid Nova ref. Hidden (voided)
    archive rows are never found."""
    ref = (ref or "").strip()
    if not ref:
        return None
    project = db.execute(select(Project).where(Project.reference_id == ref)).scalars().first()
    if project is not None:
        return project
    live = ArchivedProject.voided_at.is_(None)
    row = db.execute(select(ArchivedProject).where(ArchivedProject.grid_nova_ref == ref, live)).scalars().first()
    if row is None and (m := re.fullmatch(r"ARC-(\d+)", ref, re.I)):
        row = db.execute(select(ArchivedProject).where(ArchivedProject.id == int(m[1]), live)).scalars().first()
    return row


# ── Redaction ────────────────────────────────────────────────────────────────────

def _redact(fields: dict, viewer) -> dict:
    allowed = config.visible_fields(viewer)
    return {k: v for k, v in fields.items() if k in allowed}


def _sections_for(sections: dict, viewer) -> dict:
    held = config.groups_for(viewer)
    return {k: v for k, v in sections.items() if SECTION_GROUPS[k] in held}


# ── Archived projects ─────────────────────────────────────────────────────────────

ARCHIVE_FIELDS = (
    "scheme_or_segment", "system_type", "capacity_kw", "pump_hp", "street_lamp_count", "state", "district", "block",
    "address_text", "pincode", "latitude", "longitude", "customer_name", "customer_phone", "customer_email", "discom",
    "consumer_number", "contract_value_inr", "subsidy_amount_inr", "module_make", "module_model",
    "module_wattage_wp", "module_count", "module_dcr", "inverter_make", "inverter_model", "inverter_capacity_kw",
    "inverter_count", "structure_type", "battery_details", "other_equipment", "order_date", "installation_date",
    "commissioning_date", "net_meter_date", "warranty_end_modules", "warranty_end_inverter", "om_status", "notes",
)


def _archive_fields(row: ArchivedProject) -> dict:
    t = "archived_projects"
    fields = {name: prov(getattr(row, name), t, row.id) for name in ARCHIVE_FIELDS}
    fields["documents"] = prov(row.documents or [], t, row.id)
    fields["data_quality_flags"] = prov(row.data_quality_flags or [], t, row.id)
    fields["import_batch_id"] = prov(row.import_batch_id, t, row.id)
    fields["source_row"] = prov(row.source_row, t, row.id)
    return fields


def _archive_sections(row: ArchivedProject) -> dict:
    t = "archived_projects"
    equipment = []
    if row.module_make or row.module_model or row.module_count:
        equipment.append(prov({"category": "module", "make": row.module_make, "model": row.module_model,
                               "rating": row.module_wattage_wp, "count": row.module_count, "dcr": row.module_dcr},
                              t, row.id))
    if row.inverter_make or row.inverter_model or row.inverter_count:
        equipment.append(prov({"category": "inverter", "make": row.inverter_make, "model": row.inverter_model,
                               "rating": row.inverter_capacity_kw, "count": row.inverter_count, "dcr": None},
                              t, row.id))
    stages = [prov({"stage": stage, "date": getattr(row, col)}, t, row.id)
              for col, stage in _STAGE_DATES if getattr(row, col) is not None]
    warranty = [prov({"handover_date": None, "system_warranty_end": None, "om_free_until": None,
                      "module_warranty_end": row.warranty_end_modules,
                      "inverter_warranty_end": row.warranty_end_inverter}, t, row.id)] \
        if row.warranty_end_modules or row.warranty_end_inverter else []
    return {"equipment": equipment, "stages": stages, "warranty": warranty, "service_requests": [],
            "documents": [prov(d, t, row.id) for d in row.documents or []]}


# ── Live projects ─────────────────────────────────────────────────────────────────

def _live_context(db: Session, project: Project) -> dict:
    lead = db.execute(select(Lead).where(Lead.project_id == project.id).order_by(Lead.id)).scalars().first()
    onboarding = db.execute(select(CustomerOnboarding).where(CustomerOnboarding.project_id == project.id)).scalars().first()
    execution = db.execute(select(ProjectExecution).where(ProjectExecution.project_id == project.id)).scalars().first()
    return {"lead": lead, "onboarding": onboarding, "execution": execution}


def can_view_live(viewer, ctx: dict) -> bool:
    """Today's rules: Sales its own or unassigned leads; Operations only projects in
    execution; a Network Manager none; everyone else who may read the archive reads live
    projects too. Several roles: the widest (app/auth/access.py live_project_rule)."""
    rule = access.live_project_rule(viewer)
    if rule == "assigned":
        lead = ctx["lead"]
        return lead is not None and lead.assigned_employee_id in (None, getattr(viewer, "employee_id", None))
    if rule == "execution":
        return ctx["execution"] is not None
    return rule == "all"


def _aggregate_units(db: Session, project_id: int) -> list[dict]:
    units = list(db.execute(select(InstalledUnit).where(
        InstalledUnit.project_id == project_id, InstalledUnit.status != InstalledUnitStatus.voided)
        .order_by(InstalledUnit.id)).scalars())
    catalogue = {e.id: e for e in db.execute(select(Equipment).where(
        Equipment.id.in_({u.equipment_id for u in units if u.equipment_id}))).scalars()} if units else {}
    groups: dict = {}
    for u in units:
        item = catalogue.get(u.equipment_id)
        make = item.brand if item else u.brand_as_printed
        model = item.model_name if item else u.model_as_printed
        rating = u.rating_wp if u.category == InstalledUnitCategory.module else u.rating_kw
        key = (u.category.value, make, model, rating)
        g = groups.setdefault(key, {"ids": [], "dcr": []})
        g["ids"].append(u.id)
        g["dcr"].append(u.is_dcr)
    out = []
    for (category, make, model, rating), g in groups.items():
        dcr = None if category != "module" else ("yes" if all(g["dcr"]) else "no" if not any(g["dcr"]) else "unknown")
        out.append(prov({"category": category, "make": make, "model": model, "rating": rating,
                         "count": len(g["ids"]), "dcr": dcr}, "installed_units", g["ids"]))
    return out


def _live_scheme(project: Project, inputs: Optional[ProposalInputs]) -> Optional[str]:
    scheme = inputs.scheme_name.value if inputs is not None and inputs.scheme_name is not None else None
    if scheme == "pm_surya_ghar":
        return "pm_surya_ghar_residential"
    if scheme in ("pm_kusum_a", "pm_kusum_b", "pm_kusum_c"):
        return "pm_kusum_pump"
    if scheme == "wbreda":
        return "wbreda_school"
    family = project.project_family.value if project.project_family else None
    if family == "rooftop_solar":
        return "commercial" if inputs is not None and inputs.site_type.value == "rooftop_commercial" else None
    return {"ground_mounted": "ground_mounted", "government_institutional": "institutional_govt",
            "street_lighting": "street_lighting"}.get(family or "")


def _live_fields(db: Session, project: Project, ctx: dict) -> tuple[dict, dict]:
    from app.onboarding import progress
    from app.payments import service as payments

    lead, onboarding, execution = ctx["lead"], ctx["onboarding"], ctx["execution"]
    customer = db.get(Customer, project.customer_id)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id) if onboarding else None
    pt, ot = "projects", "customer_onboardings"
    fields: dict = {}

    def put(name, value, table, rid):
        fields[name] = prov(value, table, rid)

    state_row = db.get(TerritoryState, onboarding.state_id) if onboarding is not None else None
    district_row = db.get(TerritoryDistrict, onboarding.district_id) if onboarding is not None else None
    state_name = state_row.name if state_row else None
    district_name = district_row.name if district_row else None
    put("scheme_or_segment", _live_scheme(project, inputs), pt, project.id)
    put("system_type", getattr(getattr(inputs, "connection_type", None), "value", None), "proposal_inputs",
        inputs.id if inputs else None)
    put("capacity_kw", Decimal(str(project.capacity_kw)) if project.capacity_kw is not None else None, pt, project.id)
    put("pump_hp", None, pt, project.id)
    put("street_lamp_count", None, pt, project.id)
    put("state", state_name or project.state, ot if state_name else pt, onboarding.id if state_name else project.id)
    put("district", district_name or (inputs.district if inputs else None), ot if district_name else "proposal_inputs",
        onboarding.id if district_name else (inputs.id if inputs else None))
    # The onboarding's territory block, like the archive's block; none on record → null (never guessed).
    block_row = db.get(TerritoryBlock, onboarding.block_id) if onboarding is not None and onboarding.block_id else None
    put("block", block_row.name if block_row else None, ot, onboarding.id if onboarding else None)
    put("address_text", (inputs.full_address if inputs and inputs.full_address else project.location),
        "proposal_inputs" if inputs and inputs.full_address else pt, inputs.id if inputs and inputs.full_address else project.id)
    put("pincode", inputs.pin_code if inputs else None, "proposal_inputs", inputs.id if inputs else None)
    put("latitude", onboarding.site_latitude if onboarding else None, ot, onboarding.id if onboarding else None)
    put("longitude", onboarding.site_longitude if onboarding else None, ot, onboarding.id if onboarding else None)
    put("customer_name", customer.name if customer else None, "customers", customer.id if customer else None)
    put("customer_phone", customer.phone if customer else None, "customers", customer.id if customer else None)
    put("customer_email", customer.email if customer else None, "customers", customer.id if customer else None)
    put("discom", inputs.discom if inputs else None, "proposal_inputs", inputs.id if inputs else None)
    put("consumer_number", inputs.consumer_number if inputs else None, "proposal_inputs", inputs.id if inputs else None)

    # money: the accepted proposal's total, and what the ledger holds against it
    proposal = db.execute(select(Proposal).where(Proposal.lead_id == lead.id, Proposal.status == ProposalStatus.accepted)
                          .order_by(Proposal.version.desc(), Proposal.id.desc())).scalars().first() if lead else None
    total = payments.money(proposal.total_value) if proposal is not None and proposal.total_value else None
    received = payments.paid(db, project.id) if onboarding is not None else None
    put("contract_value_inr", total, "proposals", proposal.id if proposal else None)
    put("subsidy_amount_inr", None, "proposals", proposal.id if proposal else None)
    put("amount_received_inr", received, "payment_ledger_entries", project.id)
    put("amount_outstanding_inr", (total - received) if total is not None and received is not None else None,
        "payment_ledger_entries", project.id)

    # equipment, flat — from the installed-unit register
    units = _aggregate_units(db, project.id)
    for category, prefix in (("module", "module"), ("inverter", "inverter")):
        rows = [u for u in units if u["value"]["category"] == category]
        ids = [i for u in rows for i in u["record_id"]]
        makes = sorted({u["value"]["make"] for u in rows if u["value"]["make"]})
        models = sorted({u["value"]["model"] for u in rows if u["value"]["model"]})
        put(f"{prefix}_make", ", ".join(makes) or None, "installed_units", ids or None)
        put(f"{prefix}_model", ", ".join(models) or None, "installed_units", ids or None)
        put(f"{prefix}_count", sum(u["value"]["count"] for u in rows) or None, "installed_units", ids or None)
        ratings = {u["value"]["rating"] for u in rows if u["value"]["rating"]}
        put("module_wattage_wp" if category == "module" else "inverter_capacity_kw",
            next(iter(ratings)) if len(ratings) == 1 else None, "installed_units", ids or None)
    modules = [u for u in units if u["value"]["category"] == "module"]
    put("module_dcr", (modules[0]["value"]["dcr"] if len(modules) == 1 else ("unknown" if modules else None)),
        "installed_units", [i for u in modules for i in u["record_id"]] or None)
    for name in ("structure_type", "battery_details", "other_equipment"):
        put(name, None, "installed_units", None)

    # dates and stages
    events = {e.stage.value: e for e in db.execute(select(ProjectStageEvent).where(
        ProjectStageEvent.project_id == project.id).order_by(ProjectStageEvent.recorded_at)).scalars()}
    for col, stage in _STAGE_DATES:
        e = events.get(stage)
        put(col, e.recorded_at.date() if e else None, "project_stage_events", e.id if e else None)
    warranty = db.execute(select(ProjectWarranty).where(ProjectWarranty.project_id == project.id)).scalars().first()
    items = list(db.execute(select(WarrantyItem).where(WarrantyItem.warranty_id == warranty.id)).scalars()) if warranty else []
    for name, category in (("warranty_end_modules", "module"), ("warranty_end_inverter", "inverter")):
        ends = [i.warranty_end for i in items if i.category == category and i.warranty_end]
        put(name, max(ends) if ends else None, "warranty_items", [i.id for i in items if i.category == category] or None)
    current = progress.timeline(db, onboarding)["current_stage"] if onboarding else None
    put("current_stage", current, "project_stage_events", events[current].id if current in events else None)

    # service
    requests = list(db.execute(select(ServiceRequest).where(ServiceRequest.project_id == project.id,
                                                            ServiceRequest.status.in_(OPEN_REQUESTS))
                               .order_by(ServiceRequest.id)).scalars())
    put("om_status", None, "service_requests", None)
    put("notes", None, pt, project.id)
    put("documents", [], pt, project.id)
    put("data_quality_flags", [], pt, project.id)
    put("open_service_requests", len(requests), "service_requests", [r.id for r in requests])
    associate = db.get(Associate, onboarding.owner_associate_id) if onboarding else None
    put("associate_id", associate.associate_id if associate else None, "associates", associate.id if associate else None)
    put("associate_name", associate.full_name if associate else None, "associates", associate.id if associate else None)

    stages = [prov({"stage": e.stage.value, "date": e.recorded_at.date()}, "project_stage_events", e.id)
              for e in events.values()]
    warranty_section = [prov({"handover_date": warranty.handover_date, "system_warranty_end": warranty.system_warranty_end,
                              "om_free_until": warranty.om_free_until,
                              "module_warranty_end": fields["warranty_end_modules"]["value"],
                              "inverter_warranty_end": fields["warranty_end_inverter"]["value"]},
                             "project_warranties", warranty.id)] if warranty else []
    sections = {"equipment": units, "stages": stages, "warranty": warranty_section,
                "service_requests": [prov({"category": r.category.value, "status": r.status.value,
                                           "raised": r.created_at, "within_warranty": r.within_warranty},
                                          "service_requests", r.id) for r in requests],
                "documents": []}
    return fields, sections


# ── The two public functions ──────────────────────────────────────────────────────

def _build(db: Session, subject: Subject, viewer) -> tuple[dict, dict]:
    if isinstance(subject, ArchivedProject):
        if subject.voided_at is not None:
            raise ViewForbidden("This archived project is not available.")
        return _archive_fields(subject), _archive_sections(subject)
    ctx = _live_context(db, subject)
    if not can_view_live(viewer, ctx):
        raise ViewForbidden("Your role cannot view this project.")
    return _live_fields(db, subject, ctx)


def project_card(db: Session, subject: Subject, viewer) -> dict:
    """The short form (lists, search results), redacted for `viewer`'s role."""
    fields, _ = _build(db, subject, viewer)
    return {"source": source_of(subject), "ref": ref_of(subject), "id": subject.id,
            "fields": _redact({k: v for k, v in fields.items() if k in CARD_FIELDS}, viewer)}


def project_dossier(db: Session, subject: Subject, viewer) -> dict:
    """Everything on record, redacted for `viewer`'s role."""
    fields, sections = _build(db, subject, viewer)
    return {"source": source_of(subject), "ref": ref_of(subject), "id": subject.id,
            "fields": _redact(fields, viewer), "sections": _sections_for(sections, viewer)}
