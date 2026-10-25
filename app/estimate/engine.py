"""The solar estimate: consumption → size, generation, subsidy, savings, payback and the
long-term figures — pure functions over config/solar_estimate.yaml and
config/subsidies.yaml. Prices are not computed here: callers pass the system cost from
Grid Nova's price list (app/pricing/list_pricing.py).

Formulas, exactly as implemented (Decimal throughout, ROUND_HALF_UP):

    per_kwp            units_per_kwp_per_month for the state (fallback: unconfirmed)
    consumption_kw     monthly_units × target_offset% ÷ per_kwp
                       (= annual units × offset ÷ annual generation per kWp; annual = × 12)
    area_kw            usable_roof_area_sqft ÷ sqft_per_kwp
    exact_kw           min(consumption_kw, area_kw), to 0.01 kW (roof_limited when the area wins)
    monthly_generation kW × per_kwp, to the whole kWh;  annual = monthly × 12
    coverage_percent   monthly_generation ÷ monthly_units × 100, to 0.1
    monthly_saving     bill(units) − bill(max(units − monthly_generation, 0))  (app/estimate/tariff.py)
                       + export credit (only a confirmed flat credit; default none)
    annual_saving      monthly_saving × 12
    net_cost           system cost − confirmed subsidies
    payback_years      net_cost ÷ annual_saving (year 1), to 0.1; null when the saving is ≤ 0
    year n factor      1 (n = 1); 1 − first_year% − (n − 2) × annual% (n ≥ 2)
    year n generation  annual_generation × factor, to the whole kWh
    year n saving      12 × saving(year n generation ÷ 12) × (1 + escalation%)^(n − 1), to the paisa

Consumption, most accurate first (`consumption_basis`):
    monthly_units_and_bill    units from the bill + the customer's own ₹/unit for savings
                              (bill ÷ units above any free allowance; outside the sanity
                              range → warning, the tariff table or flat rate instead)
    monthly_units             units; savings on the DISCOM table if confirmed, else the flat rate
    monthly_bill_tariff_table units solved from a confirmed DISCOM table
    monthly_bill_flat_rate    units = free allowance + bill ÷ the flat rate
    (none)                    roof area only: generation, no savings

Confidence: high = units given and a confirmed DISCOM tariff; medium = the customer's own
₹/unit, or a bill solved through a confirmed table; low = the flat rate, or no
consumption. A generation figure not confirmed for the state caps it at medium.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from app.estimate import subsidy as subsidy_rules
from app.estimate.config import Value, estimate_config, state_key, subsidy_config, version
from app.estimate.tariff import HUNDRED, ZERO, BillModel, bill, marginal_saving, money, units_from_bill

KW_STEP = Decimal("0.01")
UNITS_STEP = Decimal("0.01")
ONE_DP = Decimal("0.1")
WHOLE = Decimal(1)
TWELVE = Decimal(12)
HALF = Decimal("0.5")
LEVELS = ("low", "medium", "high")


def today_ist() -> date:
    from app.config import settings
    return datetime.now(ZoneInfo(settings.business_timezone)).date()


@dataclass(frozen=True)
class EstimateInputs:
    state: str
    segment: str = "residential"
    is_housing_society: bool = False
    monthly_units_kwh: Optional[Decimal] = None
    monthly_bill_inr: Optional[Decimal] = None
    usable_roof_area_sqft: Optional[Decimal] = None
    requested_kw: Optional[Decimal] = None
    discom: Optional[str] = None
    tariff_category: Optional[str] = None
    sanctioned_load_kw: Optional[Decimal] = None
    number_of_houses: Optional[int] = None
    module_wattage_wp: Optional[int] = None
    connection_type: Optional[str] = None
    on: Optional[date] = None


def to_decimal(value) -> Optional[Decimal]:
    """The JSON edge: an API float (or int / str) to Decimal via its text form; a whole
    float (JSON 3 arrives as 3.0) becomes a whole Decimal, so it reads back as "3"."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return Decimal(int(value))
    return Decimal(str(value))


# ── Generation ──────────────────────────────────────────────────────────────────

def generation_rate(state: Optional[str]) -> Value:
    gen = estimate_config()["generation"]
    return gen["states"].get(state_key(state), gen["fallback"])


def monthly_generation(kw: Decimal, state: Optional[str]) -> Decimal:
    return (kw * generation_rate(state).value).quantize(WHOLE, rounding=ROUND_HALF_UP)


def degradation_factor(year: int) -> Decimal:
    gen = estimate_config()["generation"]
    if year <= 1:
        return Decimal(1)
    return 1 - gen["first_year_percent"].value / HUNDRED - (year - 2) * gen["annual_percent"].value / HUNDRED


# ── Consumption and the bill model ──────────────────────────────────────────────

@dataclass
class Consumption:
    monthly_units: Optional[Decimal]
    monthly_bill: Optional[Decimal]
    basis: Optional[str]
    model: BillModel
    tariff_source: str                 # discom_tariff | customer_bill | flat_rate
    implied_rate: Optional[Decimal] = None
    table_name: Optional[str] = None   # "WBSEDCL/domestic" when a table was considered
    table_status: Optional[str] = None
    free_units: Optional[dict] = None
    warnings: list = field(default_factory=list)   # [(code, params)]


def free_units(state: Optional[str], discom: Optional[str]) -> Optional[dict]:
    """The free-unit scheme for this DISCOM (or, when the DISCOM is not one in config, the
    state): {key, value: Value}."""
    tariffs = estimate_config()["tariffs"]
    name = (discom or "").strip().upper()
    for key, scheme in tariffs["free_units"].items():
        if name in scheme["discoms"] or (name not in tariffs["discoms"] and state_key(state) == key):
            return {"key": key, "value": scheme["value"]}
    return None


def _table(discom: Optional[str], category: Optional[str], warnings: list):
    """(table or None, 'DISCOM/category' considered, its status)."""
    name = (discom or "").strip().upper()
    if not name:
        return None, None, None
    entry = estimate_config()["tariffs"]["discoms"].get(name)
    if entry is None:
        warnings.append(("unknown_discom", {"discom": discom}))
        return None, None, None
    cats = entry["categories"]
    if category and category in cats:
        chosen = cats[category]
    elif len(cats) == 1:
        chosen = next(iter(cats.values()))
    else:
        if any(t.usable for t in cats.values()):
            warnings.append(("tariff_category_needed", {"discom": name, "categories": sorted(cats)}))
        else:
            warnings.append(("tariff_table_not_available", {"discom": name, "category": None,
                                                            "status": "unconfirmed"}))
        return None, f"{name}/{'|'.join(sorted(cats))}", "unconfirmed"
    label = f"{name}/{chosen.category}"
    if not chosen.usable:
        warnings.append(("tariff_table_not_available", {"discom": name, "category": chosen.category,
                                                        "status": chosen.status}))
        return None, label, chosen.status
    return chosen, label, chosen.status


def consumption(inp: EstimateInputs) -> Consumption:
    cfg = estimate_config()["tariffs"]
    warnings: list = []
    table, label, table_status = _table(inp.discom, inp.tariff_category, warnings)
    scheme = free_units(inp.state, inp.discom)
    free = scheme["value"].value if scheme else ZERO
    flat = BillModel("flat_rate", free, rate=cfg["flat_rate"].value)
    table_model = BillModel("tariff_table", free, table=table, sanctioned_load_kw=inp.sanctioned_load_kw) \
        if table else None
    common = dict(table_name=label, table_status=table_status, free_units=scheme, warnings=warnings)
    units, amount = inp.monthly_units_kwh, inp.monthly_bill_inr

    if units is not None:
        implied = None
        if amount is not None:
            if units > free:
                implied = money(amount / (units - free))
                sane = cfg["sanity"]
                if sane["min"] <= implied <= sane["max"]:
                    return Consumption(units, amount, "monthly_units_and_bill",
                                       BillModel("implied_rate", free, rate=implied), "customer_bill", implied,
                                       **common)
                warnings.append(("implied_tariff_outside_range", {"implied_tariff_inr_per_kwh": str(implied),
                                                                  "min": str(sane["min"]), "max": str(sane["max"])}))
            else:
                warnings.append(("implied_rate_unavailable_within_free_units",
                                 {"monthly_units_kwh": str(units), "free_units": str(free)}))
        if table_model:
            return Consumption(units, amount, "monthly_units", table_model, "discom_tariff", implied, **common)
        return Consumption(units, amount, "monthly_units", flat, "flat_rate", implied, **common)

    if amount is not None:
        if table_model:
            solved, why = units_from_bill(table_model, amount)
            if solved is not None:
                return Consumption(solved.quantize(UNITS_STEP, rounding=ROUND_HALF_UP), amount,
                                   "monthly_bill_tariff_table", table_model, "discom_tariff", **common)
            warnings.append(("bill_not_solvable_with_tariff_table", {"reason": why}))
        solved, why = units_from_bill(flat, amount)
        if solved is None:
            warnings.append(("bill_not_solvable", {"reason": why}))
            return Consumption(None, amount, None, flat, "flat_rate", **common)
        return Consumption(solved.quantize(UNITS_STEP, rounding=ROUND_HALF_UP), amount, "monthly_bill_flat_rate",
                           flat, "flat_rate", **common)

    return Consumption(None, None, None, table_model or flat, "discom_tariff" if table_model else "flat_rate",
                       **common)


# ── Size ────────────────────────────────────────────────────────────────────────

@dataclass
class Sizing:
    exact_kw: Decimal
    consumption_kw: Optional[Decimal]      # unrounded
    area_kw: Optional[Decimal]             # unrounded
    roof_limited: bool
    sizing_basis: str                      # requested_size | roof_area | the consumption basis

    @property
    def legacy_half_kw(self) -> Decimal:
        """The pre-BE-14 `recommended_size_kw`: the raw size to the nearest 0.5 kW (ties to
        even, as Python's round() did), never below 0.5; a requested size as given."""
        if self.sizing_basis == "requested_size":
            return self.exact_kw
        raw = min(v for v in (self.consumption_kw, self.area_kw) if v is not None)
        return max((raw / HALF).quantize(WHOLE, rounding=ROUND_HALF_EVEN) * HALF, HALF)


def consumption_kw(monthly_units: Decimal, state: Optional[str]) -> Decimal:
    offset = estimate_config()["sizing"]["target_offset_percent"].value
    return monthly_units * offset / HUNDRED / generation_rate(state).value


def area_kw(area_sqft: Decimal) -> Decimal:
    return area_sqft / estimate_config()["roof_area"]["sqft_per_kwp"].value


def size(inp: EstimateInputs, cons: Consumption) -> Sizing:
    by_use = consumption_kw(cons.monthly_units, inp.state) if cons.monthly_units is not None else None
    by_area = area_kw(inp.usable_roof_area_sqft) if inp.usable_roof_area_sqft is not None else None
    if inp.requested_kw is not None:
        return Sizing(inp.requested_kw, by_use, by_area, False, "requested_size")
    candidates = [v for v in (by_use, by_area) if v is not None]
    if not candidates:
        raise ValueError("Provide the monthly units, the monthly bill or the usable roof area.")
    raw = min(candidates)
    exact = max(raw.quantize(KW_STEP, rounding=ROUND_HALF_UP), KW_STEP)
    roof_limited = by_use is not None and by_area is not None and by_area < by_use
    basis = "roof_area" if by_use is None or roof_limited else cons.basis
    return Sizing(exact, by_use, by_area, roof_limited, basis)


def whole_kw(exact_kw: Decimal) -> tuple[int, int]:
    """(ceil, floor) whole kW of the exact size, ceil at least 1."""
    return (max(1, int(exact_kw.to_integral_value(rounding=ROUND_CEILING))),
            int(exact_kw.to_integral_value(rounding=ROUND_FLOOR)))


# ── Money figures for one system size ───────────────────────────────────────────

def _export_credit(state: Optional[str]) -> dict:
    sav = estimate_config()["savings"]
    return sav["export_credit"].get(state_key(state), sav["export_credit_fallback"])


def monthly_saving(kw_generation: Decimal, cons: Consumption, state: Optional[str]) -> Decimal:
    saving = marginal_saving(cons.model, cons.monthly_units, kw_generation)
    credit = _export_credit(state)
    surplus = max(kw_generation - cons.monthly_units, ZERO)
    if credit["rule"] == "flat_inr_per_kwh" and credit["value"].status == "confirmed" and surplus > 0:
        saving += money(surplus * credit["value"].value)
    return saving


def coverage_percent(monthly_gen: Decimal, monthly_units: Optional[Decimal]) -> Optional[Decimal]:
    if not monthly_units:
        return None
    return (monthly_gen / monthly_units * HUNDRED).quantize(ONE_DP, rounding=ROUND_HALF_UP)


def confidence(cons: Consumption, state: Optional[str]) -> str:
    if cons.monthly_units is None:
        level = "low"
    elif cons.tariff_source == "discom_tariff":
        level = "high" if cons.basis in ("monthly_units", "monthly_units_and_bill") else "medium"
    elif cons.tariff_source == "customer_bill":
        level = "medium"
    else:
        level = "low"
    if generation_rate(state).status != "confirmed" and level == "high":
        level = "medium"
    return level


def savings_block(kw: Decimal, inp: EstimateInputs, cons: Consumption) -> Optional[dict]:
    if cons.monthly_units is None:
        return None
    gen = monthly_generation(kw, inp.state)
    monthly = monthly_saving(gen, cons, inp.state)
    before = bill(cons.model, cons.monthly_units)
    after = bill(cons.model, max(cons.monthly_units - gen, ZERO))
    return {"monthly": monthly, "annual": monthly * TWELVE, "method": "marginal",
            "tariff_source": cons.tariff_source, "confidence": confidence(cons, inp.state),
            "monthly_bill_before": before, "monthly_bill_after": after,
            "rate_inr_per_kwh": cons.model.rate, "tariff_table": cons.table_name if cons.tariff_source ==
            "discom_tariff" else None,
            "free_units_per_month": cons.model.free_units if cons.model.free_units else None}


def long_term_block(kw: Decimal, inp: EstimateInputs, cons: Consumption) -> dict:
    sav = estimate_config()["savings"]
    years = int(sav["years"].value)
    escalation = sav["escalation"].value
    annual_gen = monthly_generation(kw, inp.state) * TWELVE
    rows, total_gen, total_savings = [], ZERO, ZERO
    for year in range(1, years + 1):
        factor = degradation_factor(year)
        gen = (annual_gen * factor).quantize(WHOLE, rounding=ROUND_HALF_UP)
        saving = None
        if cons.monthly_units is not None:
            grow = (1 + escalation / HUNDRED) ** (year - 1)
            saving = money(monthly_saving(gen / TWELVE, cons, inp.state) * TWELVE * grow)
            total_savings += saving
        total_gen += gen
        rows.append({"year": year, "output_percent": (factor * HUNDRED).quantize(KW_STEP, rounding=ROUND_HALF_UP),
                     "generation_units": gen, "savings": saving})
    gcfg = estimate_config()["generation"]
    return {"years": years, "total_generation": total_gen,
            "total_savings": total_savings if cons.monthly_units is not None else None,
            "by_year": rows,
            "assumptions": {"degradation_first_year_percent": gcfg["first_year_percent"].value,
                            "degradation_annual_percent": gcfg["annual_percent"].value,
                            "tariff_escalation_percent_per_year": escalation,
                            "o_and_m": "First year free (price list); later years' cost is not modelled "
                                       "and not deducted."}}


def financials(kw: Decimal, inp: EstimateInputs, cons: Consumption, system_cost: Optional[Decimal]) -> dict:
    """Generation, coverage, subsidy, savings, payback and long-term figures for `kw`.
    Decimal values; `public()` turns them into JSON."""
    rate = generation_rate(inp.state)
    gen = monthly_generation(kw, inp.state)
    sub = subsidy_rules.calculate(kw=kw, state=inp.state, segment=inp.segment,
                                  is_housing_society=inp.is_housing_society, number_of_houses=inp.number_of_houses,
                                  connection_type=inp.connection_type, module_wattage_wp=inp.module_wattage_wp,
                                  on=inp.on or today_ist())
    sav = savings_block(kw, inp, cons)
    net = system_cost - sub.confirmed_total if system_cost is not None else None
    payback = None
    if net is not None and sav is not None and sav["annual"] > 0:
        payback = (max(net, ZERO) / sav["annual"]).quantize(ONE_DP, rounding=ROUND_HALF_UP)
    return {
        "kw": kw,
        "generation": {"units_per_kwp_per_month": rate.value, "monthly_units": gen, "annual_units": gen * TWELVE,
                       "status": rate.status, "source": rate.source},
        "coverage_percent": coverage_percent(gen, cons.monthly_units),
        "subsidy": sub,
        "savings": sav,
        "payback_years": payback,
        "net_cost": net,
        "net_cost_if_announced_support_received": net - sub.announced_total if net is not None else None,
        "announced_state_support": [t for t in sub.state if t["status"] == "announced"],
        "long_term": long_term_block(kw, inp, cons),
    }


# ── Warnings and assumptions ────────────────────────────────────────────────────

def size_warnings(inp: EstimateInputs, sizing: Sizing, priced_kw: Optional[int]) -> list:
    out = []
    load = inp.sanctioned_load_kw
    if load is not None and (sizing.exact_kw > load or (priced_kw is not None and priced_kw > load)):
        out.append(("above_sanctioned_load", {"sanctioned_load_kw": str(load), "exact_kw": str(sizing.exact_kw),
                                              "priced_kw": priced_kw}))
    if priced_kw is not None and sizing.area_kw is not None and Decimal(priced_kw) > sizing.area_kw:
        out.append(("priced_size_exceeds_roof_area", {"priced_kw": priced_kw,
                                                      "max_by_area_kw": str(sizing.area_kw.quantize(
                                                          KW_STEP, rounding=ROUND_HALF_UP)),
                                                      "sqft_per_kwp": str(
                                                          estimate_config()["roof_area"]["sqft_per_kwp"].value)}))
    rate = generation_rate(inp.state)
    if rate.status != "confirmed":
        out.append(("generation_unconfirmed_for_state", {"state": inp.state,
                                                         "units_per_kwp_per_month": str(rate.value)}))
    return out


def assumptions(inp: EstimateInputs, cons: Consumption, sizing: Optional[Sizing]) -> list[dict]:
    """Every value the estimate used: {name, value, unit, source, status, as_of}."""
    cfg = estimate_config()
    out = [generation_rate(inp.state).public("units_per_kwp_per_month")]
    if sizing is None or sizing.consumption_kw is not None:
        out.append(cfg["sizing"]["target_offset_percent"].public("target_offset_percent"))
    if inp.usable_roof_area_sqft is not None:
        out.append(cfg["roof_area"]["sqft_per_kwp"].public("sqft_per_kwp"))
    if cons.tariff_source == "flat_rate":
        out.append(cfg["tariffs"]["flat_rate"].public("flat_rate_inr_per_kwh"))
    elif cons.tariff_source == "customer_bill":
        out.append({"name": "implied_rate_inr_per_kwh", "value": str(cons.implied_rate), "unit": "INR/kWh",
                    "status": "customer_bill", "source": "The customer's bill ÷ units", "as_of": None})
    if cons.table_name:
        out.append({"name": "discom_tariff_table", "value": cons.table_name, "unit": "table",
                    "status": cons.table_status, "source": "config/solar_estimate.yaml tariffs.discoms",
                    "as_of": None})
    if cons.free_units:
        out.append(cons.free_units["value"].public("free_units_per_month"))
    sav = cfg["savings"]
    credit = _export_credit(inp.state)
    out += [cfg["generation"]["first_year_percent"].public("degradation_first_year_percent"),
            cfg["generation"]["annual_percent"].public("degradation_annual_percent"),
            sav["escalation"].public("tariff_escalation_percent_per_year"),
            sav["years"].public("long_term_years"),
            {**credit["value"].public("export_credit"), "value": credit["rule"] if credit["value"].value is None
             else str(credit["value"].value)},
            sav["o_and_m_first_year_free"].public("o_and_m_first_year_free")]
    cen = subsidy_config()["central"]
    out.append({"name": "central_cfa", "value": cen["scheme"], "unit": "scheme", "status": cen["status"],
                "source": cen["source"], "as_of": cen["as_of"].isoformat()})
    out.append(cen["cfa_capacity_basis"].public("cfa_capacity_basis"))
    return out


def warnings_public(items: list) -> tuple[list[str], dict]:
    """(codes in order, {code: params}) — the API shape."""
    codes, details = [], {}
    for code, params in items:
        if code not in details:
            codes.append(code)
            details[code] = params
    return codes, details


def public(obj):
    """Decimals → strings, dates → ISO, SubsidyResult → its public dict, recursively."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, subsidy_rules.SubsidyResult):
        return obj.public()
    if isinstance(obj, dict):
        return {k: public(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [public(v) for v in obj]
    return obj


def estimate_version() -> str:
    return version()
