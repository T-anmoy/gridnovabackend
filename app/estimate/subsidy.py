"""PM Surya Ghar central financial assistance (CFA) and state top-ups
(config/subsidies.yaml).

    a) residential (individual house): cumulative slabs on the CFA capacity, pro-rata,
       capped — general states 30,000 / 18,000 per kWp (max 78,000), special-category
       states 33,000 / 19,800 (max 85,800)
    b) housing society / RWA: rate × min(installed kWp, 3 kWp × houses, 500 kWp); without
       the number of houses the status is needs_number_of_houses (never ₹0, never a guess)
    c) state top-ups for residential individual consumers, inside their validity dates

CFA capacity: `nominal` (the kW sold) or `module_dc` (ceil(nominal W ÷ module Wp) × module
Wp) per central.cfa_capacity_basis. Top-ups use the nominal kW.

Only `confirmed` amounts count towards confirmed_total (what net_cost deducts);
`announced` amounts go to announced_total; nothing `unconfirmed` is ever added.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Optional

from app.estimate.config import state_key, subsidy_config
from app.estimate.tariff import ZERO, money

THOUSAND = Decimal(1000)


@dataclass
class SubsidyResult:
    status: str                       # eligible | needs_number_of_houses | not_eligible
    reason: Optional[str]             # not_eligible: segment_not_eligible | off_grid_not_eligible | no_capacity
    central: Optional[dict]
    society: Optional[dict]
    state: list = field(default_factory=list)
    confirmed_total: Decimal = money(ZERO)
    announced_total: Decimal = money(ZERO)
    conditions: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def public(self) -> dict:
        def s(v):
            return str(v) if isinstance(v, Decimal) else v
        return {"status": self.status, "reason": self.reason, "central": _strs(self.central),
                "society": _strs(self.society), "state": [_strs(t) for t in self.state],
                "confirmed_total": s(self.confirmed_total), "announced_total": s(self.announced_total),
                "conditions": list(self.conditions), "notes": list(self.notes)}


def _strs(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _strs(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strs(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def category(state: Optional[str]) -> str:
    return "special" if state_key(state) in subsidy_config()["central"]["special_keys"] else "general"


def cfa_capacity(kw: Decimal, module_wattage_wp: Optional[int]) -> tuple[Decimal, str, Optional[str]]:
    """(capacity in kWp the CFA is paid on, basis used, note)."""
    basis = subsidy_config()["central"]["cfa_capacity_basis"].value
    if basis == "nominal":
        return kw, "nominal", None
    if not module_wattage_wp:
        return kw, "nominal", "module_wattage_unknown_nominal_used"
    wp = Decimal(module_wattage_wp)
    modules = (kw * THOUSAND / wp).to_integral_value(rounding=ROUND_CEILING)
    return modules * wp / THOUSAND, "module_dc", None


def residential_slabs(capacity_kwp: Decimal, state: Optional[str]) -> tuple[list[dict], Decimal]:
    """The residential CFA by slab — [{from_kwp, to_kwp, kwp, rate_per_kwp, amount}] — and
    the capped total. The proposal prints these lines."""
    table = subsidy_config()["central"]["residential"][category(state)]
    rows, lower, total = [], ZERO, ZERO
    for up_to, rate in table["slabs"]:
        in_slab = min(max(capacity_kwp - lower, ZERO), up_to - lower)
        if in_slab > 0:
            amount = money(in_slab * rate)
            rows.append({"from_kwp": lower, "to_kwp": up_to, "kwp": in_slab, "rate_per_kwp": rate, "amount": amount})
            total += amount
        lower = up_to
    return rows, min(total, table["max_inr"])


def _society(capacity_kwp: Decimal, houses: int, cat: str) -> dict:
    soc = subsidy_config()["central"]["housing_society"]
    limits = {"installed": capacity_kwp, "houses": soc["kwp_per_house"] * houses, "max_kwp": soc["max_kwp"]}
    limited_by = min(limits, key=lambda k: (limits[k], ("installed", "houses", "max_kwp").index(k)))
    eligible = limits[limited_by]
    rate = soc["rate_per_kwp"][cat]
    return {"category": cat, "rate_per_kwp": rate, "number_of_houses": houses, "installed_kwp": capacity_kwp,
            "eligible_kwp": eligible, "limited_by": limited_by, "kwp_per_house": soc["kwp_per_house"],
            "max_kwp": soc["max_kwp"], "total": money(eligible * rate)}


def top_up_amount(rule: dict, kw: Decimal) -> Decimal:
    if rule["type"] == "per_kw_with_cap":
        return money(min(kw * rule["rate_per_kw"], rule["cap"]))
    amount = ZERO
    for min_kw, step_amount in rule["steps"]:
        if kw >= min_kw:
            amount = step_amount
    return money(amount)


def state_top_ups(state: Optional[str], kw: Decimal, on: date) -> list[dict]:
    """The state's top-ups valid on `on` (IST date), for a residential individual consumer."""
    out = []
    for item in subsidy_config()["state_top_ups"].get(state_key(state), ()):
        if on < item["valid_from"] or (item["valid_until"] is not None and on > item["valid_until"]):
            continue
        amount = top_up_amount(item["amount_rule"], kw)
        if amount <= 0:
            continue
        out.append({"state": item["state"], "amount": amount, "status": item["status"],
                    "note": item["eligibility_note"], "source": item["source"], "as_of": item["as_of"],
                    "valid_until": item["valid_until"], "rule": item["amount_rule"]["type"]})
    return out


def calculate(*, kw: Decimal, state: Optional[str], segment: Optional[str], is_housing_society: bool = False,
              number_of_houses: Optional[int] = None, connection_type: Optional[str] = None,
              module_wattage_wp: Optional[int] = None, on: date) -> SubsidyResult:
    cen = subsidy_config()["central"]
    seg = (segment or "").strip().lower()
    if seg not in cen["eligible_segments"]:
        return SubsidyResult("not_eligible", "segment_not_eligible", None, None)
    if (connection_type or "on_grid").strip().lower() not in cen["eligible_connection_types"]:
        return SubsidyResult("not_eligible", "off_grid_not_eligible", None, None)
    if kw <= 0:
        return SubsidyResult("not_eligible", "no_capacity", None, None)
    cat = category(state)
    capacity, basis, note = cfa_capacity(kw, module_wattage_wp)
    notes = [note] if note else []
    conditions = list(cen["conditions"])
    if is_housing_society:
        if not number_of_houses:
            return SubsidyResult("needs_number_of_houses", None, None, None, conditions=conditions, notes=notes)
        society = _society(capacity, number_of_houses, cat)
        return SubsidyResult("eligible", None, None, {**society, "basis": basis}, confirmed_total=society["total"],
                             conditions=conditions, notes=notes)
    slabs, total = residential_slabs(capacity, state)
    tops = state_top_ups(state, kw, on)
    confirmed = money(total + sum((t["amount"] for t in tops if t["status"] == "confirmed"), ZERO))
    announced = money(sum((t["amount"] for t in tops if t["status"] == "announced"), ZERO))
    central = {"category": cat, "basis": basis, "capacity_kwp": capacity, "slabs": slabs, "total": total,
               "max_inr": cen["residential"][cat]["max_inr"]}
    return SubsidyResult("eligible", None, central, None, state=tops, confirmed_total=confirmed,
                         announced_total=announced, conditions=conditions, notes=notes)
