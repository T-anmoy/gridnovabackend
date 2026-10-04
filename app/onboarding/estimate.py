"""The onboarding-time INDICATIVE estimate an associate shows the customer.

Not a proposal: no approval, no send-guard, no sign-off, no progress stage, and it
never feeds the formal proposal or the agreement. The numbers come from exactly the
engine behind the public Solar Calculator (app/pricing/instant_estimate.py — sizing,
Grid Nova's price list for both packages, PM Surya Ghar subsidy), applied to this
customer's onboarding data, so an associate shows the same figures the public website
would. Each estimate is kept as shown (inputs and result), so there is a record of what
the customer saw; the snapshot carries `price_list_version`, `priced_kw`, the package(s)
chosen on the onboarding (`chosen_packages`: Nova Assured for the standard package,
Nova Signature for premium, both for "as recommended") and both packages' figures.
Estimates made before the price list stay readable exactly as stored.

If the associate recorded a requested capacity (desired_output_kw), that size is priced
— and must be a whole kW in the price list's range for the connection phase (422
unsupported_size otherwise); it is never rounded. Otherwise the engine sizes from
monthly units, then the bill, then roof area, and rounds up to a whole kW.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    AuditLog, CustomerOnboarding, IndicativeEstimate, ProposalInputs, SiteType, TerritoryState, TimelineEventType,
)
from app.pricing import engine as pricing_engine
from app.pricing import instant_estimate

# Only where the site type states the segment outright; anything else is chosen by the
# associate rather than guessed (e.g. an institutional roof is not necessarily "government").
SEGMENT_FROM_SITE_TYPE = {SiteType.rooftop_residential: "residential", SiteType.rooftop_commercial: "commercial"}
ACCESS_DIFFICULTIES = ("easy", "moderate", "difficult")


def build_inputs(db: Session, onboarding: CustomerOnboarding, *, segment: Optional[str],
                 roof_type: Optional[str], access_difficulty: Optional[str],
                 is_housing_society: bool) -> instant_estimate.InstantEstimateInputs:
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    segment = segment or SEGMENT_FROM_SITE_TYPE.get(inputs.site_type)
    if segment is None:
        raise AssociateError(
            f"Choose a segment for this estimate ({', '.join(pricing_engine.KNOWN_SEGMENTS)}) — it cannot be "
            f"worked out from the site type '{inputs.site_type.value}'.",
            code="segment_required", status_code=422, field="segment")
    if segment not in pricing_engine.KNOWN_SEGMENTS:
        raise AssociateError(f"segment must be one of {list(pricing_engine.KNOWN_SEGMENTS)}.",
                             code="invalid_segment", status_code=422, field="segment")
    if access_difficulty is not None and access_difficulty not in ACCESS_DIFFICULTIES:
        raise AssociateError(f"access_difficulty must be one of {list(ACCESS_DIFFICULTIES)}.",
                             code="invalid_access_difficulty", status_code=422, field="access_difficulty")

    if inputs.desired_output_kw:
        method = "requested_size"
    elif inputs.avg_monthly_units_kwh:
        method = "by_units"
    elif inputs.avg_monthly_bill_inr:
        method = "by_bill"
    elif inputs.roof_area_sqft:
        method = "by_roof_area"
    else:
        raise AssociateError("Enter the requested kW, the monthly units, the monthly bill or the roof area "
                             "before making an estimate.", code="not_enough_to_size", status_code=422)
    state = db.get(TerritoryState, onboarding.state_id)
    return instant_estimate.InstantEstimateInputs(
        segment=segment,
        state=state.name,
        sizing_method=method,
        monthly_units_kwh=inputs.avg_monthly_units_kwh or None,
        monthly_bill_inr=inputs.avg_monthly_bill_inr or None,
        usable_roof_area_sqft=inputs.roof_area_sqft or None,
        connection_phase=inputs.connection_phase.value if inputs.connection_phase else None,
        roof_type=roof_type,
        floors=inputs.floors or None,
        shading=inputs.shading_condition.value if inputs.shading_condition else None,
        access_difficulty=access_difficulty,
        is_housing_society=is_housing_society,
        requested_size_kw=inputs.desired_output_kw if method == "requested_size" else None,
        connection_type=inputs.connection_type.value if inputs.connection_type else None,
        battery_storage=bool(inputs.battery_storage),
    )


def chosen_packages(inputs: ProposalInputs) -> list[str]:
    """The package(s) the onboarding's equipment package means; both when "as
    recommended by Grid Nova" or not chosen."""
    from app.pricing.price_list import PACKAGE_FOR_TIER, PACKAGES
    tier = inputs.equipment_tier.value if inputs.equipment_tier else None
    return [PACKAGE_FOR_TIER[tier]] if tier in PACKAGE_FOR_TIER else list(PACKAGES)


def create(db: Session, onboarding: CustomerOnboarding, performed_by: str, *, segment: Optional[str] = None,
           roof_type: Optional[str] = None, access_difficulty: Optional[str] = None,
           is_housing_society: bool = False) -> IndicativeEstimate:
    """Compute and keep one indicative estimate. Caller commits."""
    engine_inputs = build_inputs(db, onboarding, segment=segment, roof_type=roof_type,
                                 access_difficulty=access_difficulty, is_housing_society=is_housing_society)
    try:
        result = instant_estimate.calculate_instant_estimate(MemoryManager(db), engine_inputs)
    except instant_estimate.UnsupportedSize as exc:
        raise AssociateError(str(exc), code="unsupported_size", status_code=422, field="desired_output_kw",
                             extra={"reason": exc.reason})
    except ValueError as exc:
        raise AssociateError(str(exc), code="estimate_failed", status_code=422)
    snapshot = asdict(result)
    snapshot["chosen_packages"] = chosen_packages(db.get(ProposalInputs, onboarding.proposal_inputs_id))
    estimate = IndicativeEstimate(onboarding_id=onboarding.id, inputs=asdict(engine_inputs),
                                  result=snapshot, created_by=performed_by)
    db.add(estimate)
    db.flush()
    if result.price_status == "list_price":
        shown = "; ".join(f"{result.packages[p]['display_name']} INR {float(result.packages[p]['final']):,.0f}"
                          for p in snapshot["chosen_packages"])
        figure = f"{result.priced_kw} kW — {shown} (list price, before subsidy)"
    else:
        figure = f"{result.recommended_size_kw:g} kW — custom quote ({', '.join(result.custom_quote_reasons)})"
    db.add(AuditLog(entity_type="indicative_estimate", entity_id=str(estimate.id), action="created",
                    performed_by=performed_by,
                    after_value={"onboarding_id": onboarding.id, "size_kw": result.recommended_size_kw,
                                 "priced_kw": result.priced_kw, "price_status": result.price_status,
                                 "price_list_version": result.price_list_version,
                                 "chosen_packages": snapshot["chosen_packages"], "net_cost": result.net_cost}))
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.indicative_estimate_created,
        description=f"Indicative estimate shown: {figure}", created_by=performed_by)
    return estimate


def list_for(db: Session, onboarding: CustomerOnboarding) -> list[IndicativeEstimate]:
    return list(db.execute(select(IndicativeEstimate).where(IndicativeEstimate.onboarding_id == onboarding.id)
                           .order_by(IndicativeEstimate.id.desc())).scalars())


def serialize(e: IndicativeEstimate) -> dict:
    result = e.result or {}
    # Estimates made before the price list (PM-A) have none of these — null, never guessed.
    return {"estimate_id": e.id, "indicative": True, "created_at": e.created_at.isoformat(),
            "created_by": e.created_by, "inputs": e.inputs, "result": e.result,
            "price_status": result.get("price_status"), "price_list_version": result.get("price_list_version"),
            "priced_kw": result.get("priced_kw"), "chosen_packages": result.get("chosen_packages")}
