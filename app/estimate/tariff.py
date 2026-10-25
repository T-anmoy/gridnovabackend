"""A month's electricity bill as a function of units — and its inverse.

Three bill models, most accurate first:

    tariff_table   a confirmed DISCOM table (config/solar_estimate.yaml): slab energy
                   charges, the fixed charge, electricity duty
    implied_rate   the customer's own bill ÷ units (both from their bill)
    flat_rate      Grid Nova's flat ₹/unit

A state free-unit scheme (Bihar: the first 125 units a month free) applies to every
model: the first F units are charged nothing, and the units above F are charged as they
would be anyway (for a table: the slab charges on all units minus those on the first F).

    energy(u)   table: E(u) − E(min(u, F)),  E = the slab charges
                rate:  rate × max(u − F, 0)
    bill(u)     table: fixed + energy(u) + duty;  rate: energy(u)  (fixed charges and duty
                are inside the customer's own rate; for the flat rate they are not modelled)

Bills are rounded to the paisa (ROUND_HALF_UP); everything else is exact Decimal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.estimate.config import TariffTable

PAISA = Decimal("0.01")
ZERO = Decimal(0)
HUNDRED = Decimal(100)


def money(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class BillModel:
    kind: str                          # tariff_table | implied_rate | flat_rate
    free_units: Decimal = ZERO
    rate: Optional[Decimal] = None     # implied_rate / flat_rate
    table: Optional[TariffTable] = None
    sanctioned_load_kw: Optional[Decimal] = None


def slab_energy(units: Decimal, table: TariffTable) -> Decimal:
    """E(u): each slab's rate on the units that fall inside it."""
    total, lower = ZERO, ZERO
    for slab in table.slabs:
        upper = slab.up_to_units
        if upper is None or units <= upper:
            return total + (units - lower) * slab.rate if units > lower else total
        total += (upper - lower) * slab.rate
        lower = upper
    return total


def energy(model: BillModel, units: Decimal) -> Decimal:
    units = max(units, ZERO)
    if model.kind == "tariff_table":
        return slab_energy(units, model.table) - slab_energy(min(units, model.free_units), model.table)
    return model.rate * max(units - model.free_units, ZERO)


def fixed_charge(model: BillModel) -> Optional[Decimal]:
    """The table's monthly fixed charge; None when it needs a sanctioned load that is not
    known. Zero for the rate models."""
    if model.kind != "tariff_table" or model.table.fixed_charge is None:
        return ZERO
    charge = model.table.fixed_charge
    if charge.type == "flat_per_month":
        return charge.value
    if model.sanctioned_load_kw is None:
        return None
    return charge.value * model.sanctioned_load_kw


def _total(model: BillModel, units: Decimal, fixed: Decimal) -> Decimal:
    e = energy(model, units)
    if model.kind != "tariff_table" or model.table.electricity_duty is None:
        return fixed + e
    duty = model.table.electricity_duty
    base = e if duty.type == "percent_of_energy" else e + fixed
    return fixed + e + base * duty.value / HUNDRED


def bill(model: BillModel, units: Decimal) -> Optional[Decimal]:
    """The month's bill for `units`, or None when the fixed charge cannot be known."""
    fixed = fixed_charge(model)
    if fixed is None:
        return None
    return money(_total(model, units, fixed))


def marginal_saving(model: BillModel, units: Decimal, solar_units: Decimal) -> Optional[Decimal]:
    """bill(units) − bill(max(units − solar, 0)): what solar takes off the bill, with the
    fixed charge unchanged (it cancels, so an unknown fixed charge does not matter)."""
    probe = model if fixed_charge(model) is not None else BillModel(
        kind=model.kind, free_units=model.free_units, rate=model.rate, table=model.table,
        sanctioned_load_kw=ZERO)
    before = bill(probe, units)
    after = bill(probe, max(units - solar_units, ZERO))
    return before - after


def units_from_bill(model: BillModel, amount: Decimal) -> tuple[Optional[Decimal], Optional[str]]:
    """(units, None) or (None, reason): the units whose bill is `amount`, solving the
    piecewise-linear bill exactly. Reasons: fixed_charge_needs_sanctioned_load,
    bill_below_fixed_charges, bill_within_free_units."""
    fixed = fixed_charge(model)
    if fixed is None:
        return None, "fixed_charge_needs_sanctioned_load"
    if model.kind != "tariff_table":
        if amount <= 0:
            return None, "bill_within_free_units" if model.free_units > 0 else "bill_below_fixed_charges"
        return model.free_units + amount / model.rate, None
    base = _total(model, ZERO, fixed)
    if amount < base:
        return None, "bill_below_fixed_charges"
    points = sorted({ZERO, model.free_units} | {s.up_to_units for s in model.table.slabs if s.up_to_units})
    for i, low in enumerate(points):
        high = points[i + 1] if i + 1 < len(points) else None
        at_low = _total(model, low, fixed)
        if high is not None:
            # Linear between breakpoints: interpolate exactly.
            at_high = _total(model, high, fixed)
            if not (at_low <= amount <= at_high):
                continue
            if at_high == at_low:
                return None, "bill_within_free_units"
            return low + (amount - at_low) * (high - low) / (at_high - at_low), None
        # Past the last breakpoint the bill rises at the last slab's rate.
        slope = _total(model, low + 1, fixed) - at_low
        if slope <= 0:
            return None, "bill_within_free_units"
        return low + (amount - at_low) / slope, None
    return None, "bill_below_fixed_charges"
