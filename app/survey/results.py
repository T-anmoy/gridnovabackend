"""Live survey results, computed on the saved site assessment (never stored): the size
the roof, the connection and the usage each allow, the suggested size, Grid Nova's
indicative list price for both packages with the subsidy (the PM-A engine — custom-quote
reasons when the configuration is outside the price list), and warnings.

Warnings are codes with params — the app words them. Sizes are kW (2 decimals); money
and ₹/unit figures are decimal strings from Decimal arithmetic only.

    code                         params
    requested_above_suggested    requested_kw, suggested_kw, limiting_factor
    load_enhancement_needed      required_kw, sanctioned_load_kw, max_by_load_kw
    implied_tariff_outside_range implied_tariff_inr_per_kwh, min, max
    units_bill_mismatch          field, entered, from_bills
    three_phase_below_min        kw, min_kw
    fragile_roof                 roof_material
    overhead_line                —
    roof_not_owned_no_noc        —
    society_noc_missing          society_noc_status
    area_implausible             reason (shadow_free_exceeds_total | roof_area_large), shadow_free_area_sqft,
                                 roof_area_sqft, max_plausible_sqft
    duplicate_consumer_number    — (the other associate is never named)
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import (
    CustomerOnboarding, OnboardingStatus, ProposalInputs, RoofMaterial, SocietyNocStatus, SurveyVariant,
    TerritoryState,
)
from app.survey import config

TWO = Decimal("0.01")
FACTORS = ("area", "load", "usage")   # tie order for limiting_factor
SEGMENT = {SurveyVariant.individual_house: ("residential", False),
           SurveyVariant.housing_society: ("residential", True),
           SurveyVariant.commercial_roof: ("commercial", False)}


def _kw(value: Optional[Decimal]) -> Optional[float]:
    return float(value.quantize(TWO, ROUND_HALF_UP)) if value is not None else None


def _dec(value) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


def _w(code: str, **params) -> dict:
    return {"code": code, "params": params}


def sizes(inputs: ProposalInputs, state: str) -> dict:
    from app.pricing import sizing

    cfg = config.load()
    by_area = by_load = by_usage = None
    if inputs.shadow_free_area_sqft is not None:
        by_area = _dec(inputs.shadow_free_area_sqft) / _dec(cfg["area_sqft_per_kw"])
    if inputs.sanctioned_load_kw:
        by_load = _dec(inputs.sanctioned_load_kw) * _dec(cfg["load_cap_ratio"])
    if inputs.avg_monthly_units_kwh:
        by_usage = _dec(sizing.estimate_size_from_units(inputs.avg_monthly_units_kwh, state))
    elif inputs.avg_monthly_bill_inr:
        by_usage = _dec(sizing.estimate_size_from_bill(inputs.avg_monthly_bill_inr, state))
    candidates = {k: v for k, v in zip(FACTORS, (by_area, by_load, by_usage)) if v is not None}
    suggested = limiting = None
    if candidates:
        limiting = min(candidates, key=lambda k: (candidates[k], FACTORS.index(k)))
        suggested = int(math.floor(candidates[limiting]))
    return {"max_by_area_kw": _kw(by_area), "max_by_load_kw": _kw(by_load), "recommended_by_usage_kw": _kw(by_usage),
            "suggested_kw": suggested, "limiting_factor": limiting,
            "target_offset_percent": sizing.DEFAULT_TARGET_OFFSET_PERCENT}


def bills(inputs: ProposalInputs) -> dict:
    """Implied ₹/unit over the months that have both units and a bill."""
    both = [m for m in inputs.consumption_history or []
            if m.get("bill_inr") is not None and m.get("units_kwh")]
    tariff = None
    if both:
        tariff = (sum(_dec(m["bill_inr"]) for m in both) / sum(_dec(m["units_kwh"]) for m in both)).quantize(
            TWO, ROUND_HALF_UP)
    return {"months_recorded": len(inputs.consumption_history or []),
            "average_monthly_units_kwh": inputs.avg_monthly_units_kwh,
            "average_monthly_bill_inr": str(_dec(inputs.avg_monthly_bill_inr).quantize(TWO, ROUND_HALF_UP))
            if inputs.avg_monthly_bill_inr is not None else None,
            "implied_tariff_inr_per_kwh": str(tariff) if tariff is not None else None}


def estimate(inputs: ProposalInputs, state: str, kw: Optional[int]) -> dict:
    """Indicative list price for both packages at `kw` (PM-A engine), or a custom quote
    with reasons. Never a guessed price."""
    from app.pricing import instant_estimate, list_pricing
    from app.pricing.subsidy import calculate_pm_surya_ghar_subsidy

    if not kw:
        return {"indicative": True, "size_kw": kw, "price_status": "not_enough_to_size", "custom_quote_reasons": [],
                "priced_kw": None, "phase": None, "packages": {}, "subsidy_inr": None, "subsidy_conditions": []}
    segment, society = SEGMENT[inputs.survey_variant or SurveyVariant.individual_house]
    lp = list_pricing.evaluate(
        state=state, segment=segment, is_housing_society=society,
        connection_type=inputs.connection_type.value if inputs.connection_type else None,
        battery_storage=bool(inputs.battery_storage),
        connection_phase=inputs.connection_phase.value if inputs.connection_phase else None, exact_kw=kw)
    subsidy = calculate_pm_surya_ghar_subsidy(size_kw=lp.priced_kw or kw, segment=segment,
                                              is_housing_society=society)
    from app.pricing import price_list
    return {"indicative": True, "size_kw": kw, "price_status": lp.price_status,
            "custom_quote_reasons": list(lp.custom_quote_reasons), "priced_kw": lp.priced_kw, "phase": lp.phase,
            "packages": instant_estimate.package_amounts(lp, subsidy),
            "subsidy_inr": str(Decimal(str(subsidy)).quantize(TWO, ROUND_HALF_UP)),
            "subsidy_conditions": list(instant_estimate.SUBSIDY_CONDITIONS) if subsidy else [],
            "price_list_version": price_list.version()}


def _duplicate_consumer(db: Session, onboarding: CustomerOnboarding) -> bool:
    if not onboarding.consumer_number_key:
        return False
    return db.execute(select(CustomerOnboarding.id).where(
        CustomerOnboarding.consumer_number_key == onboarding.consumer_number_key,
        CustomerOnboarding.id != onboarding.id,
        CustomerOnboarding.owner_associate_id != onboarding.owner_associate_id,
        CustomerOnboarding.status != OnboardingStatus.closed).limit(1)).first() is not None


def warnings(db: Session, onboarding: CustomerOnboarding, inputs: ProposalInputs, size: dict, bill: dict) -> list:
    from app.pricing import price_list
    from app.survey.validation import averages

    cfg = config.load()
    out = []
    requested, suggested = inputs.desired_output_kw, size["suggested_kw"]
    if requested and suggested is not None and requested > suggested:
        out.append(_w("requested_above_suggested", requested_kw=requested, suggested_kw=suggested,
                      limiting_factor=size["limiting_factor"]))
    needed = requested or size["recommended_by_usage_kw"]
    if needed and size["max_by_load_kw"] is not None and needed > size["max_by_load_kw"]:
        out.append(_w("load_enhancement_needed", required_kw=needed, sanctioned_load_kw=inputs.sanctioned_load_kw,
                      max_by_load_kw=size["max_by_load_kw"]))
    tariff = cfg["implied_tariff_inr_per_kwh"]
    if bill["implied_tariff_inr_per_kwh"] is not None and not (
            _dec(tariff["min"]) <= Decimal(bill["implied_tariff_inr_per_kwh"]) <= _dec(tariff["max"])):
        out.append(_w("implied_tariff_outside_range", implied_tariff_inr_per_kwh=bill["implied_tariff_inr_per_kwh"],
                      min=tariff["min"], max=tariff["max"]))
    if inputs.consumption_history:
        from_units, from_bill = averages(inputs.consumption_history)
        ratio = _dec(cfg["average_mismatch_ratio"])
        for field, entered, derived in (("avg_monthly_units_kwh", inputs.avg_monthly_units_kwh, from_units),
                                        ("avg_monthly_bill_inr", inputs.avg_monthly_bill_inr, from_bill)):
            if entered is not None and derived and abs(_dec(entered) - _dec(derived)) > _dec(derived) * ratio:
                out.append(_w("units_bill_mismatch", field=field, entered=str(_dec(entered)),
                              from_bills=str(_dec(derived))))
    kw = requested or suggested
    min_three = price_list.phase_range("THREE")["min_kw"]
    if inputs.connection_phase is not None and inputs.connection_phase.value == "three_phase" and kw \
            and kw < min_three:
        out.append(_w("three_phase_below_min", kw=kw, min_kw=min_three))
    if inputs.roof_material in (RoofMaterial.tin_shed, RoofMaterial.asbestos):
        out.append(_w("fragile_roof", roof_material=inputs.roof_material.value))
    if "overhead_line" in (inputs.roof_obstacles or []):
        out.append(_w("overhead_line"))
    if inputs.roof_ownership_confirmed is False and inputs.tenant_noc_available is not True:
        out.append(_w("roof_not_owned_no_noc"))
    if inputs.survey_variant == SurveyVariant.housing_society and inputs.society_noc_status not in (
            SocietyNocStatus.obtained, SocietyNocStatus.not_required):
        out.append(_w("society_noc_missing",
                      society_noc_status=inputs.society_noc_status.value if inputs.society_noc_status else None))
    limit = cfg["max_plausible_roof_area_sqft"][(inputs.survey_variant or SurveyVariant.individual_house).value]
    if inputs.shadow_free_area_sqft is not None and inputs.roof_area_sqft is not None \
            and inputs.shadow_free_area_sqft > inputs.roof_area_sqft:
        out.append(_w("area_implausible", reason="shadow_free_exceeds_total",
                      shadow_free_area_sqft=inputs.shadow_free_area_sqft, roof_area_sqft=inputs.roof_area_sqft,
                      max_plausible_sqft=limit))
    elif inputs.roof_area_sqft is not None and inputs.roof_area_sqft > limit:
        out.append(_w("area_implausible", reason="roof_area_large", shadow_free_area_sqft=inputs.shadow_free_area_sqft,
                      roof_area_sqft=inputs.roof_area_sqft, max_plausible_sqft=limit))
    if _duplicate_consumer(db, onboarding):
        out.append(_w("duplicate_consumer_number"))
    return out


def compute(db: Session, onboarding: CustomerOnboarding, *, include_prices: bool = True) -> dict:
    """GET …/survey/results. `include_prices=False` (Engineers) leaves out the estimate."""
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    state = db.get(TerritoryState, onboarding.state_id).name
    size = sizes(inputs, state)
    bill = bills(inputs)
    cfg = config.load()
    data = {"onboarding_id": onboarding.id, "variant": (inputs.survey_variant or SurveyVariant.individual_house).value,
            **size, "requested_kw": inputs.desired_output_kw, "bills": bill,
            "assumptions": {"area_sqft_per_kw": cfg["area_sqft_per_kw"], "load_cap_ratio": cfg["load_cap_ratio"],
                            "target_offset_percent": size.pop("target_offset_percent")},
            "warnings": warnings(db, onboarding, inputs, size, bill)}
    data.pop("target_offset_percent", None)
    if include_prices:
        data["estimate"] = estimate(inputs, state, size["suggested_kw"])
    return data
