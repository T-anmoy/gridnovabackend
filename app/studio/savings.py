"""The optional customer savings page of a Studio proposal (BE-18 §6): computed only from
figures staff enter from the customer's own bill, never from a marketing claim.

    generation   the plant's annual units — the proposal's own figure (config/solar_estimate.yaml
                 units / kWp / month for the site's state, × 12)
    degradation  year 1 at 100 %, year 2 at 1 − first_year, then − annual_after a year (the same
                 curve shape as the solar calculator), per module technology (config/studio.yaml)
    consumption  the bill's average monthly units, or (average bill − fixed charges) ÷ tariff
    savings      self-consumed units × tariff × (1 + escalation)^(year − 1), plus exported
                 units × the export rate staff entered (0 unless they enter one)
    payback      effective cost (after subsidy where it applies) ÷ year-1 savings, one decimal

Amounts are Decimal, rounded to the rupee; units to the whole unit. None when the page
cannot be computed (no tariff, no consumption, no capacity).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.studio import config as studio_config

WHOLE = Decimal(1)
ONE_DP = Decimal("0.1")
YEARS = 25


def _rupees(value: Decimal) -> Decimal:
    return value.quantize(WHOLE, rounding=ROUND_HALF_UP)


def factor(year: int, first_year: Decimal, annual_after: Decimal) -> Decimal:
    if year <= 1:
        return Decimal(1)
    return 1 - first_year - (year - 2) * annual_after


def monthly_consumption(inputs) -> Optional[Decimal]:
    if inputs.monthly_units is not None:
        return Decimal(inputs.monthly_units)
    if inputs.monthly_bill is not None and inputs.tariff_per_unit:
        energy = Decimal(inputs.monthly_bill) - Decimal(inputs.fixed_charges or 0)
        return (energy / Decimal(inputs.tariff_per_unit)).quantize(WHOLE, rounding=ROUND_HALF_UP) if energy > 0 \
            else None
    return None


def compute(doc, figs: dict, effective_cost: Optional[Decimal]) -> Optional[dict]:
    inputs = doc.savings
    annual_units = (figs.get("generation") or {}).get("annual_units")
    if inputs is None or inputs.tariff_per_unit is None or not annual_units:
        return None
    monthly = monthly_consumption(inputs)
    if monthly is None:
        return None
    cfg = studio_config.load()["savings"]
    first, annual = studio_config.degradation(doc.system.module_technology)
    escalation = Decimal(str(cfg["tariff_escalation"]))
    export_rate = Decimal(inputs.export_rate) if inputs.export_rate is not None else Decimal(str(cfg["export_rate_default"]))
    tariff = Decimal(inputs.tariff_per_unit)
    consumption = monthly * 12
    rows, cumulative = [], Decimal(0)
    for year in range(1, YEARS + 1):
        gen = (Decimal(annual_units) * factor(year, first, annual)).quantize(WHOLE, rounding=ROUND_HALF_UP)
        self_used = min(gen, consumption)
        exported = gen - self_used
        saving = _rupees(self_used * tariff * (1 + escalation) ** (year - 1) + exported * export_rate)
        cumulative += saving
        rows.append({"year": year, "generation_units": int(gen), "self_consumed_units": int(self_used),
                     "exported_units": int(exported), "savings": str(saving), "cumulative": str(cumulative)})
    year1 = Decimal(rows[0]["savings"])
    payback = None
    if effective_cost is not None and year1 > 0:
        payback = str((Decimal(effective_cost) / year1).quantize(ONE_DP, rounding=ROUND_HALF_UP))
    shown = [r for r in rows if r["year"] in cfg["years_shown"]]
    return {
        "monthly_consumption_units": int(monthly), "tariff_per_unit": str(tariff),
        "year1_generation_units": rows[0]["generation_units"], "year1_savings": str(year1),
        "year1_monthly_savings": str(_rupees(year1 / 12)),
        "total_savings_25_years": str(cumulative), "payback_years": payback,
        "effective_cost": str(effective_cost) if effective_cost is not None else None,
        "table": shown,
        "assumptions": {"degradation_first_year": str(first), "degradation_annual_after": str(annual),
                        "tariff_escalation": str(escalation), "export_rate": str(export_rate),
                        "module_technology": doc.system.module_technology},
        "note": cfg["note"],
        "depreciation_line": cfg["depreciation_line"] if cfg["depreciation_line_enabled"] else None,
    }
