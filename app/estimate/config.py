"""config/solar_estimate.yaml and config/subsidies.yaml — loaded once, validated at
startup (app.main), and parsed into Decimal values: no binary float survives the loader
(a YAML number with decimals is refused; write it as text, "0.55").

Every figure is a `Value` (value, unit, status, source, as_of). `version()` hashes both
files, so a saved estimate records exactly which assumptions produced it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
ESTIMATE_PATH = _CONFIG_DIR / "solar_estimate.yaml"
SUBSIDIES_PATH = _CONFIG_DIR / "subsidies.yaml"

STATUSES = ("confirmed", "announced", "unconfirmed")
EXPORT_RULES = ("none", "flat_inr_per_kwh")
FIXED_CHARGE_TYPES = ("flat_per_month", "per_kw_per_month")
DUTY_TYPES = ("percent_of_energy", "percent_of_energy_and_fixed")
CAPACITY_BASES = ("nominal", "module_dc")
AMOUNT_RULES = ("per_kw_with_cap", "capacity_steps")
APPLIES_TO = ("residential_individual",)
CONNECTION_TYPES = ("on_grid", "off_grid", "hybrid")


class EstimateConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Value:
    """One sourced figure. `value` is a Decimal, str, bool or None (not available)."""
    value: Any
    unit: str
    status: str
    source: str
    as_of: date

    def public(self, name: str) -> dict:
        v = self.value
        return {"name": name, "value": str(v) if isinstance(v, Decimal) else v, "unit": self.unit,
                "status": self.status, "source": self.source, "as_of": self.as_of.isoformat()}


@dataclass(frozen=True)
class Slab:
    up_to_units: Optional[Decimal]   # None = no upper limit (the last slab)
    rate: Decimal


@dataclass(frozen=True)
class Charge:
    type: str
    value: Decimal


@dataclass(frozen=True)
class TariffTable:
    discom: str
    category: str
    slabs: Optional[tuple[Slab, ...]]
    fixed_charge: Optional[Charge]
    electricity_duty: Optional[Charge]
    effective_from: Optional[date]
    status: str
    source: str
    as_of: date

    @property
    def usable(self) -> bool:
        """Only a confirmed table with slabs computes a bill."""
        return self.status == "confirmed" and self.slabs is not None


def state_key(name: Optional[str]) -> str:
    """'Jammu & Kashmir' → 'jammu_and_kashmir'; the key every state map uses."""
    text = (name or "").strip().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


class _Checker:
    def __init__(self, path: Path):
        self.name = path.name

    def need(self, cond, message: str) -> None:
        if not cond:
            raise EstimateConfigError(f"{self.name}: {message}")

    def dec(self, raw, where: str, *, minimum: Optional[Decimal] = None, positive: bool = False,
            maximum: Optional[Decimal] = None) -> Decimal:
        self.need(not isinstance(raw, (bool, float)) and isinstance(raw, (int, str)),
                  f"{where} must be a whole number or a decimal written as text (got {raw!r})")
        try:
            out = Decimal(str(raw).strip())
        except InvalidOperation:
            raise EstimateConfigError(f"{self.name}: {where} is not a number ({raw!r})")
        self.need(out.is_finite(), f"{where} must be finite")
        if positive:
            self.need(out > 0, f"{where} must be > 0")
        if minimum is not None:
            self.need(out >= minimum, f"{where} must be >= {minimum}")
        if maximum is not None:
            self.need(out <= maximum, f"{where} must be <= {maximum}")
        return out

    def meta(self, raw: dict, where: str) -> tuple[str, str, date]:
        self.need(isinstance(raw, dict), f"{where} must be a mapping")
        self.need(raw.get("status") in STATUSES, f"{where}.status must be one of {STATUSES}")
        self.need(isinstance(raw.get("source"), str) and raw["source"].strip(), f"{where}.source is required")
        self.need(isinstance(raw.get("as_of"), date), f"{where}.as_of must be a date (YYYY-MM-DD)")
        return raw["status"], raw["source"].strip(), raw["as_of"]

    def value(self, raw: dict, where: str, **bounds) -> Value:
        status, source, as_of = self.meta(raw, where)
        self.need(isinstance(raw.get("unit"), str) and raw["unit"], f"{where}.unit is required")
        return Value(self.dec(raw.get("value"), f"{where}.value", **bounds), raw["unit"], status, source, as_of)

    def opt_date(self, raw, where: str) -> Optional[date]:
        self.need(raw is None or isinstance(raw, date), f"{where} must be a date or null")
        return raw


def parse_tariff_table(discom: str, category: str, raw: dict, path: Path = ESTIMATE_PATH) -> TariffTable:
    """One DISCOM category's table. Public so tests can build a synthetic table — real
    slab values live only in config (and only from an order or a real bill)."""
    c = _Checker(path)
    where = f"tariffs.discoms.{discom}.categories.{category}"
    status, source, as_of = c.meta(raw, where)
    slabs = None
    if raw.get("slabs") is not None:
        c.need(isinstance(raw["slabs"], list) and raw["slabs"], f"{where}.slabs must be a list or null")
        parsed, previous = [], Decimal(0)
        for i, s in enumerate(raw["slabs"]):
            last = i == len(raw["slabs"]) - 1
            c.need(isinstance(s, dict) and set(s) == {"up_to_units", "rate"},
                   f"{where}.slabs[{i}] needs exactly up_to_units and rate")
            rate = c.dec(s["rate"], f"{where}.slabs[{i}].rate", minimum=Decimal(0))
            if s["up_to_units"] is None:
                c.need(last, f"{where}.slabs: only the last slab may be open (up_to_units: null)")
                parsed.append(Slab(None, rate))
                continue
            up_to = c.dec(s["up_to_units"], f"{where}.slabs[{i}].up_to_units", positive=True)
            c.need(up_to > previous, f"{where}.slabs must be in ascending up_to_units order")
            previous = up_to
            parsed.append(Slab(up_to, rate))
        c.need(parsed[-1].up_to_units is None, f"{where}.slabs: the last slab must be open (up_to_units: null)")
        slabs = tuple(parsed)
    charges = {}
    for key, types in (("fixed_charge", FIXED_CHARGE_TYPES), ("electricity_duty", DUTY_TYPES)):
        item = raw.get(key)
        if item is None:
            charges[key] = None
            continue
        c.need(isinstance(item, dict) and item.get("type") in types, f"{where}.{key}.type must be one of {types}")
        charges[key] = Charge(item["type"], c.dec(item.get("value"), f"{where}.{key}.value", minimum=Decimal(0)))
    effective = c.opt_date(raw.get("effective_from"), f"{where}.effective_from")
    if slabs is None:
        c.need(status == "unconfirmed", f"{where}: a table without slabs must be status unconfirmed")
    if status == "confirmed":
        c.need(effective is not None, f"{where}: a confirmed table needs effective_from")
    return TariffTable(discom, category, slabs, charges["fixed_charge"], charges["electricity_duty"], effective,
                       status, source, as_of)


def _validate_estimate(data: dict) -> dict:
    c = _Checker(ESTIMATE_PATH)
    c.need(isinstance(data.get("version"), int) and data["version"] >= 1, "version must be a whole number >= 1")
    gen = data.get("generation") or {}
    per = gen.get("units_per_kwp_per_month") or {}
    c.need(isinstance(per.get("states"), dict) and per["states"], "generation.units_per_kwp_per_month.states must map states")
    states = {}
    for key, raw in per["states"].items():
        c.need(key == state_key(key), f"generation state key {key!r} must be snake_case")
        states[key] = c.value(raw, f"generation.units_per_kwp_per_month.states.{key}", positive=True,
                              maximum=Decimal(1000))
    fallback = c.value(per.get("fallback") or {}, "generation.units_per_kwp_per_month.fallback", positive=True,
                       maximum=Decimal(1000))
    for key, v in [*states.items(), ("fallback", fallback)]:
        # Proposals print it ("~120 units / kWp / month") as a whole number.
        c.need(v.value == v.value.to_integral_value(), f"generation units_per_kwp_per_month.{key} must be whole")
    deg = gen.get("degradation") or {}
    first = c.value(deg.get("first_year_percent") or {}, "generation.degradation.first_year_percent",
                    minimum=Decimal(0), maximum=Decimal(50))
    annual = c.value(deg.get("annual_percent") or {}, "generation.degradation.annual_percent",
                     minimum=Decimal(0), maximum=Decimal(10))

    roof = data.get("roof_area") or {}
    sqft = c.value(roof.get("sqft_per_kwp") or {}, "roof_area.sqft_per_kwp", positive=True, maximum=Decimal(1000))
    sqft_low = c.value(roof.get("sqft_per_kwp_low") or {}, "roof_area.sqft_per_kwp_low", positive=True)
    c.need(sqft_low.value <= sqft.value, "roof_area.sqft_per_kwp_low must be <= sqft_per_kwp")

    sizing = data.get("sizing") or {}
    offset = c.value(sizing.get("target_offset_percent") or {}, "sizing.target_offset_percent", positive=True,
                     maximum=Decimal(200))

    sav = data.get("savings") or {}
    escalation = c.value(sav.get("tariff_escalation_percent_per_year") or {},
                         "savings.tariff_escalation_percent_per_year", minimum=Decimal(0), maximum=Decimal(20))
    years = c.value(sav.get("long_term_years") or {}, "savings.long_term_years", positive=True, maximum=Decimal(40))
    c.need(years.value == years.value.to_integral_value(), "savings.long_term_years must be whole years")
    c.need(1 - first.value / 100 - (years.value - 2) * annual.value / 100 > 0,
           "degradation must leave positive output in the last long-term year")
    export = sav.get("export_credit") or {}
    c.need(isinstance(export.get("states"), dict), "savings.export_credit.states must map states")

    def credit(raw, where):
        status, source, as_of = c.meta(raw, where)
        c.need(raw.get("rule") in EXPORT_RULES, f"{where}.rule must be one of {EXPORT_RULES}")
        if raw["rule"] == "none":
            c.need(raw.get("value") is None, f"{where}.value must be null for rule none")
            amount = None
        else:
            amount = c.dec(raw.get("value"), f"{where}.value", positive=True)
        return {"rule": raw["rule"], "value": Value(amount, raw.get("unit") or "INR/kWh", status, source, as_of)}

    credits = {k: credit(v, f"savings.export_credit.states.{k}") for k, v in export["states"].items()}
    for k in credits:
        c.need(k == state_key(k), f"export credit state key {k!r} must be snake_case")
    credit_fallback = credit(export.get("fallback") or {}, "savings.export_credit.fallback")
    om = sav.get("o_and_m_first_year_free") or {}
    om_status, om_source, om_as_of = c.meta(om, "savings.o_and_m_first_year_free")
    c.need(isinstance(om.get("value"), bool), "savings.o_and_m_first_year_free.value must be true/false")

    tar = data.get("tariffs") or {}
    flat = c.value(tar.get("flat_rate_inr_per_kwh") or {}, "tariffs.flat_rate_inr_per_kwh", positive=True,
                   maximum=Decimal(100))
    sane = tar.get("implied_rate_sanity_inr_per_kwh") or {}
    sane_status, sane_source, sane_as_of = c.meta(sane, "tariffs.implied_rate_sanity_inr_per_kwh")
    lo = c.dec(sane.get("min"), "tariffs.implied_rate_sanity_inr_per_kwh.min", positive=True)
    hi = c.dec(sane.get("max"), "tariffs.implied_rate_sanity_inr_per_kwh.max", positive=True)
    c.need(lo < hi, "tariffs.implied_rate_sanity_inr_per_kwh needs min < max")
    discoms = {}
    c.need(isinstance(tar.get("discoms"), dict) and tar["discoms"], "tariffs.discoms must map DISCOMs")
    for name, d in tar["discoms"].items():
        c.need(isinstance(d, dict) and isinstance(d.get("state"), str) and d["state"].strip(),
               f"tariffs.discoms.{name}.state is required")
        c.need(isinstance(d.get("categories"), dict) and d["categories"], f"tariffs.discoms.{name}.categories required")
        discoms[name.upper()] = {"state": d["state"].strip(),
                                 "categories": {cat: parse_tariff_table(name.upper(), cat, raw)
                                                for cat, raw in d["categories"].items()}}
    free = {}
    for key, raw in (tar.get("free_units") or {}).items():
        where = f"tariffs.free_units.{key}"
        v = c.value(raw, where, positive=True)
        c.need(isinstance(raw.get("state"), str) and state_key(raw["state"]) == key,
               f"{where}.state must name the state its key is for")
        names = raw.get("discoms") or []
        c.need(isinstance(names, list) and all(n in discoms for n in names), f"{where}.discoms must name known DISCOMs")
        free[key] = {"value": v, "state": raw["state"], "discoms": tuple(names)}
    return {
        "version": data["version"],
        "generation": {"states": states, "fallback": fallback, "first_year_percent": first,
                       "annual_percent": annual},
        "roof_area": {"sqft_per_kwp": sqft, "sqft_per_kwp_low": sqft_low},
        "sizing": {"target_offset_percent": offset},
        "savings": {"escalation": escalation, "years": years, "export_credit": credits,
                    "export_credit_fallback": credit_fallback,
                    "o_and_m_first_year_free": Value(om["value"], "flag", om_status, om_source, om_as_of)},
        "tariffs": {"flat_rate": flat,
                    "sanity": {"min": lo, "max": hi, "status": sane_status, "source": sane_source,
                               "as_of": sane_as_of},
                    "discoms": discoms, "free_units": free},
    }


def _slab_max(c: _Checker, raw: dict, where: str) -> tuple[tuple[tuple[Decimal, Decimal], ...], Decimal]:
    slabs = raw.get("slabs") or []
    c.need(isinstance(slabs, list) and slabs, f"{where}.slabs must list slabs")
    out, previous, total = [], Decimal(0), Decimal(0)
    for i, s in enumerate(slabs):
        up_to = c.dec(s.get("up_to_kwp"), f"{where}.slabs[{i}].up_to_kwp", positive=True)
        rate = c.dec(s.get("rate_per_kwp"), f"{where}.slabs[{i}].rate_per_kwp", positive=True)
        c.need(up_to > previous, f"{where}.slabs must be ascending")
        total += (up_to - previous) * rate
        previous = up_to
        out.append((up_to, rate))
    max_inr = c.dec(raw.get("max_inr"), f"{where}.max_inr", positive=True)
    c.need(max_inr == total, f"{where}.max_inr ({max_inr}) must equal the slabs' total ({total})")
    return tuple(out), max_inr


def _validate_subsidies(data: dict) -> dict:
    c = _Checker(SUBSIDIES_PATH)
    c.need(isinstance(data.get("version"), int) and data["version"] >= 1, "version must be a whole number >= 1")
    cen = data.get("central") or {}
    status, source, as_of = c.meta(cen, "central")
    c.need(isinstance(cen.get("scheme"), str) and cen["scheme"], "central.scheme is required")
    res = cen.get("residential") or {}
    residential = {}
    for category in ("general", "special"):
        slabs, max_inr = _slab_max(c, res.get(category) or {}, f"central.residential.{category}")
        residential[category] = {"slabs": slabs, "max_inr": max_inr}
    soc = cen.get("housing_society") or {}
    rates = soc.get("rate_per_kwp") or {}
    society = {"rate_per_kwp": {k: c.dec(rates.get(k), f"central.housing_society.rate_per_kwp.{k}", positive=True)
                                for k in ("general", "special")},
               "kwp_per_house": c.dec(soc.get("kwp_per_house"), "central.housing_society.kwp_per_house", positive=True),
               "max_kwp": c.dec(soc.get("max_kwp"), "central.housing_society.max_kwp", positive=True)}
    special = cen.get("special_category_states") or []
    c.need(isinstance(special, list) and special and all(isinstance(s, str) and s.strip() for s in special),
           "central.special_category_states must list states")
    segments = cen.get("eligible_segments") or []
    c.need(isinstance(segments, list) and segments, "central.eligible_segments must list segments")
    connections = cen.get("eligible_connection_types") or []
    c.need(isinstance(connections, list) and connections and set(connections) <= set(CONNECTION_TYPES),
           f"central.eligible_connection_types must be among {CONNECTION_TYPES}")
    basis_raw = cen.get("cfa_capacity_basis") or {}
    b_status, b_source, b_as_of = c.meta(basis_raw, "central.cfa_capacity_basis")
    c.need(basis_raw.get("value") in CAPACITY_BASES, f"central.cfa_capacity_basis.value must be one of {CAPACITY_BASES}")
    conditions = cen.get("conditions") or []
    c.need(isinstance(conditions, list) and conditions and all(isinstance(x, str) for x in conditions),
           "central.conditions must list condition codes")

    tops = {}
    for key, items in (data.get("state_top_ups") or {}).items():
        c.need(key == state_key(key), f"state_top_ups key {key!r} must be snake_case")
        c.need(isinstance(items, list) and items, f"state_top_ups.{key} must list top-ups")
        parsed = []
        for i, item in enumerate(items):
            where = f"state_top_ups.{key}[{i}]"
            t_status, t_source, t_as_of = c.meta(item, where)
            c.need(isinstance(item.get("state"), str) and state_key(item["state"]) == key,
                   f"{where}.state must name the state its key is for")
            rule = item.get("amount_rule") or {}
            c.need(rule.get("type") in AMOUNT_RULES, f"{where}.amount_rule.type must be one of {AMOUNT_RULES}")
            if rule["type"] == "per_kw_with_cap":
                amount_rule = {"type": rule["type"],
                               "rate_per_kw": c.dec(rule.get("rate_per_kw"), f"{where}.amount_rule.rate_per_kw",
                                                    positive=True),
                               "cap": c.dec(rule.get("cap"), f"{where}.amount_rule.cap", positive=True)}
            else:
                steps, previous = [], Decimal(0)
                for j, s in enumerate(rule.get("steps") or []):
                    min_kw = c.dec(s.get("min_kw"), f"{where}.amount_rule.steps[{j}].min_kw", positive=True)
                    c.need(min_kw > previous, f"{where}.amount_rule.steps must be ascending")
                    previous = min_kw
                    steps.append((min_kw, c.dec(s.get("amount"), f"{where}.amount_rule.steps[{j}].amount",
                                                positive=True)))
                c.need(steps, f"{where}.amount_rule.steps must list steps")
                amount_rule = {"type": rule["type"], "steps": tuple(steps)}
            c.need(item.get("applies_to") in APPLIES_TO, f"{where}.applies_to must be one of {APPLIES_TO}")
            c.need(isinstance(item.get("eligibility_note"), str) and item["eligibility_note"].strip(),
                   f"{where}.eligibility_note is required")
            valid_from = c.opt_date(item.get("valid_from"), f"{where}.valid_from")
            c.need(valid_from is not None, f"{where}.valid_from is required")
            valid_until = c.opt_date(item.get("valid_until"), f"{where}.valid_until")
            c.need(valid_until is None or valid_until >= valid_from, f"{where}.valid_until must be >= valid_from")
            parsed.append({"state": item["state"], "amount_rule": amount_rule, "applies_to": item["applies_to"],
                           "eligibility_note": item["eligibility_note"].strip(), "status": t_status,
                           "source": t_source, "as_of": t_as_of, "valid_from": valid_from,
                           "valid_until": valid_until})
        tops[key] = tuple(parsed)
    prop = data.get("proposal") or {}
    c.need(isinstance(prop.get("print_confirmed_state_top_up"), bool),
           "proposal.print_confirmed_state_top_up must be true/false")
    return {
        "version": data["version"],
        "central": {"scheme": cen["scheme"], "status": status, "source": source, "as_of": as_of,
                    "residential": residential, "housing_society": society,
                    "special_category_states": tuple(s.strip() for s in special),
                    "special_keys": frozenset(state_key(s) for s in special),
                    "eligible_segments": tuple(s.strip().lower() for s in segments),
                    "eligible_connection_types": tuple(connections),
                    "cfa_capacity_basis": Value(basis_raw["value"], "basis", b_status, b_source, b_as_of),
                    "conditions": tuple(conditions)},
        "state_top_ups": tops,
        "proposal": {"print_confirmed_state_top_up": prop["print_confirmed_state_top_up"]},
    }


def _read(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    return raw, yaml.safe_load(raw.decode("utf-8")) or {}


@lru_cache(maxsize=1)
def estimate_config() -> dict:
    return _validate_estimate(_read(ESTIMATE_PATH)[1])


@lru_cache(maxsize=1)
def subsidy_config() -> dict:
    return _validate_subsidies(_read(SUBSIDIES_PATH)[1])


@lru_cache(maxsize=1)
def version() -> str:
    """'<estimate version>.<subsidies version>+<sha256 of both files, 12 hex>'."""
    digest = hashlib.sha256()
    for path in (ESTIMATE_PATH, SUBSIDIES_PATH):
        digest.update(_read(path)[0])
    return f"{estimate_config()['version']}.{subsidy_config()['version']}+{digest.hexdigest()[:12]}"


def load() -> None:
    """Startup check: both files parse and validate."""
    estimate_config()
    subsidy_config()
    version()


def clear_caches() -> None:
    for fn in (estimate_config, subsidy_config, version):
        fn.cache_clear()
