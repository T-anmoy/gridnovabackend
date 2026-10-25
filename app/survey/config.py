"""config/site_survey.yaml — loaded once, validated at startup (app.main)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from app.memory.models import SurveyVariant

_PATH = Path(__file__).resolve().parents[2] / "config" / "site_survey.yaml"

# Every field a survey section may name — the associate-writable survey fields plus the
# existing site-assessment fields the paper checklist also asks for.
KNOWN_FIELDS = frozenset({
    "discom", "consumer_number", "connection_phase", "sanctioned_load_kw", "consumption_history",
    "roof_accessible", "roof_access_note", "roof_material", "roof_material_other", "roof_orientation",
    "roof_ownership_confirmed", "floors", "shading_condition", "roof_obstacles", "roof_sections",
    "roof_area_sqft", "shadow_free_area_sqft", "roof_age_years", "roofs_available_count", "roofs_considered_count",
    "future_roof_expansion_planned", "inverter_location", "indoor_wall_available", "distance_panel_to_inverter_m",
    "distance_inverter_to_lt_panel_m", "distance_method", "water_available", "society_flat_count",
    "society_noc_status", "contract_demand_kva", "tenant_noc_available",
})


# What reverification_fields may name: the survey fields, the averages derived from the
# bills, and the size the customer asked for.
REVERIFIABLE_FIELDS = KNOWN_FIELDS | {"survey_variant", "avg_monthly_units_kwh", "avg_monthly_bill_inr",
                                      "desired_output_kw"}


class SurveyConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise SurveyConfigError(f"{_PATH.name}: {message}")

    def positive(key: str) -> None:
        need(isinstance(data.get(key), (int, float)) and not isinstance(data[key], bool) and data[key] > 0,
             f"{key} must be a positive number")

    for key in ("survey_required_to_submit", "resign_after_edit", "review_required_for_formal_proposal"):
        need(isinstance(data.get(key), bool), f"{key} must be true/false")
    # BE-14: the roof area per kW and the bill-per-unit range moved to the solar estimate
    # engine (config/solar_estimate.yaml) — one source for the survey and the calculator.
    for key in ("area_sqft_per_kw", "implied_tariff_inr_per_kwh"):
        need(key not in data, f"{key} is now in config/solar_estimate.yaml (roof_area / tariffs); remove it here")
    for key in ("load_cap_ratio", "average_mismatch_ratio", "distance_max_m",
                "signature_max_kb", "review_target_hours"):
        positive(key)
    months = [data.get(k) for k in ("min_consumption_months", "requested_consumption_months",
                                    "max_consumption_months")]
    need(all(isinstance(m, int) and m > 0 for m in months) and months[0] <= months[1] <= months[2],
         "need whole numbers min_consumption_months <= requested_consumption_months <= max_consumption_months")
    variants = {v.value for v in SurveyVariant}
    plausible = data.get("max_plausible_roof_area_sqft") or {}
    need(set(plausible) == variants and all(isinstance(v, (int, float)) and v > 0 for v in plausible.values()),
         f"max_plausible_roof_area_sqft needs a positive number for each of {sorted(variants)}")
    options = data.get("discom_options") or []
    need(options and all(isinstance(o, str) and o.strip() for o in options), "discom_options must list names")
    sections = data.get("sections") or []
    keys = [s.get("key") for s in sections]
    need(sections and all(isinstance(k, str) and k for k in keys) and len(set(keys)) == len(keys),
         "sections need unique keys")
    for s in sections:
        need(s.get("fields") and set(s["fields"]) <= KNOWN_FIELDS,
             f"sections.{s.get('key')}.fields must name known survey fields")
    need(set(data.get("variants") or {}) == variants, f"variants must list exactly {sorted(variants)}")
    for name, extra in (data.get("variants") or {}).items():
        for section, fields in (extra or {}).items():
            need(section in keys, f"variants.{name}: unknown section {section}")
            need(fields and set(fields) <= KNOWN_FIELDS, f"variants.{name}.{section} must name known fields")
    for rule in data.get("conditional") or []:
        need(rule.get("section") in keys, f"conditional: unknown section {rule.get('section')}")
        need(isinstance(rule.get("when"), dict) and len(rule["when"]) == 1 and set(rule["when"]) <= KNOWN_FIELDS,
             "conditional.when must name one known field")
        need(rule.get("require") and set(rule["require"]) <= KNOWN_FIELDS,
             "conditional.require must name known fields")
    reverify = data.get("reverification_fields")
    need(isinstance(reverify, list) and reverify and set(reverify) <= REVERIFIABLE_FIELDS,
         f"reverification_fields must name fields from {sorted(REVERIFIABLE_FIELDS)}")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def required_fields(variant: str) -> list[tuple[str, list[str]]]:
    """[(section_key, [field, …]), …] in display order, before conditional fields."""
    cfg = load()
    extra = cfg["variants"].get(variant) or {}
    return [(s["key"], list(s["fields"]) + [f for f in extra.get(s["key"], []) if f not in s["fields"]])
            for s in cfg["sections"]]


MAX_ROOF_SECTIONS = 6


def options() -> dict:
    """What the survey form needs (served in GET /api/v1/associate/onboardings/options)."""
    from app.memory.models import (
        ROOF_OBSTACLES, DistanceMethod, InverterLocation, RoofMaterial, RoofOrientation, SocietyNocStatus,
    )

    cfg = load()
    return {
        "variants": {v.value: [{"key": key, "fields": fields} for key, fields in required_fields(v.value)]
                     for v in SurveyVariant},
        "conditional": cfg.get("conditional") or [],
        "discom_options": list(cfg["discom_options"]),
        "roof_materials": [m.value for m in RoofMaterial],
        "roof_orientations": [o.value for o in RoofOrientation],
        "roof_obstacles": list(ROOF_OBSTACLES),
        "inverter_locations": [x.value for x in InverterLocation],
        "distance_methods": [x.value for x in DistanceMethod],
        "society_noc_statuses": [x.value for x in SocietyNocStatus],
        "max_roof_sections": MAX_ROOF_SECTIONS,
        "requested_consumption_months": cfg["requested_consumption_months"],
        "min_consumption_months": cfg["min_consumption_months"],
        "max_consumption_months": cfg["max_consumption_months"],
        "distance_max_m": cfg["distance_max_m"],
        "signature_max_kb": cfg["signature_max_kb"],
        "required_to_submit": cfg["survey_required_to_submit"],
        "resign_after_edit": cfg["resign_after_edit"],
    }
