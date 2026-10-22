"""Associate-led customer onboarding — associate-authenticated (get_current_associate,
never the staff dependency). The associate enters everything on the customer's behalf.

Visibility: app/auth/scoping.py OnboardingScope — own customers; a Preferred Associate
also sees (read-only) customers whose site is in their territory. Only the owner edits.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.associates import uploads, validation
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import (
    onboarding_scope, require_onboarding_documents, require_onboarding_edit, require_onboarding_view,
)
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    INSTALLATION_PHOTO_SLOTS, ROOF_OBSTACLES, Associate, ConnectionPhase, ConnectionType, ConsumerPriorityClass,
    CustomerOnboarding, CustomerTimeline, CustomerType, DiscomConsumerCategory, DistanceMethod, GovernmentScheme,
    InverterLocation, OnboardingStatus, ProjectFamily, ProposalInputs, RoofMaterial, RoofOrientation,
    ShadingCondition, SiteType, SocietyNocStatus, SurveyVariant,
)
from app.onboarding import config, serialize, service, site_location
from app.survey import config as survey_config

router = APIRouter(prefix="/api/v1/associate/onboardings")

MAX_FILE_BYTES = 10 * 1024 * 1024


def _associate(db: Session, current: AuthenticatedAssociate) -> Associate:
    return db.get(Associate, current.associate_pk)


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


# ── Schemas ──────────────────────────────────────────────────────────────────────

class ConsumptionMonth(BaseModel):
    """One electricity bill. `month` is YYYY-MM (checked in app/survey/validation.py)."""
    month: str = Field(max_length=7)
    units_kwh: Optional[Decimal] = Field(default=None, ge=0, le=10_000_000)
    bill_inr: Optional[Decimal] = Field(default=None, ge=0, le=100_000_000)


class RoofSection(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    length_m: Decimal = Field(gt=0, le=500)
    width_m: Decimal = Field(gt=0, le=500)
    obstacle_area_sqm: Optional[Decimal] = Field(default=None, ge=0, le=250_000)


class _SiteAssessment(BaseModel):
    """Fields shared by create and update. Hard limits reject impossible values;
    implausible-but-possible ones come back as warnings instead."""
    customer_type: Optional[CustomerType] = None
    organisation_name: Optional[str] = Field(default=None, max_length=200)
    gst_number: Optional[str] = None
    project_family: Optional[ProjectFamily] = None
    scheme_linked: Optional[bool] = None
    scheme_name: Optional[GovernmentScheme] = None
    pin_code: Optional[str] = None
    full_address: Optional[str] = Field(default=None, max_length=500)
    site_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    site_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    discom: Optional[str] = Field(default=None, max_length=100)
    consumer_number: Optional[str] = Field(default=None, max_length=50)
    site_type: Optional[SiteType] = None
    roof_area_sqft: Optional[float] = Field(default=None, gt=0, le=10_000_000)
    floors: Optional[int] = Field(default=None, ge=0, le=200)
    shading_condition: Optional[ShadingCondition] = None
    structural_notes: Optional[str] = Field(default=None, max_length=2000)
    roof_ownership_confirmed: Optional[bool] = None
    roof_age_years: Optional[float] = Field(default=None, ge=0, le=200)
    avg_monthly_bill_inr: Optional[float] = Field(default=None, ge=0, le=100_000_000)
    avg_monthly_units_kwh: Optional[float] = Field(default=None, ge=0, le=100_000_000)
    peak_load_kw: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    sanctioned_load_kw: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    connection_phase: Optional[ConnectionPhase] = None
    desired_output_kw: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    load_notes: Optional[str] = Field(default=None, max_length=2000)
    discom_consumer_category: Optional[DiscomConsumerCategory] = None
    consumer_priority_class: Optional[ConsumerPriorityClass] = None
    connection_type: Optional[ConnectionType] = None
    net_metering_required: Optional[bool] = None
    dcr_panels_required: Optional[bool] = None
    ev_charging: Optional[bool] = None
    battery_storage: Optional[bool] = None
    equipment_package: Optional[str] = None
    additional_requirements: Optional[str] = Field(default=None, max_length=2000)
    connection_in_customer_name: Optional[bool] = None
    property_in_customer_name: Optional[bool] = None
    customer_timeline: Optional[CustomerTimeline] = None
    state_lgd_code: Optional[int] = None
    district_lgd_code: Optional[int] = None
    block_lgd_code: Optional[int] = None
    customer_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    customer_mobile: Optional[str] = None
    customer_email: Optional[str] = None
    # ── On-site survey (S1, app/survey/) ──
    consumption_history: Optional[list[ConsumptionMonth]] = None
    survey_variant: Optional[SurveyVariant] = None
    roof_accessible: Optional[bool] = None
    roof_access_note: Optional[str] = Field(default=None, max_length=1000)
    roof_material: Optional[RoofMaterial] = None
    roof_material_other: Optional[str] = Field(default=None, max_length=200)
    roof_orientation: Optional[RoofOrientation] = None
    roof_obstacles: Optional[list[Literal[ROOF_OBSTACLES]]] = Field(default=None, max_length=len(ROOF_OBSTACLES))
    roof_sections: Optional[list[RoofSection]] = Field(default=None, max_length=survey_config.MAX_ROOF_SECTIONS)
    shadow_free_area_sqft: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    roofs_available_count: Optional[int] = Field(default=None, ge=0, le=100)
    roofs_considered_count: Optional[int] = Field(default=None, ge=0, le=100)
    future_roof_expansion_planned: Optional[bool] = None
    water_available: Optional[bool] = None
    inverter_location: Optional[InverterLocation] = None
    indoor_wall_available: Optional[bool] = None
    distance_panel_to_inverter_m: Optional[Decimal] = Field(default=None, ge=0, le=100_000)
    distance_inverter_to_lt_panel_m: Optional[Decimal] = Field(default=None, ge=0, le=100_000)
    distance_method: Optional[DistanceMethod] = None
    society_flat_count: Optional[int] = Field(default=None, ge=1, le=100_000)
    society_noc_status: Optional[SocietyNocStatus] = None
    contract_demand_kva: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    tenant_noc_available: Optional[bool] = None

    @field_validator("pin_code")
    @classmethod
    def _pin(cls, v):
        return validation.normalize_pin(v) if v is not None else v

    @field_validator("customer_mobile")
    @classmethod
    def _mobile(cls, v):
        return validation.normalize_mobile(v) if v is not None else v

    @field_validator("customer_email")
    @classmethod
    def _email(cls, v):
        return validation.normalize_email(v) if v else None

    @field_validator("gst_number")
    @classmethod
    def _gst(cls, v):
        return validation.normalize_gstin(v) if v else None

    @field_validator("customer_name")
    @classmethod
    def _name(cls, v):
        return validation.normalize_name(v) if v is not None else v

    @field_validator("equipment_package")
    @classmethod
    def _package(cls, v):
        if v is not None and v not in config.load()["equipment_packages"]:
            raise ValueError(f"must be one of {sorted(config.load()['equipment_packages'])}")
        return v

    @model_validator(mode="after")
    def _gps_pair(self):
        if (self.site_latitude is None) != (self.site_longitude is None):
            raise ValueError("site_latitude and site_longitude must be given together")
        return self

    def to_service(self, fields: set[str]) -> dict:
        data = {k: getattr(self, k) for k in fields}
        for k in ("consumption_history", "roof_sections"):
            if data.get(k) is not None:
                data[k] = [item.model_dump() for item in data[k]]
        if "equipment_package" in data:
            package = data.pop("equipment_package")
            data["equipment_tier"] = (config.load()["equipment_packages"][package]["equipment_tier"]
                                      if package else None)
        return data


class OnboardingCreate(_SiteAssessment):
    customer_name: str = Field(min_length=1, max_length=200)
    customer_mobile: str
    customer_type: CustomerType
    project_family: ProjectFamily
    scheme_linked: bool = False
    scheme_name: GovernmentScheme = GovernmentScheme.none
    pin_code: str
    full_address: str = Field(min_length=1, max_length=500)
    site_type: SiteType
    consumer_priority_class: ConsumerPriorityClass = ConsumerPriorityClass.normal
    connection_type: ConnectionType
    dcr_panels_required: bool = False
    ev_charging: bool = False
    battery_storage: bool = False
    connection_in_customer_name: bool
    property_in_customer_name: bool
    state_lgd_code: int
    district_lgd_code: int
    notice_version: str
    notice_acknowledged: Literal[True]

    @field_validator("notice_version")
    @classmethod
    def _current_notice(cls, v):
        if v != config.current_notice()[0]:
            raise ValueError("This notice version is no longer current. Reload it and read the current notice.")
        return v


class OnboardingUpdate(_SiteAssessment):
    pass


# ── Routes ───────────────────────────────────────────────────────────────────────

@router.get("/options")
def options(current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Everything the onboarding form needs besides the territory lists (those are
    public: /api/v1/public/territories/...)."""
    version, text = config.current_notice()
    cfg = config.load()
    return {
        "notice": {"version": version, "text": text},
        "equipment_packages": [{"key": k, "label": v["label"], "equipment_tier": v["equipment_tier"]}
                               for k, v in cfg["equipment_packages"].items()],
        "documents": [{"key": d["key"], "label": d["label"], "required_when": d["required_when"]}
                      for d in cfg["required_documents"]],
        "photo_slots": list(INSTALLATION_PHOTO_SLOTS),
        "customer_timelines": [t.value for t in CustomerTimeline],
        "site_location": site_location.options(),
        "survey": survey_config.options(),
    }


@router.post("", status_code=201)
def create_onboarding(body: OnboardingCreate, db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    associate = _associate(db, current)
    data = body.to_service(body.model_fields_set | {
        "customer_type", "project_family", "scheme_linked", "scheme_name", "consumer_priority_class",
        "connection_type", "dcr_panels_required", "ev_charging", "battery_storage"})
    data.pop("notice_version", None)
    data.pop("notice_acknowledged", None)
    try:
        onboarding, warnings = service.create(db, associate, data)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {**serialize.onboarding(db, onboarding, viewer_is_owner=True), "warnings": warnings}


@router.get("")
def list_onboardings(status: Optional[OnboardingStatus] = None, mine_only: bool = False,
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                     db: Session = Depends(get_db),
                     current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Own customers, plus (for a Preferred Associate) every customer sited in their
    territory. `mine_only=true` narrows to own."""
    associate = _associate(db, current)
    scope = onboarding_scope(associate)
    q = select(CustomerOnboarding).where(
        CustomerOnboarding.owner_associate_id == associate.id if mine_only else scope.condition())
    if status:
        q = q.where(CustomerOnboarding.status == status)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = list(db.execute(q.order_by(CustomerOnboarding.created_at.desc(), CustomerOnboarding.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)).scalars())
    from app.onboarding.preload import preloaded
    rate_cache: dict = {}
    with preloaded(db, rows):
        items = serialize.list_items(db, rows, lambda o: {"viewer_is_owner": o.owner_associate_id == associate.id})
        for o, item in zip(rows, items):
            item["estimated_commission"] = _estimated_commission(db, associate, o, rate_cache)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _estimated_commission(db: Session, associate: Associate, onboarding: CustomerOnboarding,
                          rate_cache: Optional[dict] = None) -> Optional[dict]:
    """BE-10: "what will I earn?" — an estimate only (app/commissions/estimate.py). Money
    is owner-only: a Preferred Associate viewing a territory customer gets null."""
    if onboarding.owner_associate_id != associate.id:
        return None
    from app.commissions.estimate import estimated_commission
    return estimated_commission(db, onboarding, rate_cache)


def _load(db: Session, onboarding_id: int) -> CustomerOnboarding:
    onboarding = service.get(db, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return onboarding


@router.get("/{onboarding_id}")
def get_onboarding(onboarding_id: int, db: Session = Depends(get_db),
                   current: AuthenticatedAssociate = Depends(get_current_associate)):
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return {**serialize.onboarding(db, onboarding, viewer_is_owner=onboarding.owner_associate_id == associate.id),
            "estimated_commission": _estimated_commission(db, associate, onboarding)}


@router.patch("/{onboarding_id}")
def update_onboarding(onboarding_id: int, body: OnboardingUpdate, db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Partial update — only the fields sent change. Owner only; draft or submitted
    only (an edit to a submitted customer returns it to draft)."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    changes = body.to_service(set(body.model_fields_set))
    for required in ("customer_name", "customer_mobile", "customer_type", "project_family", "pin_code",
                     "site_type", "connection_type", "connection_in_customer_name", "property_in_customer_name",
                     "state_lgd_code", "district_lgd_code", "full_address"):
        if required in changes and changes[required] is None:
            raise HTTPException(status_code=422, detail={"code": "required", "field": required,
                                                         "message": f"{required} cannot be cleared."})
    try:
        warnings = service.update(db, onboarding, associate, changes)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {**serialize.onboarding(db, onboarding, viewer_is_owner=True), "warnings": warnings}


@router.post("/{onboarding_id}/files", status_code=201)
def upload_file(onboarding_id: int, file: UploadFile = File(...),
                kind: Literal["document", "photo"] = Form(...),
                document_type: Optional[str] = Form(default=None),
                photo_slot: Optional[str] = Form(default=None),
                latitude: Optional[float] = Form(default=None, ge=-90, le=90),
                longitude: Optional[float] = Form(default=None, ge=-180, le=180),
                captured_at: Optional[str] = Form(default=None, max_length=40),
                capture_method: Optional[Literal["camera", "gallery"]] = Form(default=None),
                exif_taken_at_client: Optional[str] = Form(default=None, max_length=40),
                accuracy_m: Optional[float] = Form(default=None, gt=0, le=100_000),
                db: Session = Depends(get_db),
                current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Upload one checklist document or one site photo. The server records when it
    received the file; GPS and the client's capture time are stored if supplied.
    Photos record their provenance (app/execution/photos.py): `capture_method` camera
    (GPS + captured_at required) or gallery (phone gallery; location optional).
    `accuracy_m` (photos): the GPS fix's reported accuracy, stored and returned as-is —
    a camera photo's fix can then become the site location (POST .../site-location)."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    if onboarding.status == OnboardingStatus.closed:
        raise HTTPException(status_code=409, detail={"code": "not_editable",
                                                     "message": "This customer record is closed."})
    if (latitude is None) != (longitude is None):
        raise HTTPException(status_code=422, detail={"code": "gps_pair", "field": "latitude",
                                                     "message": "latitude and longitude must be given together."})
    if kind == "document":
        if document_type not in config.document_keys():
            raise HTTPException(status_code=422, detail={"code": "unknown_document_type", "field": "document_type",
                                                         "message": f"document_type must be one of {config.document_keys()}."})
        target, allowed = "document", uploads.DOCUMENT_TYPES
    else:
        if photo_slot not in INSTALLATION_PHOTO_SLOTS:
            raise HTTPException(status_code=422, detail={"code": "unknown_photo_slot", "field": "photo_slot",
                                                         "message": f"photo_slot must be one of {list(INSTALLATION_PHOTO_SLOTS)}."})
        target, allowed = "installation_photo", uploads.IMAGE_TYPES
    try:
        document = uploads.read_document_sync(file, document_type or photo_slot, allowed=allowed,
                                              max_bytes=MAX_FILE_BYTES)
    except uploads.UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    provenance = None
    if kind == "photo":
        from app.execution import photos
        try:
            provenance = photos.provenance(content=document.content, capture_method=capture_method,
                                           latitude=latitude, longitude=longitude, captured_at=captured_at,
                                           exif_taken_at_client=exif_taken_at_client)
        except AssociateError as exc:
            raise _error(exc)

    from app.storage import r2

    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    # Same key shape as the staff upload endpoint, so staff open these files through
    # the existing GET /api/v1/proposal-inputs/{id}/files/{file_ref}.
    key = f"proposal-inputs/{inputs.id}/{target}/{uuid.uuid4().hex}_{document.filename}"
    try:
        r2.upload_bytes(key, document.content, document.content_type)
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    metadata = {"received_at": datetime.now(timezone.utc).isoformat(), "content_type": document.content_type,
                "uploaded_by": service.actor(associate)}
    if latitude is not None:
        metadata.update(latitude=latitude, longitude=longitude)
    if captured_at:
        metadata["client_captured_at"] = captured_at
    if kind == "photo" and accuracy_m is not None:
        metadata["accuracy_m"] = accuracy_m
    if provenance is not None:
        from app.execution import photos
        metadata.update(photos.flag_duplicate_and_record(db, provenance, project_id=onboarding.project_id,
                                                         source="onboarding_photo", slot=photo_slot,
                                                         storage_key=key))
    MemoryManager(db).attach_proposal_input_upload(
        inputs_id=inputs.id, target=target, filename=document.filename, storage_key=key,
        performed_by=service.actor(associate), document_type=document_type if kind == "document" else None,
        photo_slot=photo_slot if kind == "photo" else None, metadata=metadata,
    )
    service.after_file_upload(db, onboarding, service.actor(associate))
    db.commit()
    return serialize.onboarding(db, onboarding, viewer_is_owner=True)


@router.get("/{onboarding_id}/files/{file_ref}")
def file_link(onboarding_id: int, file_ref: str, db: Session = Depends(get_db),
              current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A short-lived signed link to one uploaded file — the owner (a territory viewer
    only when territory_viewers_see_documents is on). Every link is audited."""
    from app.api.routes.proposal_inputs import uploaded_files
    from app.storage import r2

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_documents(associate, onboarding)
    match = next((f for f in uploaded_files(db.get(ProposalInputs, onboarding.proposal_inputs_id))
                  if f["file_ref"] == file_ref), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        url, expires_at = r2.signed_get_url(match["storage_key"], filename=match["filename"])
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    service.audit_document_link(db, onboarding, service.actor(associate), "onboarding_file", file_ref,
                                match["filename"])
    db.commit()
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": match["filename"], "file_ref": file_ref}


@router.post("/{onboarding_id}/submit")
def submit_onboarding(onboarding_id: int, db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Declare the customer's record complete (every required document uploaded) so
    staff can make the verification call."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    try:
        service.submit(db, onboarding, associate)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return serialize.onboarding(db, onboarding, viewer_is_owner=True)


class _DeviceGps(BaseModel):
    source: Literal["device_gps"]
    latitude: float
    longitude: float
    accuracy_m: float = Field(gt=0, le=100_000)
    captured_at: datetime


class _PhotoGps(BaseModel):
    source: Literal["photo_gps"]
    photo_slot: str = Field(max_length=100)


class _MapsLink(BaseModel):
    source: Literal["google_maps_link"]
    link: str = Field(min_length=1, max_length=500)


class _PastedCoordinates(BaseModel):
    source: Literal["pasted_coordinates"]
    text: str = Field(min_length=1, max_length=100)


class SiteLocationRequest(RootModel):
    root: Annotated[Union[_DeviceGps, _PhotoGps, _MapsLink, _PastedCoordinates], Field(discriminator="source")]


def link_resolver(who: str):
    """(link) -> (lat, lon, precision), raising AssociateError — shared with the staff route."""
    from app.onboarding import maps_link

    def resolve(link: str):
        try:
            return maps_link.coordinates_from_link(link, who=who)
        except maps_link.LinkError as exc:
            body = exc.body()
            raise AssociateError(exc.message, code=exc.code, status_code=exc.status_code, field="link",
                                 extra={k: v for k, v in body.items() if k not in ("code", "message", "field")})
    return resolve


@router.post("/{onboarding_id}/site-location")
def set_site_location(onboarding_id: int, body: SiteLocationRequest, db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Set the customer's site location (owner only; draft/submitted — a submitted
    customer returns to draft, like any edit). One fix per call: phone GPS, a camera
    photo's GPS fix (recommended), a Google Maps link, or pasted coordinates. The new
    location is UNCONFIRMED until POST .../site-location/confirm."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    who = service.actor(associate)
    try:
        location_warnings = site_location.set_location(db, onboarding, who, body.root.model_dump(),
                                                       resolve_link=link_resolver(who))
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    data = serialize.onboarding(db, onboarding, viewer_is_owner=True)
    return {**data, "warnings": data["warnings"] + location_warnings}


@router.post("/{onboarding_id}/site-location/confirm")
def confirm_site_location(onboarding_id: int, db: Session = Depends(get_db),
                          current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The associate confirms the location is the customer's house (required to submit,
    even for a precise GPS fix). 409 site_location_not_set when there is none."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    try:
        site_location.confirm(db, onboarding, service.actor(associate))
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    data = serialize.onboarding(db, onboarding, viewer_is_owner=True)
    return {**data, "warnings": data["warnings"] + site_location.location_warnings(db, onboarding)}


@router.get("/{onboarding_id}/progress")
def progress_timeline(onboarding_id: int, db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The project's sales progress, stage by stage (anyone who can view the customer)."""
    from app.onboarding import progress

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return progress.timeline(db, onboarding)


@router.post("/{onboarding_id}/request-formal-proposal")
def request_formal_proposal(onboarding_id: int, db: Session = Depends(get_db),
                            current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Ask Grid Nova to prepare the FORMAL proposal (owner only; needs the approved site
    assessment). Creates no draft — staff prepare it. Says whether the customer record
    has everything a formal proposal needs."""
    from app.onboarding import formal, progress

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    try:
        state = formal.request_formal_proposal(db, onboarding, service.actor(associate))
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {"request": state, "progress": progress.timeline(db, onboarding)}


class EstimateRequest(BaseModel):
    segment: Optional[str] = None
    roof_type: Optional[str] = Field(default=None, max_length=50)
    access_difficulty: Optional[str] = None
    is_housing_society: bool = False


@router.post("/{onboarding_id}/estimates", status_code=201)
def create_estimate(onboarding_id: int, body: EstimateRequest = EstimateRequest(), db: Session = Depends(get_db),
                    current: AuthenticatedAssociate = Depends(get_current_associate)):
    """An approximate, unsigned INDICATIVE estimate to show the customer — the same
    engine as the public Solar Calculator, on this customer's data. Not a proposal: no
    approval, no send-guard. Owner only; any time before the record is closed."""
    from app.onboarding import estimate

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    if onboarding.status == OnboardingStatus.closed:
        raise HTTPException(status_code=409, detail={"code": "not_editable",
                                                     "message": "This customer record is closed."})
    try:
        record = estimate.create(db, onboarding, service.actor(associate), segment=body.segment,
                                 roof_type=body.roof_type, access_difficulty=body.access_difficulty,
                                 is_housing_society=body.is_housing_society)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return estimate.serialize(record)


@router.get("/{onboarding_id}/estimates")
def list_estimates(onboarding_id: int, db: Session = Depends(get_db),
                   current: AuthenticatedAssociate = Depends(get_current_associate)):
    from app.onboarding import estimate

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    return {"items": [estimate.serialize(e) for e in estimate.list_for(db, onboarding)]}


@router.get("/{onboarding_id}/formal-proposal")
def get_formal_proposal(onboarding_id: int, db: Session = Depends(get_db),
                        current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The latest formal proposal for this customer, if Grid Nova has prepared one —
    with a download link once it is approved, so it can be delivered in person. A
    territory viewer sees its status only: no price (money is owner-only) and no PDF
    unless territory_viewers_see_documents is on."""
    from app.onboarding import config as onboarding_config, formal

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_view(associate, onboarding)
    owner = onboarding.owner_associate_id == associate.id
    data = formal.serialize_latest(db, onboarding,
                                   with_pdf=owner or onboarding_config.territory_viewers_see_documents())
    if not owner:
        data["total_value"] = None
    if data.get("pdf"):
        service.audit_document_link(db, onboarding, service.actor(associate), "formal_proposal_pdf",
                                    str(data.get("proposal_id")))
        db.commit()
    return data


class InPersonAcceptanceRequest(BaseModel):
    proposal_id: int
    delivered_on: date
    note: str = Field(min_length=1, max_length=2000)
    reference: str = Field(min_length=1, max_length=200)


@router.post("/{onboarding_id}/proposal/in-person-acceptance")
def in_person_acceptance(onboarding_id: int, body: InPersonAcceptanceRequest, db: Session = Depends(get_db),
                         current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Record that the approved formal proposal was handed over and accepted in person
    (owner only). The alternative to the customer's online accept link."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    return _record_in_person(db, onboarding, service.actor(associate), body)


def _record_in_person(db: Session, onboarding: CustomerOnboarding, recorded_by: str,
                      body: InPersonAcceptanceRequest) -> dict:
    """Shared by the associate and staff routes."""
    from app.associates.service import today_ist
    from app.onboarding import formal, progress

    try:
        formal.record_in_person_acceptance(db, onboarding, recorded_by, proposal_id=body.proposal_id,
                                           delivered_on=body.delivered_on, note=body.note,
                                           reference=body.reference, today=today_ist())
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {"formal_proposal": formal.serialize_latest(db, onboarding), "progress": progress.timeline(db, onboarding)}


@router.get("/{onboarding_id}/agreement")
def get_agreement(onboarding_id: int, db: Session = Depends(get_db),
                  current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The customer agreement: whether it is generated/signed, with short-lived links."""
    from app.onboarding import agreement

    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_documents(associate, onboarding)
    record = agreement.get(db, onboarding)
    links = agreement.links(record)
    if links:
        service.audit_document_link(db, onboarding, service.actor(associate), "agreement", ",".join(sorted(links)))
        db.commit()
    return {**agreement.serialize(record), "links": links}


@router.post("/{onboarding_id}/agreement/signed")
def upload_signed_agreement(onboarding_id: int, file: UploadFile = File(...),
                            stamp_paper_number: str = Form(..., max_length=50),
                            stamp_paper_date: str = Form(...),
                            db: Session = Depends(get_db),
                            current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Upload the signed agreement scan with the stamp paper's number and date (owner)."""
    associate = _associate(db, current)
    onboarding = _load(db, onboarding_id)
    require_onboarding_edit(associate, onboarding)
    return _signed_upload(db, onboarding, service.actor(associate), file, stamp_paper_number, stamp_paper_date)


def _signed_upload(db: Session, onboarding: CustomerOnboarding, performed_by: str, file: UploadFile,
                   stamp_paper_number: str, stamp_paper_date: str) -> dict:
    """Shared by the associate and staff upload routes."""
    from datetime import date as _date

    from app.associates.service import today_ist
    from app.onboarding import agreement

    try:
        when = _date.fromisoformat(stamp_paper_date)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_stamp_paper_date", "field": "stamp_paper_date",
                                                     "message": "stamp_paper_date must be YYYY-MM-DD."})
    try:
        document = uploads.read_document_sync(file, "signed agreement", allowed=uploads.DOCUMENT_TYPES,
                                              max_bytes=MAX_FILE_BYTES)
    except uploads.UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    from app.storage import r2
    try:
        record = agreement.record_signed(db, onboarding, performed_by, content=document.content,
                                         content_type=document.content_type, filename=document.filename,
                                         stamp_paper_number=stamp_paper_number, stamp_paper_date=when,
                                         today=today_ist())
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    db.commit()
    return {**agreement.serialize(record), "links": agreement.links(record)}
