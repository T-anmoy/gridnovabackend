"""config/studio.yaml — the Proposal Studio's project types, workflow rules, reminders,
proof-check thresholds, savings assumptions and itemised GST categories (BE-18).

Loaded once and validated at startup (app/main.py lifespan): an unknown layout or coverage,
payment terms that do not total 100, a decimal written as a YAML float, an itemised GST rate
outside 0–28 % or a category claimed by no registry entry stops the boot rather than produce a
wrong customer document.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "studio.yaml"

PROJECT_TYPES = ("residential_rooftop", "housing_society", "commercial_rooftop", "industrial_rooftop",
                 "ground_mounted", "government_institutional", "pm_kusum", "street_lighting", "generic")
LAYOUT_RULES = ("compact_or_detailed", "detailed", "itemised")
# Module technology (the price list's options) → its key under savings.degradation.
TECHNOLOGIES = {"Mono PERC": "mono_perc", "TOPCon": "topcon"}


class StudioConfigError(RuntimeError):
    pass


def _dec(value, where: str) -> Decimal:
    if isinstance(value, float):
        raise StudioConfigError(f"{_PATH.name}: {where} must be quoted text, not a YAML float")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise StudioConfigError(f"{_PATH.name}: {where} is not a number") from None


PRESET_SOURCES = ("price_list", "detailed")


def payment_term_presets() -> list[dict]:
    """[{id, label, terms: [{percent, label}]}] — the figures read from each preset's source."""
    from app.pricing import price_list
    from app.proposals import document_config
    out = []
    for x in load()["payment_term_presets"]:
        if x["source"] == "price_list":
            terms = [{"percent": str(m["percent"]), "label": m["label"]}
                     for m in price_list.commercial_terms()["payment_schedule"]]
        else:
            terms = [{"percent": str(t["percent"]), "label": t["label"]}
                     for t in document_config.load()["detailed"]["payment_terms"]]
        out.append({"id": x["id"], "label": x["label"], "terms": terms})
    return out


def _validate(data: dict) -> dict:
    def need(cond, message: str) -> None:
        if not cond:
            raise StudioConfigError(f"{_PATH.name}: {message}")

    need(isinstance(data, dict) and data.get("schema_version") == 1, "schema_version must be 1")
    types = data.get("project_types") or {}
    need(tuple(types) == PROJECT_TYPES, f"project_types must be exactly {PROJECT_TYPES}, in that order")
    from app.proposals import document_config
    uncovered = document_config.load()["uncovered_segments"]
    for name, spec in types.items():
        need(spec.get("layout") in LAYOUT_RULES, f"project_types.{name}.layout must be one of {LAYOUT_RULES}")
        need(spec.get("coverage") in (None, *uncovered), f"project_types.{name}.coverage must be null or one of "
                                                         f"{sorted(uncovered)}")
        need(spec.get("mount") in (None, "rooftop", "ground_mounted"), f"project_types.{name}.mount")
        need(isinstance(spec.get("label"), str) and spec["label"].strip(), f"project_types.{name} needs a label")
    for key, terms in (data.get("payment_terms") or {}).items():
        need(sum(_dec(t["percent"], f"payment_terms.{key}") for t in terms) == 100,
             f"payment_terms.{key} must total 100")
    need(set(data.get("payment_terms") or {}) == {"itemised"}, "payment_terms must define itemised (only)")
    presets = data.get("payment_term_presets") or []
    need(isinstance(presets, list) and presets, "payment_term_presets must list at least one preset")
    need(len({x.get("id") for x in presets}) == len(presets), "payment_term_presets ids must be distinct")
    for x in presets:
        need(isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("label"), str)
             and x.get("source") in PRESET_SOURCES,
             f"payment_term_presets: each needs an id, a label and a source in {PRESET_SOURCES}")
    rem = data.get("reminders") or {}
    for key in ("not_viewed_after_days", "expiring_within_days", "summary_expiring_within_days"):
        need(isinstance(rem.get(key), int) and rem[key] > 0, f"reminders.{key} must be a positive integer")
    need(isinstance(rem.get("customer_expiry_reminder"), bool), "reminders.customer_expiry_reminder is true/false")
    need(isinstance(data["workflow"].get("validity_days"), int) and data["workflow"]["validity_days"] > 0,
         "workflow.validity_days must be a positive integer")
    _dec(data["warn_single_phase_above_kw"], "warn_single_phase_above_kw")
    bands = data.get("price_per_kwp_band") or {}
    for name, band in bands.items():
        need(name in types, f"price_per_kwp_band.{name}: unknown project type")
        if band is not None:
            need(_dec(band["low"], f"price_per_kwp_band.{name}.low") < _dec(band["high"], f"…{name}.high"),
                 f"price_per_kwp_band.{name}: low must be below high")
    ranges = data.get("page_ranges") or {}
    need(set(ranges) == {"compact", "detailed", "itemised"}, "page_ranges must cover compact, detailed, itemised")
    for name, (low, high) in ranges.items():
        need(0 < low <= high, f"page_ranges.{name} must be [low, high]")
    sav = data.get("savings") or {}
    need(set(sav.get("degradation") or {}) == set(TECHNOLOGIES.values()),
         f"savings.degradation must cover {sorted(TECHNOLOGIES.values())}")
    for tech, spec in sav["degradation"].items():
        for key in ("first_year", "annual_after"):
            need(Decimal(0) <= _dec(spec[key], f"savings.degradation.{tech}.{key}") < Decimal("0.1"),
                 f"savings.degradation.{tech}.{key} must be a fraction below 0.1")
    need(_dec(sav["tariff_escalation"], "savings.tariff_escalation") >= 0, "savings.tariff_escalation must be ≥ 0")
    need(_dec(sav["export_rate_default"], "savings.export_rate_default") >= 0, "savings.export_rate_default ≥ 0")
    need(isinstance(sav.get("note"), str) and sav["note"].strip(), "savings.note is required")
    need(isinstance(sav.get("depreciation_line_enabled"), bool), "savings.depreciation_line_enabled is true/false")
    for word in ("tax", "depreciation"):
        need(word not in sav["note"].lower(), "savings.note may never mention tax or depreciation")
    item = data.get("itemised") or {}
    for cat, spec in (item.get("categories") or {}).items():
        rate = _dec(spec["gst_rate"], f"itemised.categories.{cat}.gst_rate")
        need(Decimal(0) <= rate <= Decimal("0.28"), f"itemised.categories.{cat}.gst_rate must be 0–0.28")
        need(isinstance(spec.get("label"), str), f"itemised.categories.{cat} needs a label")
    need(item.get("units"), "itemised.units must list the units")
    for key in ("verify_url_base", "discount_line", "special_notes_label", "society_subsidy_note", "savings_title"):
        need(isinstance((data.get("render") or {}).get(key), str), f"render.{key} is required")
    need(data["render"]["verify_url_base"].startswith("https://"), "render.verify_url_base must be https")
    for key in ("addressee", "site", "connection"):
        need(key in (data.get("assistant_fields") or {}), f"assistant_fields.{key} is required")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def project_type(name: str) -> dict:
    return load()["project_types"][name]


def validity_days() -> int:
    return int(load()["workflow"]["validity_days"])


def degradation(technology: str) -> tuple[Decimal, Decimal]:
    spec = load()["savings"]["degradation"][TECHNOLOGIES.get(technology, "mono_perc")]
    return Decimal(str(spec["first_year"])), Decimal(str(spec["annual_after"]))


def gst_category(category: str) -> Optional[dict]:
    return (load()["itemised"]["categories"] or {}).get(category)


def price_band(project: str) -> Optional[tuple[Decimal, Decimal]]:
    band = (load().get("price_per_kwp_band") or {}).get(project)
    return None if band is None else (Decimal(str(band["low"])), Decimal(str(band["high"])))
