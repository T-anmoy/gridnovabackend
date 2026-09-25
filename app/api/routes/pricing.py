"""
Pricing API — two endpoints, two audiences, over the same calculation
framework (app/pricing/engine.py).

/estimate is public and unauthenticated (the customer-facing configurator).
Its response is deliberately thin: a total and three broad category totals,
never the itemized breakdown, and never anything procurement-cost-derived —
see the security note on calculate_equipment_cost() in engine.py, and
TestPublicEstimateNeverLeaksProcurementCost in tests/test_pricing_api.py,
which checks this against a real, distinctive cost value, not just by reading
the code.

/breakdown is Admin/Sales only and returns the full itemized PricingBreakdown,
for internal use.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.pricing import engine as pricing_engine
from app.pricing import instant_estimate as instant_estimate_module
from app.pricing.engine import PricingInputs
from app.security.client_ip import client_ip

router = APIRouter(prefix="/api/v1/pricing")


class _BaseEstimateRequest(BaseModel):
    segment: str = Field(min_length=1)
    size_kw: Optional[float] = Field(default=None, gt=0)
    monthly_bill_inr: Optional[float] = Field(default=None, ge=0)
    state: str = Field(min_length=1)
    roof_type: Optional[str] = None
    floors: Optional[int] = Field(default=None, ge=1)
    shading: Optional[str] = None
    is_remote: bool = False

    @model_validator(mode="after")
    def _one_sizing_input_required(self):
        if self.size_kw is None and self.monthly_bill_inr is None:
            raise ValueError("Provide either size_kw or monthly_bill_inr.")
        return self

    def resolved_size_kw(self) -> float:
        if self.size_kw is not None:
            return self.size_kw
        return pricing_engine.estimate_size_kw_from_monthly_bill(self.monthly_bill_inr)


class PublicEstimateRequest(_BaseEstimateRequest):
    pass


class BreakdownRequest(_BaseEstimateRequest):
    site_type: Optional[str] = None
    requires_net_metering: bool = False
    equipment_selection: list[int] = Field(default_factory=list)
    selected_services: list[str] = Field(default_factory=list)


def _pricing_inputs(body: _BaseEstimateRequest) -> PricingInputs:
    kwargs = dict(
        segment=body.segment,
        size_kw=body.resolved_size_kw(),
        state=body.state,
        roof_type=body.roof_type,
        floors=body.floors,
        shading=body.shading,
        is_remote=body.is_remote,
    )
    if isinstance(body, BreakdownRequest):
        kwargs.update(
            site_type=body.site_type,
            requires_net_metering=body.requires_net_metering,
            equipment_selection=body.equipment_selection,
            selected_services=body.selected_services,
        )
    return PricingInputs(**kwargs)


@router.post("/estimate")
def public_estimate(
    body: PublicEstimateRequest,
    db: Session = Depends(get_db),
):
    """Public, unauthenticated. Indicative only — a total and three broad
    category totals, never the itemized breakdown."""
    memory = MemoryManager(db)
    try:
        result = pricing_engine.calculate_full_estimate(memory, _pricing_inputs(body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "indicative_estimate": True,
        "total_inr": result.total,
        "categories": {
            "Equipment": result.category_totals["equipment"],
            "Installation": result.category_totals["installation"],
            "Other": result.category_totals["other"],
        },
    }


@router.post("/breakdown")
def full_breakdown(
    body: BreakdownRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Admin/Sales only — the full itemized PricingBreakdown, for internal use.

    Reuses can_adjust_pricing() (Admin+Sales) rather than a fresh inline
    check — it's the existing precedent for exactly this permission shape
    (PATCH /proposals/{id}/adjust-pricing), not a new rule.
    """
    if not employee.can_adjust_pricing():
        raise HTTPException(status_code=403, detail="Your role cannot view a full pricing breakdown.")

    memory = MemoryManager(db)
    try:
        result = pricing_engine.calculate_full_estimate(memory, _pricing_inputs(body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "base_rate_per_kw": result.base_rate_per_kw,
        "size_band_modifier": result.size_band_modifier,
        "base_installation_subtotal": result.base_installation_subtotal,
        "equipment_cost": result.equipment_cost,
        "complexity_modifier": result.complexity_modifier,
        "location_modifier": result.location_modifier,
        "installation_subtotal": result.installation_subtotal,
        "bos_addition": result.bos_addition,
        "civil_addition": result.civil_addition,
        "regulatory_addition": result.regulatory_addition,
        "optional_services_total": result.optional_services_total,
        "subtotal_before_tax": result.subtotal_before_tax,
        "gst_rate_percent": result.gst_rate_percent,
        "gst_amount": result.gst_amount,
        "total_inr": result.total,
        "category_totals": result.category_totals,
    }


# ── Instant estimate (expanded /solar-calculator flow) ─────────────────────────
# Public, unauthenticated, additive — /estimate and /breakdown above are
# completely untouched. Same hand-rolled per-IP rate limiter pattern as
# app/api/routes/public_intake.py (each public route file keeps its own,
# rather than sharing one — the established convention here).

_INSTANT_ESTIMATE_RATE_LIMIT = 20
_INSTANT_ESTIMATE_RATE_WINDOW_SECONDS = 60
_instant_estimate_request_log: defaultdict[str, list[datetime]] = defaultdict(list)


def _check_instant_estimate_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_INSTANT_ESTIMATE_RATE_WINDOW_SECONDS)
    _instant_estimate_request_log[ip] = [t for t in _instant_estimate_request_log[ip] if t > cutoff]
    if len(_instant_estimate_request_log[ip]) >= _INSTANT_ESTIMATE_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before trying again.")
    _instant_estimate_request_log[ip].append(now)


class InstantEstimateRequest(BaseModel):
    segment: str = Field(min_length=1)
    state: str = Field(min_length=1)
    sizing_method: str = Field(min_length=1)  # "by_units" | "by_bill" | "by_roof_area"
    monthly_units_kwh: Optional[float] = Field(default=None, gt=0)
    monthly_bill_inr: Optional[float] = Field(default=None, gt=0)
    usable_roof_area_sqft: Optional[float] = Field(default=None, gt=0)
    connection_phase: Optional[str] = None
    roof_type: Optional[str] = None
    floors: Optional[int] = Field(default=None, ge=1)
    shading: Optional[str] = None
    access_difficulty: Optional[str] = None
    is_remote: bool = False
    is_housing_society: bool = False

    @model_validator(mode="after")
    def _at_least_one_sizing_value(self):
        if (
            self.monthly_units_kwh is None
            and self.monthly_bill_inr is None
            and self.usable_roof_area_sqft is None
        ):
            raise ValueError(
                "Provide at least one of monthly_units_kwh, monthly_bill_inr, "
                "or usable_roof_area_sqft."
            )
        return self


@router.post("/instant-estimate")
def instant_estimate(
    body: InstantEstimateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public, unauthenticated — the expanded /solar-calculator flow: real
    sizing (consumption and/or roof-area based, phase-capped), fixed
    best-tier and access-difficulty pricing, and the real PM Surya Ghar
    subsidy. Always prices at the top tier — tier is not a customer-supplied
    choice on this path (see instant_estimate.py's FIXED_PUBLIC_TIER). An
    extra `tier` field in the request body, if a caller sends one, is simply
    ignored. Returns a complete, itemized breakdown, or an indicative range
    (never a false confident number) when shading was reported as
    significant."""
    _check_instant_estimate_rate_limit(request)

    memory = MemoryManager(db)
    try:
        result = instant_estimate_module.calculate_instant_estimate(
            memory,
            instant_estimate_module.InstantEstimateInputs(
                segment=body.segment,
                state=body.state,
                sizing_method=body.sizing_method,
                monthly_units_kwh=body.monthly_units_kwh,
                monthly_bill_inr=body.monthly_bill_inr,
                usable_roof_area_sqft=body.usable_roof_area_sqft,
                connection_phase=body.connection_phase,
                roof_type=body.roof_type,
                floors=body.floors,
                shading=body.shading,
                access_difficulty=body.access_difficulty,
                is_remote=body.is_remote,
                is_housing_society=body.is_housing_society,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "recommended_size_kw": result.recommended_size_kw,
        "sizing_detail": {
            "consumption_based_kw": result.consumption_based_kw,
            "area_based_kw": result.area_based_kw,
            "phase_capped": result.phase_capped,
        },
        "base_cost": result.base_cost,
        "adders": result.adders,
        "component_breakdown": result.component_breakdown,
        "subtotal_before_tax": result.subtotal_before_tax,
        "gst_amount": result.gst_amount,
        "subsidy_inr": result.subsidy_inr,
        "net_cost": result.net_cost,
        "is_indicative_range": result.is_indicative_range,
        "net_cost_range_min": result.net_cost_range_min,
        "net_cost_range_max": result.net_cost_range_max,
        "note": result.note,
    }
