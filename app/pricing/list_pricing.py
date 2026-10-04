"""When and at what size the price list prices a system — shared by the public
calculator, associate estimates and formal proposals.

Applicability (config/pricing_policy.yaml, every value an unconfirmed default): West Bengal,
residential (housing society allowed), grid-connected without battery, a known
connection phase, and a size inside that phase's range. Anything else is a custom quote
with reason codes — never a guessed price.

Size snapping (calculator only): the calculated size is rounded UP to the next whole kW
within the phase range (`size_rounding: ceil`). A size the associate asked for is never
snapped: it must already be a whole kW inside the range (UnsupportedSize otherwise).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from app.pricing import policy, price_list

REASONS = ("outside_west_bengal", "segment_not_in_price_list", "battery_or_off_grid", "phase_unknown",
           "size_above_max", "three_phase_below_min", "size_below_min")


class UnsupportedSize(ValueError):
    """A requested (not calculated) size the price list cannot price."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class ListPricing:
    price_status: str                                   # list_price | custom_quote
    custom_quote_reasons: list[str] = field(default_factory=list)
    phase: Optional[str] = None                         # SINGLE | THREE
    recommended_kw_exact: Optional[float] = None
    priced_kw: Optional[int] = None
    packages: dict = field(default_factory=dict)        # package → PriceQuote

    @property
    def priced_kw_differs(self) -> bool:
        return self.priced_kw is not None and self.recommended_kw_exact is not None \
            and float(self.priced_kw) != float(self.recommended_kw_exact)


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def phase_of(connection_phase: Optional[str]) -> Optional[str]:
    return price_list.PHASE_FOR_CONNECTION.get(_norm(connection_phase))


def applicability_reasons(*, state: Optional[str], segment: Optional[str], is_housing_society: bool,
                          connection_type: Optional[str], battery_storage: bool,
                          connection_phase: Optional[str]) -> list[str]:
    rules = policy.applicability()
    reasons = []
    if _norm(state) not in {_norm(s) for s in rules["states"]}:
        reasons.append("outside_west_bengal")
    if _norm(segment) not in {_norm(s) for s in rules["segments"]} or (
            is_housing_society and not rules["housing_society_allowed"]):
        reasons.append("segment_not_in_price_list")
    if _norm(connection_type or "on_grid") not in rules["connection_types"] or (
            battery_storage and not rules["battery_allowed"]):
        reasons.append("battery_or_off_grid")
    if rules["require_known_phase"] and phase_of(connection_phase) is None:
        reasons.append("phase_unknown")
    return reasons


def snap(exact_kw: float, phase: str) -> tuple[Optional[int], Optional[str]]:
    """(priced_kw, None) or (None, reason) — ceil to a whole kW within the phase range."""
    r = price_list.phase_range(phase)
    kw = max(1, math.ceil(round(float(exact_kw), 6)))
    if kw > r["max_kw"]:
        return None, "size_above_max"
    if kw < r["min_kw"]:
        return None, "three_phase_below_min" if phase == "THREE" else "size_below_min"
    return kw, None


def evaluate(*, state: Optional[str], segment: Optional[str], is_housing_society: bool = False,
             connection_type: Optional[str] = None, battery_storage: bool = False,
             connection_phase: Optional[str] = None, exact_kw: Optional[float] = None,
             requested_kw: Optional[float] = None) -> ListPricing:
    """List prices for both packages, or a custom quote with reasons. `requested_kw`
    (the associate's override) raises UnsupportedSize when the configuration is
    otherwise list-priceable but that size is not in the table."""
    reasons = applicability_reasons(state=state, segment=segment, is_housing_society=is_housing_society,
                                    connection_type=connection_type, battery_storage=battery_storage,
                                    connection_phase=connection_phase)
    phase = phase_of(connection_phase)
    result = ListPricing(price_status="custom_quote", phase=phase,
                         recommended_kw_exact=float(requested_kw if requested_kw is not None else exact_kw)
                         if (requested_kw is not None or exact_kw is not None) else None)
    priced = None
    if phase is not None:
        if requested_kw is not None:
            q = price_list.quote("NOVA_ASSURED", phase, requested_kw)
            if isinstance(q, price_list.Unsupported):
                if not reasons:
                    raise UnsupportedSize(q.reason, f"A {requested_kw:g} kW {phase.lower()}-phase system is not in "
                                                    f"Grid Nova's price list: {q.detail}.")
                if q.reason in REASONS:
                    reasons.append(q.reason)
            else:
                priced = q.kw
        elif exact_kw is not None:
            priced, why = snap(exact_kw, phase)
            if why:
                reasons.append(why)
    result.custom_quote_reasons = reasons
    if reasons or priced is None:
        return result
    result.price_status = "list_price"
    result.priced_kw = priced
    result.packages = {p: price_list.quote(p, phase, priced) for p in price_list.PACKAGES}
    return result
