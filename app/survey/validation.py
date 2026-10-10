"""Survey input rules beyond what the request schema checks: bills, roof sections, the
values the server derives from them, and cross-field limits. Every refusal is a 422
AssociateError with a code and the field — never a silent fix.

Derived values:
  * shadow_free_area_sqft — from roof_sections whenever there are sections (sending it
    as well is refused: derived_field); entered directly otherwise.
  * avg_monthly_units_kwh / avg_monthly_bill_inr — the mean of consumption_history when
    the same request sends the history without them.
Sums and means run in Decimal; stored numbers are rounded half-up.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from app.associates.service import AssociateError
from app.memory.models import ROOF_OBSTACLES, ProposalInputs
from app.survey import config

SQFT_PER_SQM = Decimal("10.7639104")
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
TWO = Decimal("0.01")
ONE = Decimal("0.1")


def _refuse(message: str, code: str, field: str, **extra) -> AssociateError:
    return AssociateError(message, code=code, status_code=422, field=field, extra=extra or None)


def _dec(value) -> Decimal:
    return Decimal(str(value))


def current_month() -> str:
    from app.associates.service import today_ist
    today = today_ist()
    return f"{today.year:04d}-{today.month:02d}"


def consumption_history(months: list[dict]) -> list[dict]:
    """Validated, sorted by month. Each entry needs units_kwh; bill_inr is optional.
    Numbers stay numbers (the existing staff site-assessment API reads this list)."""
    cfg = config.load()
    if len(months) > cfg["max_consumption_months"]:
        raise _refuse(f"Enter at most {cfg['max_consumption_months']} months of bills.", "too_many_months",
                      "consumption_history", max_months=cfg["max_consumption_months"])
    latest = current_month()
    seen: set[str] = set()
    out = []
    for i, m in enumerate(months):
        month = str(m.get("month") or "").strip()
        if not _MONTH.match(month) or int(month[:4]) < 2000:
            raise _refuse(f"Month {i + 1} must be written YYYY-MM.", "invalid_month", "consumption_history",
                          index=i)
        if month > latest:
            raise _refuse(f"{month} is in the future.", "future_month", "consumption_history", index=i)
        if month in seen:
            raise _refuse(f"{month} is entered twice.", "duplicate_month", "consumption_history", index=i)
        seen.add(month)
        units, bill = m.get("units_kwh"), m.get("bill_inr")
        if units is None:
            raise _refuse(f"Enter the units for {month}.", "units_required", "consumption_history", index=i)
        if _dec(units) < 0 or (bill is not None and _dec(bill) < 0):
            raise _refuse(f"{month} has a negative value.", "negative_value", "consumption_history", index=i)
        out.append({"month": month, "units_kwh": float(_dec(units).quantize(TWO, ROUND_HALF_UP)),
                    "bill_inr": float(_dec(bill).quantize(TWO, ROUND_HALF_UP)) if bill is not None else None})
    return sorted(out, key=lambda m: m["month"])


def averages(history: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(mean units, mean bill) over the months that have each."""
    units = [_dec(m["units_kwh"]) for m in history if m.get("units_kwh") is not None]
    bills = [_dec(m["bill_inr"]) for m in history if m.get("bill_inr") is not None]
    mean_units = float((sum(units) / len(units)).quantize(TWO, ROUND_HALF_UP)) if units else None
    mean_bill = float((sum(bills) / len(bills)).quantize(TWO, ROUND_HALF_UP)) if bills else None
    return mean_units, mean_bill


def roof_sections(sections: list[dict]) -> tuple[list[dict], float]:
    """(sections with metres as decimal strings, shadow-free area in sq ft)."""
    total = Decimal(0)
    out = []
    for i, s in enumerate(sections):
        length, width = _dec(s["length_m"]), _dec(s["width_m"])
        obstacle = _dec(s["obstacle_area_sqm"]) if s.get("obstacle_area_sqm") is not None else Decimal(0)
        if obstacle > length * width:
            raise _refuse(f"Section {i + 1}: the obstacle area is larger than the section.",
                          "obstacle_exceeds_section", "roof_sections", index=i)
        total += length * width - obstacle
        out.append({"label": s["label"].strip(), "length_m": str(length.quantize(TWO, ROUND_HALF_UP)),
                    "width_m": str(width.quantize(TWO, ROUND_HALF_UP)),
                    "obstacle_area_sqm": str(obstacle.quantize(TWO, ROUND_HALF_UP))
                    if s.get("obstacle_area_sqm") is not None else None})
    return out, float((total * SQFT_PER_SQM).quantize(ONE, ROUND_HALF_UP))


def prepare(changes: dict, inputs: Optional[ProposalInputs]) -> dict:
    """Validate the survey part of an associate's create/update and add the derived
    values. `inputs` is the stored site assessment (None on create). Returns a new dict."""
    cfg = config.load()
    out = dict(changes)
    for text_field in ("roof_access_note", "roof_material_other"):
        if text_field in out and isinstance(out[text_field], str):
            out[text_field] = out[text_field].strip() or None

    if out.get("consumption_history") is not None:
        out["consumption_history"] = consumption_history(out["consumption_history"])
        units, bill = averages(out["consumption_history"])
        if "avg_monthly_units_kwh" not in changes and units is not None:
            out["avg_monthly_units_kwh"] = units
        if "avg_monthly_bill_inr" not in changes and bill is not None:
            out["avg_monthly_bill_inr"] = bill

    stored_sections = bool(inputs is not None and inputs.roof_sections)
    if "roof_sections" in out:
        if out["roof_sections"]:
            if "shadow_free_area_sqft" in changes:
                raise _refuse("The shadow-free area is worked out from the roof sections — send one or the other.",
                              "derived_field", "shadow_free_area_sqft")
            out["roof_sections"], out["shadow_free_area_sqft"] = roof_sections(out["roof_sections"])
        else:
            out["roof_sections"] = None
            if "shadow_free_area_sqft" not in changes and stored_sections:
                out["shadow_free_area_sqft"] = None
    elif "shadow_free_area_sqft" in changes and stored_sections:
        raise _refuse("The shadow-free area is worked out from the roof sections; change the sections instead.",
                      "derived_field", "shadow_free_area_sqft")

    if out.get("roof_obstacles") is not None:
        chosen = set(out["roof_obstacles"])
        out["roof_obstacles"] = [t for t in ROOF_OBSTACLES if t in chosen]

    for field in ("distance_panel_to_inverter_m", "distance_inverter_to_lt_panel_m"):
        if out.get(field) is not None:
            value = _dec(out[field])
            if value > _dec(cfg["distance_max_m"]):
                raise _refuse(f"{field} is more than {cfg['distance_max_m']} m — check the measurement.",
                              "out_of_range", field, max_m=cfg["distance_max_m"])
            out[field] = value.quantize(TWO, ROUND_HALF_UP)

    def current(field: str) -> Any:
        return out[field] if field in out else (getattr(inputs, field) if inputs is not None else None)

    available, considered = current("roofs_available_count"), current("roofs_considered_count")
    if available is not None and considered is not None and considered > available:
        raise _refuse("More roofs are considered than are available.", "roofs_considered_exceeds_available",
                      "roofs_considered_count")
    return out
