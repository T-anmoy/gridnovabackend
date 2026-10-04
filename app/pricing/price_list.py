"""Grid Nova's price list (config/price_list.yaml) — the ONLY source of customer-facing
system prices.

The YAML holds source values only, verbatim from Grid Nova's Pricing & Equipment Master
Data PDF (its sha256 is recorded). Everything derived is computed here, with the
source's own rules, and checked against tests/fixtures/price_list_golden_v1.csv:

    Nova Assured   basic  = the supplied figure for (phase, kW)
    Nova Signature basic  = Assured basic × (1 + 0.12)        — never from MRP; before GST
    GST                   = basic × 0.089                       (5% on 70% + 18% on 30%)
    Final                 = basic + GST
    every amount rounded ROUND_HALF_UP to the paisa

Prices exist only for the sizes in the table: single phase 1–10 kW, three phase 3–10 kW,
whole kW, destination West Bengal. Nothing here interpolates, extrapolates or works per
kW; anything else is Unsupported(reason).

The loader fails loudly (PriceListError, at boot) on any deviation: a missing or extra
size, an extra key, a supplied MRP that is not the computed Assured final, or rules that
differ from the ones implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "price_list.yaml"

PACKAGES = ("NOVA_ASSURED", "NOVA_SIGNATURE")
PHASES = ("SINGLE", "THREE")
PAISA = Decimal("0.01")
# What the rules block must say — the code implements exactly these.
_EXPECTED_GST = [{"share_of_basic": "0.70", "rate": "0.05"}, {"share_of_basic": "0.30", "rate": "0.18"}]
_EXPECTED_EFFECTIVE = Decimal("0.089")
_EXPECTED_UPLIFT = Decimal("0.12")
_TOP_KEYS = {"version", "source_document", "source_sha256", "effective_from", "currency",
             "currency_label_in_documents", "destination", "price_change_clause", "packages", "rules",
             "phase_ranges", "assured_basic_inr", "equipment", "electrical_spec_up_to_5kw", "commercial_terms",
             "source_inconsistencies_to_confirm"}
UNSUPPORTED_REASONS = ("three_phase_below_min", "size_above_max", "size_below_min", "non_integer_size",
                       "unknown_package", "unknown_phase")
# Package for each onboarding equipment tier (ONB-3, resolved by the source document).
PACKAGE_FOR_TIER = {"standard": "NOVA_ASSURED", "premium": "NOVA_SIGNATURE"}
PHASE_FOR_CONNECTION = {"single_phase": "SINGLE", "three_phase": "THREE"}


class PriceListError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceQuote:
    package: str
    phase: str
    kw: int
    basic: Decimal
    gst: Decimal
    final: Decimal
    price_list_version: int
    source_status: str            # SUPPLIED (Assured, in the PDF) | DERIVED (Signature, computed)

    def amounts(self) -> dict:
        return {"basic": str(self.basic), "gst": str(self.gst), "final": str(self.final)}


@dataclass(frozen=True)
class Unsupported:
    reason: str
    detail: str


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_UP)


def gst_of(basic: Decimal) -> Decimal:
    return _money(Decimal(basic) * _EXPECTED_EFFECTIVE)


def signature_basic(assured_basic: Decimal) -> Decimal:
    return _money(Decimal(assured_basic) * (1 + _EXPECTED_UPLIFT))


def _validate(data: dict) -> dict:
    def need(cond, message: str) -> None:
        if not cond:
            raise PriceListError(f"{_PATH.name}: {message}")

    need(isinstance(data, dict), "must be a mapping")
    need(set(data) == _TOP_KEYS, f"top-level keys must be exactly {sorted(_TOP_KEYS)} "
                                 f"(extra: {sorted(set(data) - _TOP_KEYS)}, missing: {sorted(_TOP_KEYS - set(data))})")
    need(isinstance(data["version"], int) and data["version"] >= 1, "version must be a whole number")
    need(isinstance(data["source_sha256"], str) and len(data["source_sha256"]) == 64, "source_sha256 must be a sha256")
    need(set(data["packages"]) == set(PACKAGES), f"packages must be exactly {PACKAGES}")
    tiers = {p: data["packages"][p].get("equipment_tier") for p in PACKAGES}
    need(tiers == {"NOVA_ASSURED": "standard", "NOVA_SIGNATURE": "premium"},
         "NOVA_ASSURED must be equipment_tier standard and NOVA_SIGNATURE premium")
    rules = data["rules"]
    gst = rules.get("gst") or {}
    need(gst.get("components") == _EXPECTED_GST, "rules.gst.components must be 70% at 5% and 30% at 18%")
    need(Decimal(str(gst.get("effective_rate"))) == _EXPECTED_EFFECTIVE
         == sum(Decimal(c["share_of_basic"]) * Decimal(c["rate"]) for c in gst["components"]),
         "rules.gst.effective_rate must be 0.089 (= 0.70 × 0.05 + 0.30 × 0.18)")
    need(Decimal(str((rules.get("signature") or {}).get("uplift_rate"))) == _EXPECTED_UPLIFT,
         "rules.signature.uplift_rate must be 0.12")
    ranges = data["phase_ranges"]
    need(set(ranges) == set(PHASES), f"phase_ranges must be exactly {PHASES}")
    for phase, r in ranges.items():
        need(set(r) == {"min_kw", "max_kw"} and all(isinstance(v, int) for v in r.values())
             and 1 <= r["min_kw"] <= r["max_kw"], f"phase_ranges.{phase} needs whole min_kw <= max_kw")
    table = data["assured_basic_inr"]
    need(set(table) == set(PHASES), f"assured_basic_inr must have exactly {PHASES}")
    for phase in PHASES:
        expected = set(range(ranges[phase]["min_kw"], ranges[phase]["max_kw"] + 1))
        sizes = table[phase] or {}
        need(all(isinstance(k, int) for k in sizes), f"assured_basic_inr.{phase} sizes must be whole kW")
        need(set(sizes) == expected, f"assured_basic_inr.{phase} must list exactly {sorted(expected)} kW "
                                     f"(extra: {sorted(set(sizes) - expected)}, missing: {sorted(expected - set(sizes))})")
        for kw, row in sizes.items():
            need(set(row) == {"basic", "mrp_reference"}, f"assured_basic_inr.{phase}.{kw} keys must be basic, mrp_reference")
            basic = Decimal(str(row["basic"]))
            need(basic > 0 and basic == _money(basic), f"assured_basic_inr.{phase}.{kw}.basic must be a positive amount")
            need(Decimal(str(row["mrp_reference"])) == basic + gst_of(basic),
                 f"assured_basic_inr.{phase}.{kw}: supplied MRP {row['mrp_reference']} is not the computed "
                 f"Assured final {basic + gst_of(basic)}")
    need(set(data["equipment"]) == set(PACKAGES) | {"dcr_status"}, "equipment must describe each package (+ dcr_status)")
    schedule = (data["commercial_terms"] or {}).get("payment_schedule") or []
    need([m.get("number") for m in schedule] == list(range(1, len(schedule) + 1))
         and sum(Decimal(str(m["percent"])) for m in schedule) == 100,
         "commercial_terms.payment_schedule must be numbered 1.. and sum to 100")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def version() -> int:
    return load()["version"]


def source_sha256() -> str:
    return load()["source_sha256"]


def phase_range(phase: str) -> dict:
    return dict(load()["phase_ranges"][phase])


def _whole_kw(kw) -> Optional[int]:
    try:
        value = Decimal(str(kw))
    except Exception:
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


def quote(package: str, phase: str, kw) -> Union[PriceQuote, Unsupported]:
    data = load()
    if package not in PACKAGES:
        return Unsupported("unknown_package", f"package must be one of {list(PACKAGES)}")
    if phase not in PHASES:
        return Unsupported("unknown_phase", f"phase must be one of {list(PHASES)}")
    size = _whole_kw(kw)
    if size is None:
        return Unsupported("non_integer_size", "the price list has whole-kW sizes only")
    r = data["phase_ranges"][phase]
    if size > r["max_kw"]:
        return Unsupported("size_above_max", f"the largest {phase.lower()}-phase system in the price list is "
                                             f"{r['max_kw']} kW")
    if size < r["min_kw"]:
        if phase == "THREE":
            return Unsupported("three_phase_below_min", f"three-phase systems start at {r['min_kw']} kW")
        return Unsupported("size_below_min", f"the smallest system in the price list is {r['min_kw']} kW")
    assured = Decimal(str(data["assured_basic_inr"][phase][size]["basic"]))
    basic = assured if package == "NOVA_ASSURED" else signature_basic(assured)
    gst = gst_of(basic)
    return PriceQuote(package=package, phase=phase, kw=size, basic=basic, gst=gst, final=basic + gst,
                      price_list_version=data["version"],
                      source_status="SUPPLIED" if package == "NOVA_ASSURED" else "DERIVED")


def gst_rule() -> dict:
    gst = load()["rules"]["gst"]
    return {"effective_rate": str(gst["effective_rate"]),
            "components": [{"share_of_basic": c["share_of_basic"], "rate": c["rate"]} for c in gst["components"]],
            "description": "GST at 8.9% effective: 5% on 70% of the basic price and 18% on the remaining 30%."}


def display_name(package: str) -> str:
    return load()["packages"][package]["display_name"]


def equipment(package: str) -> dict:
    return load()["equipment"][package]


def destination() -> str:
    return load()["destination"]


def commercial_terms() -> dict:
    return load()["commercial_terms"]


# ── Amounts in generated documents ──────────────────────────────────────────────

def indian_grouping(amount) -> str:
    """12,34,567.89 — Indian digit grouping, two decimals."""
    import re
    value = _money(amount)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        whole = f"{head},{tail}"
    return f"{sign}{whole}.{frac}"


def document_amount(amount) -> str:
    """How generated documents (proposal PDF, agreement, receipts) print money: the
    source's `currency_label_in_documents` ("Rs") + Indian grouping — "Rs 1,23,456.78".
    The website keeps ₹."""
    return f"{load()['currency_label_in_documents']} {indian_grouping(amount)}"
