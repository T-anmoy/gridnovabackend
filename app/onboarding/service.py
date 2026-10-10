"""Associate-led customer onboarding.

An associate enters everything on the customer's behalf. Each onboarding reuses what
the platform already has — Customer (email-then-phone dedup), Lead (source associate /
preferred_associate), Project (reference ID) and the site assessment (ProposalInputs,
which staff already review) — and adds the associate-specific facts in
customer_onboardings.

Attribution: the first COMPLETE, STAFF-VERIFIED onboarding owns the customer for
attribution_lock_days. An onboarding from a different associate that matches an
existing one (same consumer number or mobile) is still saved — no associate's work is
thrown away — but it raises an OnboardingAttributionFlag for an Admin, and neither
onboarding can be verified while that flag is open. Nothing is ever reassigned
silently. Every change is audited; nothing is deleted.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError, Place, resolve_place
from app.memory.memory_manager import PLACEHOLDER_EMAIL_DOMAIN, MemoryManager, is_placeholder_email
from app.memory.models import (
    Associate,
    AssociateType,
    AttributionFlagStatus,
    AttributionResolution,
    AuditLog,
    Customer,
    CustomerOnboarding,
    GovernmentScheme,
    LeadSource,
    OnboardingAttributionFlag,
    OnboardingStatus,
    Project,
    ProposalInputs,
    TimelineEventType,
)
from app.onboarding import config, pincode, site_location

EDITABLE_STATUSES = (OnboardingStatus.draft, OnboardingStatus.submitted)


class DuplicateOnboardingError(AssociateError):
    code = "duplicate_onboarding"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit_document_link(db: Session, onboarding: CustomerOnboarding, performed_by: str, kind: str,
                        ref: Optional[str] = None, filename: Optional[str] = None) -> None:
    """Every signed link to a customer's document is recorded — who, which, when.
    Caller commits."""
    from app.memory.models import AuditLog
    db.add(AuditLog(entity_type="onboarding_document", entity_id=str(onboarding.id), action="link_issued",
                    performed_by=performed_by, after_value={"kind": kind, "ref": ref, "filename": filename}))


def mask_mobile(mobile: Optional[str]) -> Optional[str]:
    if not mobile:
        return mobile
    digits = re.sub(r"\D", "", mobile)
    return "******" + digits[-4:] if len(digits) >= 4 else "******"


def mask_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def actor(associate: Associate) -> str:
    return f"associate:{associate.associate_id}"


def consumer_key(consumer_number: Optional[str]) -> Optional[str]:
    if not consumer_number:
        return None
    key = re.sub(r"[\s\-/]", "", consumer_number).upper()
    return key or None


def _audit(db: Session, entity_id: int, action: str, performed_by: str, before: Any = None, after: Any = None) -> None:
    db.add(AuditLog(entity_type="customer_onboarding", entity_id=str(entity_id), action=action,
                    performed_by=performed_by, before_value=before, after_value=after))


# ── Warnings (never a rejection) ─────────────────────────────────────────────────

def warnings_for(inputs: ProposalInputs, district_lgd_code: int) -> list[dict]:
    sanity = config.load()["sanity"]
    out = []
    if inputs.scheme_name == GovernmentScheme.pm_surya_ghar and not inputs.dcr_panels_required:
        out.append({"code": "non_dcr_pm_surya_ghar",
                    "message": "Non-DCR panels are not eligible for the PM Surya Ghar subsidy. "
                               "Choose DCR panels if the customer wants the subsidy."})
    kw = inputs.desired_output_kw
    if kw and inputs.sanctioned_load_kw and kw > inputs.sanctioned_load_kw * sanity["max_kw_to_sanctioned_load_ratio"]:
        out.append({"code": "kw_exceeds_sanctioned_load",
                    "message": f"The requested {kw:g} kW is more than the sanctioned load of "
                               f"{inputs.sanctioned_load_kw:g} kW. Check both figures, or plan a load extension."})
    monthly_units = inputs.avg_monthly_units_kwh
    basis = "monthly consumption"
    if not monthly_units and inputs.avg_monthly_bill_inr and sanity["inr_per_kwh_for_bill_check"]:
        monthly_units = inputs.avg_monthly_bill_inr / sanity["inr_per_kwh_for_bill_check"]
        basis = "monthly bill"
    if kw and monthly_units:
        expected = kw * sanity["kwh_per_kw_per_month"]
        if expected > monthly_units * sanity["max_generation_to_consumption_ratio"]:
            out.append({"code": "kw_high_for_consumption",
                        "message": f"A {kw:g} kW system would generate about {expected:,.0f} kWh a month, well above "
                                   f"the customer's {basis} (about {monthly_units:,.0f} kWh). Check the requested size."})
    pin_warning = pincode.check(inputs.pin_code, district_lgd_code)
    if pin_warning:
        out.append(pin_warning)
    return out


# ── Duplicates and attribution ───────────────────────────────────────────────────

def _matches(db: Session, *, mobile: str, consumer_number_key: Optional[str],
             exclude_id: Optional[int] = None) -> list[tuple[CustomerOnboarding, list[str]]]:
    conditions = [CustomerOnboarding.customer_mobile == mobile]
    if consumer_number_key:
        conditions.append(CustomerOnboarding.consumer_number_key == consumer_number_key)
    q = select(CustomerOnboarding).where(or_(*conditions), CustomerOnboarding.status != OnboardingStatus.closed)
    if exclude_id is not None:
        q = q.where(CustomerOnboarding.id != exclude_id)
    out = []
    for other in db.execute(q.order_by(CustomerOnboarding.id)).scalars():
        on = []
        if consumer_number_key and other.consumer_number_key == consumer_number_key:
            on.append("consumer_number")
        if other.customer_mobile == mobile:
            on.append("mobile")
        out.append((other, on))
    return out


def _refuse_own_duplicate(matches, associate: Associate) -> None:
    for other, on in matches:
        if other.owner_associate_id == associate.id:
            raise DuplicateOnboardingError(
                f"You have already onboarded this customer (same {' and '.join(on).replace('_', ' ')}). "
                "Update that record instead.", field="customer_mobile" if on == ["mobile"] else "consumer_number",
            )


def _flag_conflicts(db: Session, memory: MemoryManager, onboarding: CustomerOnboarding, matches,
                    performed_by: str) -> list[OnboardingAttributionFlag]:
    flags = []
    now = _now()
    for other, on in matches:
        if other.owner_associate_id == onboarding.owner_associate_id:
            continue
        exists = db.execute(select(OnboardingAttributionFlag).where(
            OnboardingAttributionFlag.onboarding_id == onboarding.id,
            OnboardingAttributionFlag.existing_onboarding_id == other.id)).scalar_one_or_none()
        if exists:
            continue
        locked = bool(other.attribution_locked_until and _aware(other.attribution_locked_until) > now)
        flag = OnboardingAttributionFlag(onboarding_id=onboarding.id, existing_onboarding_id=other.id,
                                         matched_on=on, existing_was_locked=locked,
                                         status=AttributionFlagStatus.open, created_at=now)
        db.add(flag)
        db.flush()
        _audit(db, onboarding.id, "attribution_flagged", performed_by,
               after={"flag_id": flag.id, "existing_onboarding_id": other.id, "matched_on": on,
                      "existing_was_locked": locked})
        memory.add_timeline_event(
            lead_id=onboarding.lead_id, event_type=TimelineEventType.attribution_flagged,
            description=f"Matches onboarding #{other.id} from another associate ({', '.join(on)}) — "
                        "awaiting Admin review", created_by=performed_by)
        flags.append(flag)
    if flags:
        import app.notifications.notifier as notifier_module
        payload = {"onboarding_id": onboarding.id, "lead_id": onboarding.lead_id,
                   "flag_ids": [f.id for f in flags]}
        memory.notify_after_commit(lambda: notifier_module.notifier.notify("attribution_conflict", payload),
                                   f"attribution conflict onboarding {onboarding.id}")
    return flags


def open_flags(db: Session, onboarding_id: int) -> list[OnboardingAttributionFlag]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and onboarding_id in pre.open_flags:
        return list(pre.open_flags[onboarding_id])
    return list(db.execute(select(OnboardingAttributionFlag).where(
        OnboardingAttributionFlag.status == AttributionFlagStatus.open,
        or_(OnboardingAttributionFlag.onboarding_id == onboarding_id,
            OnboardingAttributionFlag.existing_onboarding_id == onboarding_id))).scalars())


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Create / update ──────────────────────────────────────────────────────────────

# Site-assessment fields an associate supplies, stored on ProposalInputs as-is.
INPUT_FIELDS = (
    "customer_type", "organisation_name", "gst_number", "project_family", "scheme_linked", "scheme_name",
    "pin_code", "full_address", "discom", "consumer_number", "site_type", "roof_area_sqft", "floors",
    "shading_condition", "structural_notes", "roof_ownership_confirmed", "roof_age_years",
    "avg_monthly_bill_inr", "avg_monthly_units_kwh", "consumption_history", "peak_load_kw",
    "sanctioned_load_kw", "connection_phase", "desired_output_kw", "load_notes", "discom_consumer_category",
    "consumer_priority_class", "connection_type", "net_metering_required", "dcr_panels_required",
    "ev_charging", "battery_storage", "equipment_tier", "additional_requirements",
)
ONBOARDING_FIELDS = ("connection_in_customer_name", "property_in_customer_name", "customer_timeline")
# On-site survey fields (S1, app/survey/) — also stored on ProposalInputs, under the same
# edit rules. Checked and partly derived by app/survey/validation.py.
SURVEY_FIELDS = (
    "survey_variant", "roof_accessible", "roof_access_note", "roof_material", "roof_material_other",
    "roof_orientation", "roof_obstacles", "roof_sections", "shadow_free_area_sqft", "roofs_available_count",
    "roofs_considered_count", "future_roof_expansion_planned", "water_available", "inverter_location",
    "indoor_wall_available", "distance_panel_to_inverter_m", "distance_inverter_to_lt_panel_m", "distance_method",
    "society_flat_count", "society_noc_status", "contract_demand_kva", "tenant_noc_available",
)
WRITABLE_INPUT_FIELDS = INPUT_FIELDS + SURVEY_FIELDS


def _place(db: Session, data: dict) -> Place:
    return resolve_place(db, state_code=data["state_lgd_code"], district_code=data["district_lgd_code"],
                         block_code=data.get("block_lgd_code"), field_prefix="", need_district=True,
                         need_block="if_any")


def _lead_fields(customer_name: str, mobile: str, email: Optional[str], inputs: ProposalInputs) -> dict:
    from app.memory.lead_fields import required_fields_from_inputs

    fields = {"name": customer_name, "phone": mobile, **required_fields_from_inputs(inputs)}
    if email:
        fields["email"] = email
    return fields


def create(db: Session, associate: Associate, data: dict) -> tuple[CustomerOnboarding, list[dict]]:
    """`data` is validated input (see the route's OnboardingCreate). Caller commits."""
    from app.memory.lead_fields import apply_lead_fields

    from app.survey import validation as survey_validation

    data = survey_validation.prepare(data, None)
    memory = MemoryManager(db)
    who = actor(associate)
    place = _place(db, data)
    key = consumer_key(data.get("consumer_number"))
    matches = _matches(db, mobile=data["customer_mobile"], consumer_number_key=key)
    _refuse_own_duplicate(matches, associate)

    email = data.get("customer_email")
    customer = memory.get_or_create_customer(
        email or f"associate_{uuid.uuid4().hex}@{PLACEHOLDER_EMAIL_DOMAIN}",
        data["customer_name"], performed_by=who, phone=data["customer_mobile"],
    )
    if not customer.name:
        memory.update_customer(customer.id, performed_by=who, name=data["customer_name"])
    project = memory.create_project(
        customer.id, performed_by=who, state=place.state.name, project_family=data["project_family"],
        capacity_kw=data.get("desired_output_kw"), location=f"{place.district.name}, {place.state.name}",
    )
    source = (LeadSource.preferred_associate if associate.associate_type == AssociateType.preferred_associate
              else LeadSource.associate)
    lead = memory.create_lead(customer.id, performed_by=who, project_id=project.id, source=source)
    inputs = memory.create_proposal_inputs(
        lead.id, performed_by=who, state=place.state.name, district=place.district.name,
        **{f: data[f] for f in WRITABLE_INPUT_FIELDS if f in data and data[f] is not None},
    )
    version, _ = config.current_notice()
    onboarding = CustomerOnboarding(
        lead_id=lead.id, customer_id=customer.id, project_id=project.id, proposal_inputs_id=inputs.id,
        owner_associate_id=associate.id, status=OnboardingStatus.draft,
        state_id=place.state.id, district_id=place.district.id, block_id=place.block.id if place.block else None,
        customer_mobile=data["customer_mobile"], consumer_number_key=key,
        notice_version=version, notice_acknowledged_at=_now(), notice_recorded_by=who,
        verification_call_completed=False, created_by=who,
        **{f: data.get(f) for f in ONBOARDING_FIELDS if f in data},
    )
    legacy_location = data.get("site_latitude") is not None
    if legacy_location:
        # A raw pair from an older client: kept, but unknown_legacy, unverified and
        # unconfirmed — the submit gate still applies (app/onboarding/site_location.py).
        site_location.apply_legacy(db, onboarding, data["site_latitude"], data["site_longitude"], who)
    db.add(onboarding)
    db.flush()
    _audit(db, onboarding.id, "created", who, after={
        "lead_id": lead.id, "customer_id": customer.id, "project_id": project.id,
        "proposal_inputs_id": inputs.id, "notice_version": version,
        "state_id": onboarding.state_id, "district_id": onboarding.district_id, "block_id": onboarding.block_id,
    })
    apply_lead_fields(memory, lead, _lead_fields(data["customer_name"], data["customer_mobile"], email, inputs),
                      performed_by=who, via="associate onboarding")
    if legacy_location:
        _audit(db, onboarding.id, "site_location_set", who, after=site_location._snapshot(onboarding))
        memory.add_timeline_event(lead_id=lead.id, event_type=TimelineEventType.site_location_updated,
                                  description="Site location entered directly (unverified) — awaiting the "
                                              "associate's confirmation", created_by=who)
    flags = _flag_conflicts(db, memory, onboarding, matches, who)
    from app.memory.models import ProjectStage
    from app.onboarding import progress
    progress.record(db, onboarding, ProjectStage.registered, who,
                    evidence={"onboarding_id": onboarding.id, "notice_version": version})
    _rescore(db, onboarding, who)
    warnings = warnings_for(inputs, place.district.lgd_code)
    if flags:
        warnings.append(conflict_warning())
    return onboarding, warnings


def conflict_warning() -> dict:
    return {"code": "attribution_review",
            "message": "This customer is already registered with Grid Nova through another associate. Grid Nova "
                       "will review who the customer belongs to; you will be told the outcome."}


def update(db: Session, onboarding: CustomerOnboarding, associate: Associate, changes: dict) -> list[dict]:
    """Apply an associate's edits. Only while draft/submitted; an edit to a submitted
    onboarding sends it back to draft (it must be submitted again). Caller commits."""
    from app.memory.lead_fields import apply_lead_fields

    if onboarding.status not in EDITABLE_STATUSES:
        raise AssociateError(f"This customer is {onboarding.status.value} and can no longer be edited here.",
                             code="not_editable", status_code=409)
    from app.survey import validation as survey_validation

    memory = MemoryManager(db)
    who = actor(associate)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    changes = survey_validation.prepare(changes, inputs)
    customer = db.get(Customer, onboarding.customer_id)
    before: dict = {}
    after: dict = {}

    if any(k in changes for k in ("state_lgd_code", "district_lgd_code", "block_lgd_code")):
        current = {"state_lgd_code": _lgd(db, "state", onboarding.state_id),
                   "district_lgd_code": _lgd(db, "district", onboarding.district_id),
                   "block_lgd_code": _lgd(db, "block", onboarding.block_id)}
        place = _place(db, {**current, **{k: v for k, v in changes.items() if k in current}})
        for attr, value in (("state_id", place.state.id), ("district_id", place.district.id),
                            ("block_id", place.block.id if place.block else None)):
            if getattr(onboarding, attr) != value:
                before[attr], after[attr] = getattr(onboarding, attr), value
                setattr(onboarding, attr, value)
        inputs.state, inputs.district = place.state.name, place.district.name

    mobile = changes.get("customer_mobile", onboarding.customer_mobile)
    key = consumer_key(changes["consumer_number"]) if "consumer_number" in changes else onboarding.consumer_number_key
    identity_changed = mobile != onboarding.customer_mobile or key != onboarding.consumer_number_key
    matches = []
    if identity_changed:
        matches = _matches(db, mobile=mobile, consumer_number_key=key, exclude_id=onboarding.id)
        _refuse_own_duplicate(matches, associate)

    for field in WRITABLE_INPUT_FIELDS:
        if field in changes and getattr(inputs, field) != changes[field]:
            before[field], after[field] = getattr(inputs, field), changes[field]
            setattr(inputs, field, changes[field])
    for field in ONBOARDING_FIELDS:
        if field in changes and getattr(onboarding, field) != changes[field]:
            before[field], after[field] = getattr(onboarding, field), changes[field]
            setattr(onboarding, field, changes[field])
    location_before = None
    if "site_latitude" in changes and (changes["site_latitude"], changes.get("site_longitude")) != (
            onboarding.site_latitude, onboarding.site_longitude):
        location_before, location_after = site_location.apply_legacy(
            db, onboarding, changes["site_latitude"], changes.get("site_longitude"), who)
        before["site_location"], after["site_location"] = location_before, location_after
    if identity_changed:
        before.update(customer_mobile=onboarding.customer_mobile, consumer_number_key=onboarding.consumer_number_key)
        after.update(customer_mobile=mobile, consumer_number_key=key)
        onboarding.customer_mobile, onboarding.consumer_number_key = mobile, key

    from app.survey import signatures
    cleared_before, cleared_after = signatures.clear_after_edit(inputs, set(after))
    before.update(cleared_before)
    after.update(cleared_after)

    customer_changes = {}
    if "customer_name" in changes and changes["customer_name"] != customer.name:
        customer_changes["name"] = changes["customer_name"]
    if "customer_mobile" in changes and changes["customer_mobile"] != customer.phone:
        customer_changes["phone"] = changes["customer_mobile"]
    if changes.get("customer_email") and is_placeholder_email(customer.email):
        customer_changes["email"] = changes["customer_email"]
    if customer_changes:
        memory.update_customer(customer.id, performed_by=who, **customer_changes)

    if before or customer_changes:
        if onboarding.status == OnboardingStatus.submitted:
            before["status"], after["status"] = "submitted", "draft"
            onboarding.status = OnboardingStatus.draft
            onboarding.submitted_at = None
        db.flush()
        _audit(db, onboarding.id, "updated", who, before=_jsonable(before), after=_jsonable(after))
        if location_before is not None:
            site_location._after_change(db, onboarding, location_before, who,
                                        "Site location entered directly (unverified) — awaiting the "
                                        "associate's confirmation")
        lead = memory.get_lead(onboarding.lead_id)
        email = None if is_placeholder_email(customer.email) else customer.email
        apply_lead_fields(memory, lead, _lead_fields(customer.name, customer.phone or mobile, email, inputs),
                          performed_by=who, via="associate onboarding update")
    flags = _flag_conflicts(db, memory, onboarding, matches, who) if matches else []
    _rescore(db, onboarding, who)
    warnings = warnings_for(inputs, _lgd(db, "district", onboarding.district_id))
    if flags or open_flags(db, onboarding.id):
        warnings.append(conflict_warning())
    return warnings


def _lgd(db: Session, level: str, pk: Optional[int]) -> Optional[int]:
    from app.memory.models import TerritoryBlock, TerritoryDistrict, TerritoryState

    if pk is None:
        return None
    return db.get({"state": TerritoryState, "district": TerritoryDistrict, "block": TerritoryBlock}[level], pk).lgd_code


def _jsonable(d: dict) -> dict:
    from decimal import Decimal
    return {k: (v.value if hasattr(v, "value") else str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


# ── Documents and completeness ───────────────────────────────────────────────────

def checklist(db: Session, onboarding: CustomerOnboarding) -> dict:
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    required = config.required_document_keys(
        scheme_linked=bool(inputs.scheme_linked),
        all_in_customer_name=onboarding.connection_in_customer_name and onboarding.property_in_customer_name,
    )
    present = sorted({r.get("type") for r in (inputs.document_refs or [])
                      if isinstance(r, dict) and r.get("storage_key")})
    return {"required": required, "present": present, "missing": [k for k in required if k not in present]}


def submit(db: Session, onboarding: CustomerOnboarding, associate: Associate) -> CustomerOnboarding:
    if onboarding.status != OnboardingStatus.draft:
        raise AssociateError(f"Only a draft can be submitted (this one is {onboarding.status.value}).",
                             code="invalid_transition", status_code=409)
    from app.survey import status as survey_status

    missing = checklist(db, onboarding)["missing"]
    gaps = site_location.submit_gaps(onboarding)
    survey_gaps = survey_status.submit_gaps(onboarding, db.get(ProposalInputs, onboarding.proposal_inputs_id))
    if missing or any(gaps.values()) or any(survey_gaps.values()):
        problems = []
        if missing:
            problems.append("upload every required document (" + ", ".join(missing) + ")")
        if gaps["missing_site_location"]:
            problems.append("set the customer's site location")
        elif gaps["site_location_unconfirmed"]:
            problems.append("confirm the site location is the customer's house")
        if survey_gaps["missing_survey_sections"]:
            problems.append("complete the site survey (" + ", ".join(survey_gaps["missing_survey_sections"]) + ")")
        if survey_gaps["survey_unsigned"]:
            problems.append("get the site survey signed (" + ", ".join(survey_gaps["survey_unsigned"]) + ")")
        raise AssociateError("Before submitting, " + "; ".join(problems) + ".", code="incomplete",
                             status_code=422, extra={"missing_documents": missing, **gaps, **survey_gaps})
    who = actor(associate)
    onboarding.status = OnboardingStatus.submitted
    onboarding.submitted_at = _now()
    db.flush()
    _audit(db, onboarding.id, "submitted", who, before={"status": "draft"}, after={"status": "submitted"})
    MemoryManager(db).add_timeline_event(lead_id=onboarding.lead_id, event_type=TimelineEventType.onboarding_submitted,
                                         description="Submitted by the associate for verification", created_by=who)
    _rescore(db, onboarding, who)
    return onboarding


# ── Staff actions ────────────────────────────────────────────────────────────────

def record_verification_call(db: Session, onboarding: CustomerOnboarding, employee_id: str,
                             note: str) -> CustomerOnboarding:
    """The staff verification call. Makes the onboarding verified and starts the
    attribution lock — the first verified onboarding owns the customer."""
    note = (note or "").strip()
    if not note:
        raise AssociateError("Record what the verification call confirmed.", code="note_required",
                             status_code=422, field="note")
    if onboarding.status != OnboardingStatus.submitted:
        raise AssociateError(f"Only a submitted onboarding can be verified (this one is {onboarding.status.value}).",
                             code="invalid_transition", status_code=409)
    if open_flags(db, onboarding.id):
        raise AssociateError("An attribution conflict on this customer is waiting for an Admin decision.",
                             code="attribution_conflict", status_code=409)
    now = _now()
    onboarding.verification_call_completed = True
    onboarding.verification_call_note = note
    onboarding.verified_by_employee_id = employee_id
    onboarding.verified_at = now
    onboarding.attribution_locked_until = now + timedelta(days=config.load()["attribution_lock_days"])
    onboarding.status = OnboardingStatus.verified
    db.flush()
    _audit(db, onboarding.id, "verified", employee_id, before={"status": "submitted"},
           after={"status": "verified", "attribution_locked_until": onboarding.attribution_locked_until.isoformat()})
    MemoryManager(db).add_timeline_event(lead_id=onboarding.lead_id, event_type=TimelineEventType.onboarding_verified,
                                         description="Verification call completed", created_by=employee_id)
    from app.memory.models import ProjectStage
    from app.onboarding import progress
    progress.record_automatically(db, lead_id=onboarding.lead_id, stage=ProjectStage.verified,
                                  recorded_by=employee_id,
                                  evidence={"verification_call": True, "verified_by": employee_id})
    _rescore(db, onboarding, employee_id)
    return onboarding


def resolve_flag(db: Session, flag: OnboardingAttributionFlag, resolution: AttributionResolution,
                 note: str, employee_id: str) -> OnboardingAttributionFlag:
    """An Admin decides which onboarding keeps the customer. The other is closed —
    kept as history, never deleted — and its attribution lock (if any) ends."""
    note = (note or "").strip()
    if not note:
        raise AssociateError("Explain the decision.", code="note_required", status_code=422, field="note")
    if flag.status != AttributionFlagStatus.open:
        raise AssociateError("This flag is already resolved.", code="invalid_transition", status_code=409)
    loser_id = flag.onboarding_id if resolution == AttributionResolution.keep_existing else flag.existing_onboarding_id
    loser = db.get(CustomerOnboarding, loser_id)
    now = _now()
    flag.status = AttributionFlagStatus.resolved
    flag.resolution = resolution
    flag.resolution_note = note
    flag.resolved_by_employee_id = employee_id
    flag.resolved_at = now
    if loser.status != OnboardingStatus.closed:
        before = {"status": loser.status.value, "attribution_locked_until":
                  loser.attribution_locked_until.isoformat() if loser.attribution_locked_until else None}
        loser.status = OnboardingStatus.closed
        loser.closed_reason = f"Attribution flag #{flag.id} resolved ({resolution.value}): {note}"
        loser.attribution_locked_until = None
        _audit(db, loser.id, "closed", employee_id, before=before,
               after={"status": "closed", "flag_id": flag.id, "resolution": resolution.value})
    db.flush()
    _audit(db, flag.onboarding_id, "attribution_resolved", employee_id,
           after={"flag_id": flag.id, "resolution": resolution.value, "note": note, "closed_onboarding_id": loser.id})
    for onboarding_id in (flag.onboarding_id, flag.existing_onboarding_id):
        _rescore(db, db.get(CustomerOnboarding, onboarding_id), employee_id)
    return flag


def _rescore(db: Session, onboarding: CustomerOnboarding, performed_by: str) -> None:
    from app.onboarding import scoring

    scoring.rescore(db, onboarding, performed_by)


def after_file_upload(db: Session, onboarding: CustomerOnboarding, performed_by: str) -> None:
    _rescore(db, onboarding, performed_by)


# ── Queries ──────────────────────────────────────────────────────────────────────

def get(db: Session, onboarding_id: int) -> Optional[CustomerOnboarding]:
    return db.get(CustomerOnboarding, onboarding_id)


def for_lead(db: Session, lead_id: int) -> Optional[CustomerOnboarding]:
    return db.execute(select(CustomerOnboarding).where(CustomerOnboarding.lead_id == lead_id)).scalar_one_or_none()


def project_reference(db: Session, onboarding: CustomerOnboarding) -> str:
    return db.get(Project, onboarding.project_id).reference_id
