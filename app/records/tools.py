"""Nova Records' read-only tools (BE-9).

Every tool takes the database session, the CALLER (the signed-in employee: `role`,
`employee_id`) and Pydantic-validated arguments, and returns

    {"tool", "kind": "rows" | "record" | "aggregate" | "candidates",
     "rows" | "record" | "aggregate" | "candidates": …,
     "refs": [{"ref", "kind", "label", "path"?}], "searched": {filter: value}, …}

Role scoping (app/auth/scoping.py, app/projects/view.py) and field redaction
(config/archive.yaml) happen HERE, before anything reaches a prompt or a response:
nothing above this layer filters. A filter on a field the caller may not see is refused
(ToolError `filter_not_permitted`) rather than silently dropped — a search on a hidden
field would otherwise reveal it by its matches. Nothing here writes.

Customer-written free text (archive notes and O&M status, service-request descriptions,
lead fields typed in chat) is returned under `untrusted` so the agent can quote it as data
and keep it out of the facts an answer may be checked against.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import geo
from app.memory.models import (
    ArchivedProject, ArchiveScheme, Associate, AssociateApplication, CommissionEntry, CommissionEntryStatus,
    Customer, CustomerOnboarding, Equipment, EquipmentCategory, Lead, LeadSource, OnboardingStatus, PipelineStage, Project,
    ProjectStage, ServiceRequest, ServiceRequestStatus, TerritoryDistrict,
)
from app.projects import view
from app.records import access, projects
from app.records import config as records_config

SCHEMES = tuple(s.value for s in ArchiveScheme)
STAGES = tuple(s.value for s in ProjectStage)
UNTRUSTED_PROJECT_FIELDS = ("notes", "om_status", "address_text", "battery_details", "other_equipment")


class ToolError(Exception):
    """A request the tool refuses: `code` is filter_not_permitted | not_permitted |
    invalid_argument."""

    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def _rows_cap() -> int:
    return records_config.limit("max_rows")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Near(_Args):
    """A radius search: explicit coordinates (they must appear in the question — the
    agent checks), or `place`, matched against project addresses, blocks and districts on
    record (never geocoded)."""
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    place: Optional[str] = Field(None, min_length=2, max_length=100)
    km: Optional[float] = Field(None, gt=0, le=100)

    @model_validator(mode="after")
    def _one_centre(self):
        has_point = self.lat is not None and self.lng is not None
        if has_point == bool(self.place) or (self.lat is None) != (self.lng is None):
            raise ValueError("give either lat and lng, or place")
        return self


class ProjectFilters(_Args):
    source: Literal["live", "archive", "both"] = "both"
    text: Optional[str] = Field(None, min_length=1, max_length=200)
    ref: Optional[str] = Field(None, min_length=1, max_length=100)
    customer_name: Optional[str] = Field(None, min_length=2, max_length=200)
    state: Optional[str] = Field(None, max_length=120)
    district: Optional[str] = Field(None, max_length=120)
    block: Optional[str] = Field(None, max_length=120)
    scheme: Optional[Literal[SCHEMES]] = None  # type: ignore[valid-type]
    stage: Optional[Literal[STAGES]] = None  # type: ignore[valid-type]
    capacity_min_kw: Optional[Decimal] = Field(None, ge=0, le=100000)
    capacity_max_kw: Optional[Decimal] = Field(None, ge=0, le=100000)
    commissioned_from: Optional[date] = None
    commissioned_to: Optional[date] = None
    created_from: Optional[date] = None
    created_to: Optional[date] = None
    module_make: Optional[str] = Field(None, max_length=120)
    inverter_make: Optional[str] = Field(None, max_length=120)
    associate: Optional[str] = Field(None, max_length=200)
    near: Optional[Near] = None

    @model_validator(mode="after")
    def _ranges(self):
        if self.capacity_min_kw is not None and self.capacity_max_kw is not None \
                and self.capacity_min_kw > self.capacity_max_kw:
            raise ValueError("capacity_min_kw is above capacity_max_kw")
        for a, b in (("commissioned_from", "commissioned_to"), ("created_from", "created_to")):
            if getattr(self, a) and getattr(self, b) and getattr(self, a) > getattr(self, b):
                raise ValueError(f"{a} is after {b}")
        return self

    def applied(self) -> dict:
        out = {k: v for k, v in self.model_dump(exclude_none=True, mode="json").items() if k != "source"}
        if self.source != "both":
            out["source"] = self.source
        return out


# Which visible field each filter reads (config/archive.yaml groups).
FILTER_FIELDS = {"customer_name": "customer_name", "state": "state", "district": "district", "block": "block",
                 "scheme": "scheme_or_segment", "stage": "current_stage", "capacity_min_kw": "capacity_kw",
                 "capacity_max_kw": "capacity_kw", "commissioned_from": "commissioning_date",
                 "commissioned_to": "commissioning_date", "module_make": "module_make",
                 "inverter_make": "inverter_make", "associate": "associate_id", "near": "latitude"}
FILTER_LABELS = {"customer_name": "customer name", "block": "block", "near": "location", "associate": "associate",
                 "scheme": "scheme", "stage": "stage", "state": "state", "district": "district"}


def check_filters(viewer, f: ProjectFilters) -> None:
    for name, fieldname in FILTER_FIELDS.items():
        if getattr(f, name) is not None and not access.sees(viewer, fieldname):
            label = FILTER_LABELS.get(name, name.replace("_", " "))
            raise ToolError("filter_not_permitted", f"Your role cannot search projects by {label}.", name)


# ── Refs and paths ───────────────────────────────────────────────────────────────

def project_ref(viewer, source: str, pk: int, ref: str, label: str, onboarding_id=None, lead_id=None) -> dict:
    if source == "archive":
        return {"ref": ref, "kind": "archive_project", "label": label, "path": records_config.path("archive", id=pk)}
    out = {"ref": ref, "kind": "live_project", "label": label}
    if onboarding_id and access.reads_service(viewer):   # the page's own rule (can_view_execution)
        out["path"] = records_config.path("live_project", onboarding_id=int(onboarding_id))
    elif lead_id and access.reads_leads(viewer):
        out["path"] = records_config.path("lead", lead_id=int(lead_id))
    return out


def lead_ref(viewer, lead_id: int, label: str) -> dict:
    return {"ref": f"L{lead_id}", "kind": "lead", "label": label, "path": records_config.path("lead", lead_id=lead_id)}


def customer_ref(viewer, customer_id: int, label: str) -> dict:
    """No dashboard page opens a customer, so a customer citation never links."""
    return {"ref": f"C{customer_id}", "kind": "customer", "label": label, "path": None}


def _label(row: dict) -> str:
    parts = [str(row.get(k)) for k in ("district", "state") if row.get(k)]
    kw = row.get("capacity_kw")
    return ", ".join(([f"{kw} kW"] if kw else []) + parts) or "project"


def _result(tool: str, kind: str, payload, refs: list, searched: dict, **extra) -> dict:
    return {"tool": tool, "kind": kind, kind: payload, "refs": refs, "searched": searched, **extra}


# ── Projects ─────────────────────────────────────────────────────────────────────

def _subject(db: Session, source: str, pk: int):
    return db.get(Project if source == "live" else ArchivedProject, pk)


def _card_row(db: Session, viewer, sql_row: dict) -> Optional[dict]:
    subject = _subject(db, sql_row["source"], sql_row["id"])
    try:
        card = view.project_card(db, subject, viewer)
    except view.ViewForbidden:
        return None
    row = {"ref": card["ref"], "source": card["source"]}
    row.update({k: v["value"] for k, v in card["fields"].items()})
    if sql_row["source"] == "live" and access.sees(viewer, "current_stage"):
        row["current_stage"] = sql_row.get("stage")
    return row


def _project_rows(db: Session, viewer, sql_rows: list, extra: Optional[Callable] = None) -> tuple[list, list]:
    rows, refs = [], []
    for r in sql_rows:
        row = _card_row(db, viewer, r)
        if row is None:
            continue
        if extra:
            row.update(extra(r))
        rows.append(row)
        refs.append(project_ref(viewer, r["source"], r["id"], row["ref"], _label(row), r.get("onboarding_id"),
                                r.get("lead_id")))
    return rows, refs


class SearchProjects(_Args):
    filters: ProjectFilters = Field(default_factory=ProjectFilters)
    limit: int = Field(20, ge=1, le=50)


def _near_rows(db: Session, viewer, u, where: list, near: Near, limit: int) -> tuple[list, int, dict]:
    cap = records_config.limit("near_candidate_cap")
    km = near.km or records_config.limit("near_default_km")
    centres, note = [], {}
    if near.place:
        anchor_where = [or_(projects._like(u.c.address, near.place), projects._like(u.c.block, near.place),
                            projects._like(u.c.district, near.place))]
        anchors = projects.with_coordinates(db, u, anchor_where, cap)
        centres = [(a["latitude"], a["longitude"]) for a in anchors]
        note = {"near_place": near.place, "anchor_projects": len(anchors)}
    else:
        centres = [(near.lat, near.lng)]
    candidates = projects.with_coordinates(db, u, where, cap) if centres else []
    hits = []
    for c in candidates:
        d = min(geo.haversine_m(lat, lng, c["latitude"], c["longitude"]) for lat, lng in centres) / 1000
        if d <= km:
            hits.append((d, c))
    hits.sort(key=lambda h: (h[0], h[1]["source"], h[1]["id"]))
    note.update({"radius_km": km, "candidates_measured": len(candidates),
                 "candidates_capped": len(candidates) >= cap})
    return hits[:limit], len(hits), note


def search_projects(db: Session, viewer, args: SearchProjects) -> dict:
    f = args.filters
    check_filters(viewer, f)
    u = projects.unified(db, viewer, f.source)
    near = f.near
    where = projects.conditions(u, f, viewer)
    if near is not None:
        hits, total, note = _near_rows(db, viewer, u, where, near, args.limit)
        distance = {(c["source"], c["id"]): round(d, 2) for d, c in hits}
        rows, refs = _project_rows(db, viewer, [c for _, c in hits],
                                   extra=lambda r: {"distance_km": distance[(r["source"], r["id"])]})
        return _result("search_projects", "rows", rows, refs, {**f.applied(), **note}, total=total,
                       truncated=total > len(rows))
    sql_rows, total = projects.search(db, u, where, args.limit)
    rows, refs = _project_rows(db, viewer, sql_rows)
    return _result("search_projects", "rows", rows, refs, f.applied(), total=total, truncated=total > len(rows),
                   untrusted=_untrusted_from_rows(rows))


def _untrusted_from_rows(rows: list) -> list:
    return [{"ref": r["ref"], "field": k, "text": str(r[k])} for r in rows for k in UNTRUSTED_PROJECT_FIELDS
            if r.get(k)]


class ProjectDossier(_Args):
    ref: str = Field(min_length=2, max_length=100)


def project_dossier(db: Session, viewer, args: ProjectDossier) -> dict:
    searched = {"ref": args.ref}
    subject = view.find_subject(db, args.ref)
    if subject is None:
        return _result("project_dossier", "record", None, [], searched)
    try:
        dossier = view.project_dossier(db, subject, viewer)
    except view.ViewForbidden:
        # Same answer as "no such project": a project the caller may not open is not on
        # record for them.
        return _result("project_dossier", "record", None, [], searched)
    fields = {k: v["value"] for k, v in dossier["fields"].items()}
    sections = {k: [item["value"] for item in items] for k, items in dossier["sections"].items()}
    record = {"ref": dossier["ref"], "source": dossier["source"], "fields": fields, "sections": sections}
    onboarding_id = lead_id = None
    if isinstance(subject, Project):
        onboarding_id = db.execute(select(CustomerOnboarding.id).where(
            CustomerOnboarding.project_id == subject.id)).scalar()
        lead_id = db.execute(select(Lead.id).where(Lead.project_id == subject.id).order_by(Lead.id)).scalars().first()
    ref = project_ref(viewer, dossier["source"], dossier["id"], dossier["ref"], _label(fields), onboarding_id, lead_id)
    untrusted = [{"ref": dossier["ref"], "field": k, "text": str(fields[k])} for k in UNTRUSTED_PROJECT_FIELDS
                 if fields.get(k)]
    return _result("project_dossier", "record", record, [ref], searched, untrusted=untrusted)


METRICS = ("count", "sum_capacity_kw", "avg_capacity_kw", "sum_contract_value", "sum_received", "sum_outstanding")
MONEY_METRICS = ("sum_contract_value", "sum_received", "sum_outstanding")
GROUPS = ("state", "district", "block", "scheme", "month", "year", "associate", "package", "stage", "module_make",
          "inverter_make")


class Aggregate(_Args):
    metric: Literal[METRICS]  # type: ignore[valid-type]
    group_by: Optional[Literal[GROUPS]] = None  # type: ignore[valid-type]
    filters: ProjectFilters = Field(default_factory=ProjectFilters)
    limit: int = Field(20, ge=1, le=50)


GROUP_FIELDS = {"state": "state", "district": "district", "block": "block", "scheme": "scheme_or_segment", "month": "commissioning_date",
                "year": "commissioning_date", "associate": "associate_id", "stage": "current_stage",
                "module_make": "module_make", "inverter_make": "inverter_make"}


def aggregate(db: Session, viewer, args: Aggregate) -> dict:
    if args.metric in MONEY_METRICS and not access.sees_money(viewer):
        raise ToolError("not_permitted", "Your role does not see contract values or payments.", "metric")
    if args.group_by == "package":
        raise ToolError("invalid_argument", "The package a project was sold under is not on record for completed "
                                            "projects, so totals cannot be grouped by package.", "group_by")
    if args.group_by and not access.sees(viewer, GROUP_FIELDS[args.group_by]):
        raise ToolError("filter_not_permitted", f"Your role cannot group projects by {args.group_by}.", "group_by")
    if args.filters.near is not None:
        raise ToolError("invalid_argument", "Totals are not computed over a radius search; search the projects "
                                            "near a place instead.", "filters.near")
    check_filters(viewer, args.filters)
    u = projects.unified(db, viewer, args.filters.source)
    where = projects.conditions(u, args.filters, viewer)
    result = projects.aggregate(db, u, where, args.metric, args.group_by, args.limit)
    searched = {**args.filters.applied(), "metric": args.metric, **({"group_by": args.group_by} if args.group_by else {})}
    notes = []
    if args.metric in ("sum_received", "sum_outstanding"):
        notes.append("Received and outstanding amounts are on record for live projects only.")
    if args.group_by in ("month", "year"):
        notes.append("Grouped by commissioning date.")
    if args.group_by == "associate":
        notes.append("Only live projects record the associate who brought them.")
    refs = [{"ref": g["group"], "kind": "associate", "label": g.get("associate_name") or g["group"]}
            for g in result["groups"] if args.group_by == "associate" and g["group"]]
    return _result("aggregate", "aggregate", result, refs, searched, notes=notes)


# ── Warranty, service requests, payments ─────────────────────────────────────────

class WarrantyExpiring(_Args):
    days: int = Field(90, ge=1, le=3650)
    filters: ProjectFilters = Field(default_factory=ProjectFilters)
    limit: int = Field(20, ge=1, le=50)


def warranty_expiring(db: Session, viewer, args: WarrantyExpiring, today: date) -> dict:
    check_filters(viewer, args.filters)
    if not access.sees(viewer, "warranty_end_modules"):
        raise ToolError("not_permitted", "Your role does not see warranty dates.")
    u = projects.unified(db, viewer, args.filters.source)
    end = today + timedelta(days=args.days)
    window = or_(u.c.warranty_end_modules.between(today, end), u.c.warranty_end_inverter.between(today, end))
    where = projects.conditions(u, args.filters, viewer) + [window]
    sql_rows, total = projects.search(db, u, where, args.limit)

    def ends(r):
        return {"warranty_end_modules": r["warranty_end_modules"].isoformat() if r["warranty_end_modules"] else None,
                "warranty_end_inverter": r["warranty_end_inverter"].isoformat() if r["warranty_end_inverter"] else None}
    rows, refs = _project_rows(db, viewer, sql_rows, extra=ends)
    searched = {**args.filters.applied(), "warranty_ends_from": today.isoformat(), "warranty_ends_to": end.isoformat()}
    return _result("warranty_expiring", "rows", rows, refs, searched, total=total, truncated=total > len(rows))


class ServiceRequests(_Args):
    status: Optional[Literal["open", "in_progress", "resolved", "rejected", "open_or_in_progress"]] = None
    ref: Optional[str] = Field(None, max_length=100)
    within_warranty: Optional[bool] = None
    limit: int = Field(20, ge=1, le=50)


def service_requests(db: Session, viewer, args: ServiceRequests) -> dict:
    if not access.reads_service(viewer):
        raise ToolError("not_permitted", "Your role does not see service requests.")
    u = projects.unified(db, viewer, "live")   # the caller's live projects (scoping in SQL)
    visible = select(u.c.id)
    q = select(ServiceRequest, Project.reference_id).join(Project, Project.id == ServiceRequest.project_id) \
        .where(ServiceRequest.project_id.in_(visible))
    if args.status == "open_or_in_progress":
        q = q.where(ServiceRequest.status.in_((ServiceRequestStatus.open, ServiceRequestStatus.in_progress)))
    elif args.status:
        q = q.where(ServiceRequest.status == ServiceRequestStatus(args.status))
    if args.ref:
        q = q.where(func.upper(Project.reference_id) == args.ref.upper())
    if args.within_warranty is not None:
        q = q.where(ServiceRequest.within_warranty.is_(args.within_warranty))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows, refs, untrusted = [], [], []
    for sr, ref in db.execute(q.order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
                              .limit(args.limit)).all():
        key = f"SR-{sr.id}"
        rows.append({"ref": key, "project_ref": ref, "category": sr.category.value, "status": sr.status.value,
                     "raised_on": view._plain(sr.created_at), "within_warranty": sr.within_warranty,
                     "warranty_end": view._plain(sr.warranty_end), "sla_due_at": view._plain(sr.sla_due_at)})
        untrusted.append({"ref": key, "field": "description", "text": (sr.description or "")[:300]})
        sr_ref = {"ref": key, "kind": "service_request", "label": f"{sr.category.value} on {ref}"}
        if access.reads_service(viewer):   # the page's own rule (can_view_execution)
            sr_ref["path"] = records_config.path("service_request", id=sr.id)
        refs.append(sr_ref)
        onboarding_id = db.execute(select(CustomerOnboarding.id).where(
            CustomerOnboarding.project_id == sr.project_id)).scalar()
        refs.append(project_ref(viewer, "live", sr.project_id, ref, "project", onboarding_id, None))
    return _result("service_requests", "rows", rows, refs, args.model_dump(exclude_none=True, exclude={"limit"}),
                   total=int(total), truncated=total > len(rows), untrusted=untrusted)


class PaymentsStatus(_Args):
    filters: ProjectFilters = Field(default_factory=lambda: ProjectFilters(source="live"))
    only_outstanding: bool = False
    limit: int = Field(20, ge=1, le=50)


def payments_status(db: Session, viewer, args: PaymentsStatus) -> dict:
    if not access.reads_payments(viewer):
        raise ToolError("not_permitted", "Your role does not see contract values or payments.")
    check_filters(viewer, args.filters)
    f = args.filters.model_copy(update={"source": "live"})
    u = projects.unified(db, viewer, "live")
    where = projects.conditions(u, f, viewer) + [u.c.contract_value.isnot(None)]
    if args.only_outstanding:
        where.append(u.c.outstanding > 0)
    totals = {m: projects.aggregate(db, u, where, m, None, 1)["total"]
              for m in ("sum_contract_value", "sum_received", "sum_outstanding")}
    sql_rows, total = projects.search(db, u, where, args.limit)
    rows, refs = [], []
    from app.payments import service as payments
    for r in sql_rows:
        row = {"ref": r["ref"], "contract_value_inr": projects.plain_number(r["contract_value"], "sum_received"),
               "amount_received_inr": projects.plain_number(r["received"], "sum_received"),
               "amount_outstanding_inr": projects.plain_number(r["outstanding"], "sum_received"),
               "current_stage": r["stage"]}
        if access.sees(viewer, "customer_name"):
            row["customer_name"] = r["customer_name"]
        if r["onboarding_id"]:
            onboarding = db.get(CustomerOnboarding, r["onboarding_id"])
            due = [m for m in payments.milestone_status(db, onboarding) if m["due_status"] in ("due", "partially_paid")]
            row["milestones_due"] = ", ".join(f"{m['label']} ({m['outstanding']})" for m in due) or None
        rows.append(row)
        refs.append(project_ref(viewer, "live", r["id"], r["ref"], _label({**r, "capacity_kw": r["capacity_kw"]}),
                                r["onboarding_id"], r["lead_id"]))
    aggregate_block = {"metric": "payments", "projects": total, "total_contract_value": totals["sum_contract_value"],
                       "total_received": totals["sum_received"], "total_outstanding": totals["sum_outstanding"]}
    return _result("payments_status", "rows", rows, refs, {**f.applied(), "only_outstanding": args.only_outstanding},
                   total=total, truncated=total > len(rows), aggregate=aggregate_block)


# ── Leads ────────────────────────────────────────────────────────────────────────

TECHNICAL_LEAD_FIELDS = ("project_type", "capacity_kw", "location", "roof_type")
CONTACT_LEAD_FIELDS = ("name", "email", "phone", "contact_name", "contact_email", "contact_phone")


class LeadLookup(_Args):
    lead_id: Optional[int] = Field(None, ge=1)
    text: Optional[str] = Field(None, min_length=2, max_length=100)
    stage: Optional[Literal[tuple(s.value for s in PipelineStage)]] = None  # type: ignore[valid-type]
    assigned: Optional[Literal["me", "unassigned", "any"]] = None
    limit: int = Field(20, ge=1, le=50)


def _lead_scope(viewer, assigned=None):
    from app.auth.scoping import lead_scope
    return lead_scope(viewer, assigned)


def lead_lookup(db: Session, viewer, args: LeadLookup) -> dict:
    if not access.reads_leads(viewer):
        raise ToolError("not_permitted", "Your role does not see leads.")
    contact = access.lead_contact(viewer)
    q = select(Lead, Customer).join(Customer, Customer.id == Lead.customer_id) \
        .where(_lead_scope(viewer, args.assigned).condition())
    if args.lead_id:
        q = q.where(Lead.id == args.lead_id)
    if args.stage:
        q = q.where(Lead.pipeline_stage == PipelineStage(args.stage))
    if args.text:
        like = f"%{args.text.lower()}%"
        parts = [Lead.id == int(args.text)] if args.text.isdigit() else []
        if contact:
            parts += [func.lower(Customer.name).like(like), func.lower(Customer.email).like(like),
                      func.lower(func.coalesce(Customer.phone, "")).like(like)]
        q = q.where(or_(*parts)) if parts else q.where(Lead.id == -1)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows, refs, untrusted = [], [], []
    for lead, customer in db.execute(q.order_by(Lead.created_at.desc(), Lead.id.desc()).limit(args.limit)).all():
        fields = dict(lead.collected_fields or {})
        keep = {k: v for k, v in fields.items()
                if k in TECHNICAL_LEAD_FIELDS or (contact and k not in CONTACT_LEAD_FIELDS)}
        row = {"ref": f"L{lead.id}", "pipeline_stage": lead.pipeline_stage.value,
               "lead_score": lead.lead_score.value if lead.lead_score else None, "source": lead.source.value,
               "created_at": view._plain(lead.created_at), "assigned_employee_id": lead.assigned_employee_id,
               "project_ref": lead.project.reference_id if lead.project else None}
        if contact:
            row.update({"customer_name": customer.name, "customer_ref": f"C{customer.id}"})
        row.update({f"field_{k}": v for k, v in keep.items() if isinstance(v, (str, int, float, bool))})
        rows.append(row)
        label = (customer.name if contact and customer.name else "lead") + f" — {lead.pipeline_stage.value}"
        refs.append(lead_ref(viewer, lead.id, label))
        if lead.project:
            onboarding_id = db.execute(select(CustomerOnboarding.id).where(
                CustomerOnboarding.project_id == lead.project_id)).scalar()
            refs.append(project_ref(viewer, "live", lead.project_id, lead.project.reference_id,
                                    f"project of lead {lead.id}", onboarding_id, lead.id))
        if contact:
            refs.append(customer_ref(viewer, customer.id, customer.name or "customer"))
    return _result("lead_lookup", "rows", rows, refs, args.model_dump(exclude_none=True, exclude={"limit"}),
                   total=int(total), truncated=total > len(rows), untrusted=untrusted)


class PipelineSummary(_Args):
    created_from: Optional[date] = None
    created_to: Optional[date] = None
    source: Optional[Literal[tuple(s.value for s in LeadSource)]] = None  # type: ignore[valid-type]
    assigned: Optional[Literal["me", "unassigned", "any"]] = None


def _ist_midnight_utc(day: date):
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    from app.config import settings
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.business_timezone)).astimezone(timezone.utc)


def pipeline_summary(db: Session, viewer, args: PipelineSummary) -> dict:
    if not access.reads_leads(viewer):
        raise ToolError("not_permitted", "Your role does not see leads.")
    where = [_lead_scope(viewer, args.assigned).condition()]
    if args.created_from:
        where.append(Lead.created_at >= _ist_midnight_utc(args.created_from))
    if args.created_to:
        where.append(Lead.created_at < _ist_midnight_utc(args.created_to + timedelta(days=1)))
    if args.source:
        where.append(Lead.source == LeadSource(args.source))
    total = db.execute(select(func.count(Lead.id)).where(*where)).scalar_one()
    groups = [{"group": stage.value, "value": str(int(n)), "projects": int(n)}
              for stage, n in db.execute(select(Lead.pipeline_stage, func.count(Lead.id)).where(*where)
                                         .group_by(Lead.pipeline_stage).order_by(func.count(Lead.id).desc()))]
    result = {"metric": "lead_count", "group_by": "pipeline_stage", "total": str(int(total)), "projects": int(total),
              "groups": groups}
    return _result("pipeline_summary", "aggregate", result, [], args.model_dump(exclude_none=True, mode="json"),
                   notes=["Counts leads, not projects."])


# ── Customer memory ──────────────────────────────────────────────────────────────

class CustomerRecords(_Args):
    customer_id: Optional[int] = Field(None, ge=1)
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    question: str = Field(min_length=1, max_length=1000)


def _rewrite_markers(block: str, customer_id: int) -> str:
    """recall.py numbers leads by position ([L1] LEAD 45); Nova Records cites a lead by its
    id ([L45]) and the customer by theirs ([C12])."""
    import re

    block = re.sub(r"^\[L\d+\] LEAD (\d+)", r"[L\1] LEAD \1", block, flags=re.M)
    return block.replace("[C1] IDENTITY", f"[C{customer_id}] IDENTITY", 1)


def customer_records(db: Session, viewer, args: CustomerRecords) -> dict:
    if not access.reads_customer_memory(viewer):
        raise ToolError("not_permitted", "Your role does not read customer memory.")
    from app.memory import recall
    from app.memory.memory_manager import MemoryManager

    memory = MemoryManager(db)
    sales = access.memory_narrowed_to_assigned(viewer)
    me = getattr(viewer, "employee_id", None)
    visible = (lambda c: memory.customer_visible_to_sales(c.id, me)) if sales else None   # noqa: E731
    searched = args.model_dump(exclude_none=True, exclude={"question"})
    if args.customer_id is not None:
        if sales and not memory.customer_visible_to_sales(args.customer_id, me):
            return _result("customer_records", "record", None, [], searched)
        customer, candidates = recall.resolve_customer(memory, "", args.customer_id)
    else:
        customer, candidates = recall.resolve_customer(memory, args.name or args.question, None, visible=visible)
    if customer is None and len(candidates) > 1:
        items = [{"ref": f"C{c.id}", "name": c.name, "company": c.company} for c in candidates]
        return _result("customer_records", "candidates", items,
                       [customer_ref(viewer, c.id, c.name or "customer") for c in candidates], searched)
    if customer is None:
        return _result("customer_records", "record", None, [], searched)
    retrieved = recall.retrieve(memory, customer.id, args.question)
    block = _rewrite_markers(retrieved["context_block"], customer.id)
    leads = [lead.id for lead in (retrieved["dossier"].get("leads") or [])]
    refs = [customer_ref(viewer, customer.id, customer.name or "customer")]
    refs += [lead_ref(viewer, lid, f"lead of {customer.name or 'customer'}") for lid in leads]
    refs += [{"ref": f"M{m.id}", "kind": "memory", "label": m.summary[:80]} for m in retrieved["memories"]]
    record = {"customer_ref": f"C{customer.id}", "name": customer.name, "context": block, "mode": retrieved["mode"]}
    return _result("customer_records", "record", record, refs, searched)


# ── Associates ───────────────────────────────────────────────────────────────────

class AssociateSummary(_Args):
    associate_id: Optional[str] = Field(None, max_length=40)
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    district: Optional[str] = Field(None, max_length=120)
    limit: int = Field(20, ge=1, le=50)


def associate_summary(db: Session, viewer, args: AssociateSummary) -> dict:
    if not access.reads_associates(viewer):
        raise ToolError("not_permitted", "Your role does not see associates.")
    commissions = access.reads_commissions(viewer)
    district_name = func.coalesce(
        select(TerritoryDistrict.name).where(TerritoryDistrict.id == func.coalesce(
            Associate.territory_district_id, AssociateApplication.district_id)).scalar_subquery(), None)
    q = select(Associate, district_name.label("district")).outerjoin(
        AssociateApplication, AssociateApplication.id == Associate.associate_application_id)
    if args.associate_id:
        q = q.where(func.upper(Associate.associate_id) == args.associate_id.upper())
    if args.name:
        q = q.where(func.lower(Associate.full_name).like(f"%{args.name.lower()}%"))
    if args.district:
        q = q.where(func.lower(district_name) == args.district.lower())
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows, refs = [], []
    for a, district in db.execute(q.order_by(Associate.associate_id).limit(args.limit)).all():
        counts = dict(db.execute(select(CustomerOnboarding.status, func.count()).where(
            CustomerOnboarding.owner_associate_id == a.id).group_by(CustomerOnboarding.status)).all())
        row = {"ref": a.associate_id, "name": a.full_name, "type": a.associate_type.value,
               "level": a.level.value if a.level else None, "status": a.status.value, "district": district,
               "customers_onboarded": str(sum(counts.values())) if counts else "0",
               "customers_verified": str(counts.get(OnboardingStatus.verified, 0))}
        if commissions:
            for status in (CommissionEntryStatus.earned, CommissionEntryStatus.approved, CommissionEntryStatus.paid):
                value = db.execute(select(func.coalesce(func.sum(CommissionEntry.amount), 0)).where(
                    CommissionEntry.associate_id == a.id, CommissionEntry.status == status)).scalar_one()
                row[f"commission_{status.value}_inr"] = projects.plain_number(value, "sum_received")
        rows.append(row)
        refs.append({"ref": a.associate_id, "kind": "associate", "label": a.full_name})
    return _result("associate_summary", "rows", rows, refs, args.model_dump(exclude_none=True, exclude={"limit"}),
                   total=int(total), truncated=total > len(rows),
                   notes=[] if commissions else ["Commission totals are shown to Admin and Accounts only."])


# ── Catalogue and company ────────────────────────────────────────────────────────

class CatalogueLookup(_Args):
    make: Optional[str] = Field(None, max_length=120)
    component: Optional[Literal["solar_panel", "inverter", "cables"]] = None
    package: Optional[Literal["NOVA_ASSURED", "NOVA_SIGNATURE"]] = None
    phase: Optional[Literal["single_phase", "three_phase"]] = None
    kw: Optional[int] = Field(None, ge=1, le=100)


def catalogue_lookup(db: Session, viewer, args: CatalogueLookup) -> dict:
    from app.pricing import price_list

    data = price_list.load()
    rows = []
    for package in price_list.PACKAGES:
        if args.package and package != args.package:
            continue
        for component, spec in data["equipment"][package].items():
            if args.component and component != args.component:
                continue
            makes = list(spec.get("makes") or [])
            if args.make and not any(args.make.lower() in m.lower() for m in makes):
                continue
            rows.append({"ref": f"PKG-{package}", "package": price_list.display_name(package), "component": component,
                         "makes": ", ".join(makes), "or_equivalent": bool(spec.get("or_equivalent"))})
    # The equipment master is not per package: listed only when no package is asked about.
    master = select(Equipment).where(Equipment.is_active.is_(True), Equipment.is_placeholder.is_(False))
    if args.make:
        master = master.where(func.lower(Equipment.brand).like(f"%{args.make.lower()}%"))
    category = {"solar_panel": EquipmentCategory.module, "inverter": EquipmentCategory.inverter}.get(args.component)
    if category is not None:
        master = master.where(Equipment.category == category)
    elif args.component:
        master = master.where(Equipment.category.notin_((EquipmentCategory.module, EquipmentCategory.inverter)))
    for e in ([] if args.package else
              db.execute(master.order_by(Equipment.brand, Equipment.model_name).limit(_rows_cap())).scalars()):
        rows.append({"ref": f"EQ-{e.id}", "package": None, "component": e.category.value, "makes": e.brand,
                     "model": e.model_name, "dcr_compliant": e.is_dcr_compliant})
    refs = [{"ref": r["ref"], "kind": "catalogue", "label": f"{r['package'] or 'equipment master'}: {r['component']}"}
            for r in rows]
    searched = args.model_dump(exclude_none=True)
    extra = {}
    if args.package and args.phase and args.kw:
        if not access.sees_prices(viewer):
            raise ToolError("not_permitted", "Your role does not see list prices.")
        quote = price_list.quote(args.package, {"single_phase": "SINGLE", "three_phase": "THREE"}[args.phase], args.kw)
        if isinstance(quote, price_list.Unsupported):
            searched["price_not_listed"] = quote.detail
        else:
            ref = f"PRICE-{args.package}-{args.phase}-{args.kw}"
            extra["record"] = {"ref": ref, "package": price_list.display_name(args.package), "phase": args.phase,
                               "kw": args.kw, **{f"{k}_price_inr" if k == "final" else f"{k}_inr": v
                                                 for k, v in quote.amounts().items()},
                               "price_list_version": quote.price_list_version}
            refs.append({"ref": ref, "kind": "price_list", "label": f"{args.kw} kW {args.phase} list price"})
    return _result("catalogue_lookup", "rows", rows, refs, searched, total=len(rows), **extra)


class CompanyReference(_Args):
    topic: Optional[str] = Field(None, max_length=100)


def company_reference(db: Session, viewer, args: CompanyReference) -> dict:
    from app.llm.company_facts import COMPANY_FACTS

    record = {"ref": "COMPANY", "facts": COMPANY_FACTS}
    return _result("company_reference", "record", record,
                   [{"ref": "COMPANY", "kind": "company", "label": "Grid Nova verified company facts"}],
                   args.model_dump(exclude_none=True))


# ── Catalogue for the planner ────────────────────────────────────────────────────

TOOLS: dict[str, dict[str, Any]] = {
    "search_projects": {"args": SearchProjects, "fn": search_projects,
                        "doc": "Find live and/or archived projects by place, scheme, size, dates, makes, associate, "
                               "customer name or a radius around coordinates/a place on record."},
    "project_dossier": {"args": ProjectDossier, "fn": project_dossier,
                        "doc": "Everything on record for one project by reference (GNE-…, ARC-<id> or a Grid Nova ref)."},
    "aggregate": {"args": Aggregate, "fn": aggregate,
                  "doc": "Count projects or total/average capacity or money, optionally grouped. Use this for every "
                         "count, total or average — never add numbers up yourself."},
    "lead_lookup": {"args": LeadLookup, "fn": lead_lookup, "doc": "Find leads by id, customer name/email/phone or stage."},
    "pipeline_summary": {"args": PipelineSummary, "fn": pipeline_summary, "doc": "Lead counts by pipeline stage."},
    "customer_records": {"args": CustomerRecords, "fn": customer_records,
                         "doc": "What is remembered about one customer (conversations, preferences, proposals)."},
    "payments_status": {"args": PaymentsStatus, "fn": payments_status,
                        "doc": "Contract value, received and outstanding per live project, with SQL totals."},
    "warranty_expiring": {"args": WarrantyExpiring, "fn": warranty_expiring,
                          "doc": "Projects whose module or inverter warranty ends within `days` from today."},
    "service_requests": {"args": ServiceRequests, "fn": service_requests,
                         "doc": "Warranty / O&M service requests on live projects."},
    "associate_summary": {"args": AssociateSummary, "fn": associate_summary,
                          "doc": "Associates: type, territory, customers onboarded (and commissions for Admin/Accounts)."},
    "catalogue_lookup": {"args": CatalogueLookup, "fn": catalogue_lookup,
                         "doc": "Approved makes per package and the equipment master (list price for given kW/phase "
                                "only for roles that see prices)."},
    "company_reference": {"args": CompanyReference, "fn": company_reference,
                          "doc": "Grid Nova's verified company facts and commercial terms."},
}
NEEDS_TODAY = ("warranty_expiring",)


def schemas() -> dict:
    return {name: {"description": t["doc"], "parameters": t["args"].model_json_schema()} for name, t in TOOLS.items()}


def run(db: Session, viewer, tool: str, args: dict, today: date) -> dict:
    """Validate `args` and run one tool. ValueError (pydantic) for bad arguments."""
    spec = TOOLS[tool]
    parsed = spec["args"].model_validate(args or {})
    if tool in NEEDS_TODAY:
        return spec["fn"](db, viewer, parsed, today)
    return spec["fn"](db, viewer, parsed)

