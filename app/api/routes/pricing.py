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

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.pricing import engine as pricing_engine
from app.pricing.engine import PricingInputs

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
