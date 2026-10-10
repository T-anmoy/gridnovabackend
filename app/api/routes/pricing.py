"""
Pricing API.

Customer-facing prices come ONLY from Grid Nova's price list (app/pricing/price_list.py,
applied by app/pricing/list_pricing.py):

  /estimate          public — the list price (Nova Assured final as total_inr, both
                     packages) or a custom quote with reasons
  /instant-estimate  public — the /solar-calculator flow (app/pricing/instant_estimate.py)
  /packages          public — packages, makes, technology, terms, phase ranges and
                     (when policy public_price_display is `exact`) the price table

/breakdown is Admin/Sales only and still returns the layered placeholder engine's
itemized PricingBreakdown (app/pricing/engine.py, PRC-1…7) for internal use — it is
never shown to a customer and is superseded by the price list for every list-priced
configuration. Nothing here returns a procurement-cost-derived figure.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api import input_bounds as lim
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
    size_kw: Optional[float] = lim.bounded(gt=0, le=lim.MAX_KW)
    monthly_bill_inr: Optional[float] = lim.bounded(ge=0, le=lim.MAX_MONTHLY_INR)
    state: str = Field(min_length=1)
    roof_type: Optional[str] = None
    floors: Optional[int] = Field(default=None, ge=1, le=lim.MAX_FLOORS)
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
    connection_phase: Optional[str] = None
    connection_type: Optional[str] = None
    battery_storage: bool = False
    is_housing_society: bool = False


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
    request: Request,
    db: Session = Depends(get_db),
):
    """Public, unauthenticated. The list price for the size given (or the size the
    monthly bill implies, rounded up to a whole kW): `total_inr` is Nova Assured's
    final (GST-inclusive) price and `packages` has both packages; outside the price
    list `total_inr` is null with `price_status: custom_quote`. `categories` is null —
    a list price has no category breakdown."""
    from app.pricing import list_pricing, price_list

    _check_rate_limit(request, _estimate_request_log)
    if body.segment.strip().lower() not in pricing_engine.KNOWN_SEGMENTS:
        raise HTTPException(status_code=422, detail=f"Unknown segment: {body.segment!r}.")
    lp = list_pricing.evaluate(state=body.state, segment=body.segment, is_housing_society=body.is_housing_society,
                               connection_type=body.connection_type, battery_storage=body.battery_storage,
                               connection_phase=body.connection_phase, exact_kw=body.resolved_size_kw())
    assured = lp.packages.get("NOVA_ASSURED")
    return {
        "indicative_estimate": True,
        "total_inr": float(assured.final) if assured else None,
        "categories": None,
        "price_status": lp.price_status,
        "custom_quote_reasons": lp.custom_quote_reasons,
        "recommended_kw_exact": lp.recommended_kw_exact,
        "priced_kw": lp.priced_kw,
        "priced_kw_differs": lp.priced_kw_differs,
        "phase": lp.phase,
        "packages": {p: {**q.amounts(), "display_name": price_list.display_name(p)} for p, q in lp.packages.items()},
        "gst_rule": price_list.gst_rule(),
        "price_list_version": price_list.version(),
        "destination": price_list.destination(),
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
# /estimate and /packages are public too and get the same per-IP limit, in their own
# buckets (BE-1 S10).
_estimate_request_log: defaultdict[str, list[datetime]] = defaultdict(list)
_packages_request_log: defaultdict[str, list[datetime]] = defaultdict(list)


def _check_rate_limit(request: Request, log: defaultdict) -> None:
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_INSTANT_ESTIMATE_RATE_WINDOW_SECONDS)
    log[ip] = [t for t in log[ip] if t > cutoff]
    if len(log[ip]) >= _INSTANT_ESTIMATE_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before trying again.")
    log[ip].append(now)


def _check_instant_estimate_rate_limit(request: Request) -> None:
    _check_rate_limit(request, _instant_estimate_request_log)


class InstantEstimateRequest(BaseModel):
    segment: str = Field(min_length=1)
    state: str = Field(min_length=1)
    sizing_method: str = Field(min_length=1)  # "by_units" | "by_bill" | "by_roof_area"
    monthly_units_kwh: Optional[float] = lim.bounded(gt=0, le=lim.MAX_MONTHLY_KWH)
    monthly_bill_inr: Optional[float] = lim.bounded(gt=0, le=lim.MAX_MONTHLY_INR)
    usable_roof_area_sqft: Optional[float] = lim.bounded(gt=0, le=lim.MAX_AREA_SQFT)
    connection_phase: Optional[str] = None
    roof_type: Optional[str] = None
    floors: Optional[int] = Field(default=None, ge=1, le=lim.MAX_FLOORS)
    shading: Optional[str] = None
    access_difficulty: Optional[str] = None
    is_remote: bool = False
    is_housing_society: bool = False
    # Grid-connected without battery unless said otherwise (the price list covers only that).
    connection_type: Optional[str] = None
    battery_storage: bool = False

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
    """Public, unauthenticated — the /solar-calculator flow: sizing (consumption
    and/or roof-area based), Grid Nova's list prices for both packages at the priced
    size, and the PM Surya Ghar subsidy shown separately with its conditions. Outside
    the price list: no price, `price_status: custom_quote` with reasons. See
    app/pricing/instant_estimate.py for every field."""
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
                connection_type=body.connection_type,
                battery_storage=body.battery_storage,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return instant_estimate_module.response(result)


@router.get("/packages")
def packages(request: Request):
    """Public — the two packages as Grid Nova's master data describes them: makes
    ("or equivalent", never models), panel technology, commercial terms, phase ranges
    and the price-list version. The price table is included only when the pricing
    policy's `public_price_display` is `exact`. Built once per process: it depends only
    on the two config files, which are themselves loaded once."""
    _check_rate_limit(request, _packages_request_log)
    return _packages_body()


@lru_cache(maxsize=1)
def _packages_body() -> dict:
    from app.pricing import policy, price_list

    data = price_list.load()
    terms = data["commercial_terms"]
    items = []
    for package in price_list.PACKAGES:
        eq = data["equipment"][package]
        items.append({
            "package": package,
            "display_name": price_list.display_name(package),
            "equipment_tier": data["packages"][package]["equipment_tier"],
            "equipment": {component: {"makes": list(spec.get("makes") or []),
                                      "or_equivalent": bool(spec.get("or_equivalent")),
                                      **({"technology": list(spec["technology"])} if spec.get("technology") else {})}
                          for component, spec in eq.items()},
        })
    body = {
        "price_list_version": data["version"],
        "destination": data["destination"],
        "currency": data["currency"],
        "packages": items,
        "phase_ranges": {phase: dict(r) for phase, r in data["phase_ranges"].items()},
        "gst_rule": price_list.gst_rule(),
        "terms": {"payment_schedule": [{"number": m["number"], "percent": str(m["percent"]), "label": m["label"]}
                                       for m in terms["payment_schedule"]],
                  "standard_warranty": terms["standard_warranty"],
                  "solar_panel_life_span": terms["solar_panel_life_span"],
                  "o_and_m": terms["o_and_m"],
                  "price_change_clause": data["price_change_clause"]},
        "price_display": policy.load()["public_price_display"],
    }
    if body["price_display"] == "exact":
        body["prices"] = [
            {"package": p, "phase": phase, "kw": kw, **price_list.quote(p, phase, kw).amounts()}
            for p in price_list.PACKAGES for phase, r in data["phase_ranges"].items()
            for kw in range(r["min_kw"], r["max_kw"] + 1)]
    return body
