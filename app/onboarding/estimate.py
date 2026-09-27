"""The onboarding-time INDICATIVE estimate an associate shows the customer.

Not a proposal: no approval, no send-guard, no sign-off, no progress stage, and it
never feeds the formal proposal or the agreement. The numbers come from exactly the
engine behind the public Solar Calculator (app/pricing/instant_estimate.py — sizing,
cost layers, fixed public tier, access difficulty, PM Surya Ghar subsidy), applied to
this customer's onboarding data, so an associate shows the same indicative figures the
public website would. Each estimate is kept as shown (inputs and result), so there is a
record of what the customer saw.

If the associate recorded a requested capacity (desired_output_kw), that size is priced;
otherwise the engine sizes from monthly units, then the bill, then roof area.
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
    )


def create(db: Session, onboarding: CustomerOnboarding, performed_by: str, *, segment: Optional[str] = None,
           roof_type: Optional[str] = None, access_difficulty: Optional[str] = None,
           is_housing_society: bool = False) -> IndicativeEstimate:
    """Compute and keep one indicative estimate. Caller commits."""
    engine_inputs = build_inputs(db, onboarding, segment=segment, roof_type=roof_type,
                                 access_difficulty=access_difficulty, is_housing_society=is_housing_society)
    try:
        result = instant_estimate.calculate_instant_estimate(MemoryManager(db), engine_inputs)
    except ValueError as exc:
        raise AssociateError(str(exc), code="estimate_failed", status_code=422)
    estimate = IndicativeEstimate(onboarding_id=onboarding.id, inputs=asdict(engine_inputs),
                                  result=asdict(result), created_by=performed_by)
    db.add(estimate)
    db.flush()
    figure = (f"INR {result.net_cost:,.0f}" if result.net_cost is not None
              else f"INR {result.net_cost_range_min:,.0f}–{result.net_cost_range_max:,.0f}")
    db.add(AuditLog(entity_type="indicative_estimate", entity_id=str(estimate.id), action="created",
                    performed_by=performed_by,
                    after_value={"onboarding_id": onboarding.id, "size_kw": result.recommended_size_kw,
                                 "net_cost": result.net_cost, "is_indicative_range": result.is_indicative_range}))
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.indicative_estimate_created,
        description=f"Indicative estimate shown: {result.recommended_size_kw:g} kW, {figure} after subsidy",
        created_by=performed_by)
    return estimate


def list_for(db: Session, onboarding: CustomerOnboarding) -> list[IndicativeEstimate]:
    return list(db.execute(select(IndicativeEstimate).where(IndicativeEstimate.onboarding_id == onboarding.id)
                           .order_by(IndicativeEstimate.id.desc())).scalars())


def serialize(e: IndicativeEstimate) -> dict:
    return {"estimate_id": e.id, "indicative": True, "created_at": e.created_at.isoformat(),
            "created_by": e.created_by, "inputs": e.inputs, "result": e.result}
