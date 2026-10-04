"""config/associate_onboarding.yaml — loaded once, validated at startup (app.main)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from app.memory.models import EquipmentTier

_PATH = Path(__file__).resolve().parents[2] / "config" / "associate_onboarding.yaml"
PLACEHOLDER_MARKER = "[PLACEHOLDER"
REQUIRED_WHEN = ("always", "scheme_linked", "not_in_customer_name")
SCORE_FACTORS = ("verification_call", "documents_complete", "subsidy_eligibility", "system_fit",
                 "customer_timeline", "site_readiness", "site_location", "associate_track_record")
TIMELINES = ("within_1_month", "within_3_months", "within_6_months", "later", "unsure")


class OnboardingConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise OnboardingConfigError(f"{_PATH.name}: {message}")

    versions = data.get("notice_versions") or {}
    need(data.get("current_notice_version") in versions, "current_notice_version must name one of notice_versions")
    days = data.get("attribution_lock_days")
    need(isinstance(days, int) and days > 0, "attribution_lock_days must be a positive whole number")
    docs = data.get("required_documents") or []
    need(docs, "required_documents must list at least one document")
    keys = [d.get("key") for d in docs]
    need(all(isinstance(k, str) and k for k in keys) and len(set(keys)) == len(keys),
         "required_documents keys must be unique, non-empty strings")
    need(all(d.get("required_when") in REQUIRED_WHEN for d in docs),
         f"required_when must be one of {REQUIRED_WHEN}")
    sanity = data.get("sanity") or {}
    for key in ("max_kw_to_sanctioned_load_ratio", "kwh_per_kw_per_month", "max_generation_to_consumption_ratio"):
        need(isinstance(sanity.get(key), (int, float)) and sanity[key] > 0, f"sanity.{key} must be a positive number")
    tariff = sanity.get("inr_per_kwh_for_bill_check")
    need(tariff is None or (isinstance(tariff, (int, float)) and tariff > 0),
         "sanity.inr_per_kwh_for_bill_check must be null or a positive number")
    scoring = data.get("scoring") or {}
    weights = scoring.get("weights") or {}
    need(set(weights) == set(SCORE_FACTORS), f"scoring.weights must have exactly {SCORE_FACTORS}")
    need(all(isinstance(v, (int, float)) and v >= 0 for v in weights.values()) and sum(weights.values()) == 100,
         "scoring.weights must be non-negative and sum to 100")
    bands = scoring.get("bands") or {}
    need(isinstance(bands.get("hot_min"), (int, float)) and isinstance(bands.get("warm_min"), (int, float))
         and 0 < bands["warm_min"] < bands["hot_min"] <= 100, "scoring.bands needs 0 < warm_min < hot_min <= 100")
    hours = scoring.get("response_hours") or {}
    need(set(hours) == {"hot", "warm", "cold"} and all(isinstance(v, (int, float)) and v > 0 for v in hours.values()),
         "scoring.response_hours needs positive hot, warm and cold")
    fractions = scoring.get("timeline_fractions") or {}
    need(set(fractions) == set(TIMELINES) and all(0 <= v <= 1 for v in fractions.values()),
         f"scoring.timeline_fractions needs a 0..1 value for each of {TIMELINES}")
    for key in ("system_fit_penalty", "unknown_fraction", "new_associate_fraction"):
        need(isinstance(scoring.get(key), (int, float)) and 0 <= scoring[key] <= 1, f"scoring.{key} must be 0..1")
    need(isinstance(scoring.get("track_record_min_history"), int) and scoring["track_record_min_history"] >= 1,
         "scoring.track_record_min_history must be a whole number >= 1")
    fractions = scoring.get("site_location_fractions") or {}
    need(set(fractions) == {"confirmed_verified", "confirmed_unverified"}
         and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in fractions.values()),
         "scoring.site_location_fractions needs 0..1 confirmed_verified and confirmed_unverified")
    _validate_location(data.get("location") or {}, need)
    agreement = data.get("agreement") or {}
    need(isinstance(agreement.get("body"), str) and agreement["body"].strip(), "agreement.body must be text")
    margin = agreement.get("stamp_paper_top_margin_mm")
    need(isinstance(margin, (int, float)) and 0 <= margin <= 200, "agreement.stamp_paper_top_margin_mm must be 0..200")
    packages = data.get("equipment_packages") or {}
    need(packages, "equipment_packages must not be empty")
    for name, p in packages.items():
        need(p.get("equipment_tier") in {t.value for t in EquipmentTier},
             f"equipment_packages.{name}.equipment_tier must be one of {[t.value for t in EquipmentTier]}")
    return data


def _validate_location(loc: dict, need) -> None:
    need(isinstance(loc.get("required_to_submit"), bool), "location.required_to_submit must be true/false")
    acc = [loc.get(k) for k in ("accuracy_good_m", "accuracy_warn_m", "accuracy_reject_m")]
    need(all(isinstance(v, (int, float)) and v > 0 for v in acc) and acc[0] <= acc[1] <= acc[2],
         "location needs positive accuracy_good_m <= accuracy_warn_m <= accuracy_reject_m")
    need(isinstance(loc.get("photo_mismatch_m"), (int, float)) and loc["photo_mismatch_m"] > 0,
         "location.photo_mismatch_m must be a positive number")
    box = loc.get("india_bbox") or {}
    need(set(box) == {"lat_min", "lat_max", "lon_min", "lon_max"}
         and all(isinstance(v, (int, float)) for v in box.values())
         and -90 <= box["lat_min"] < box["lat_max"] <= 90 and -180 <= box["lon_min"] < box["lon_max"] <= 180,
         "location.india_bbox needs lat_min < lat_max and lon_min < lon_max")
    hosts = loc.get("link_hosts_allowlist") or []
    need(hosts and all(isinstance(h, str) and h == h.strip().lower() and h and "/" not in h and ":" not in h
                       for h in hosts),
         "location.link_hosts_allowlist must list lower-case host names (no scheme, port or path)")
    for key in ("link_resolve_rate_limit", "sample_seconds"):
        need(isinstance(loc.get(key), int) and loc[key] > 0, f"location.{key} must be a positive whole number")


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def current_notice() -> tuple[str, str]:
    data = load()
    version = data["current_notice_version"]
    return version, str(data["notice_versions"][version]["text"]).strip()


def notice_is_placeholder() -> bool:
    return PLACEHOLDER_MARKER in current_notice()[1]


def required_document_keys(*, scheme_linked: bool, all_in_customer_name: bool) -> list[str]:
    applies = {"always": True, "scheme_linked": scheme_linked, "not_in_customer_name": not all_in_customer_name}
    return [d["key"] for d in load()["required_documents"] if applies[d["required_when"]]]


def document_keys() -> list[str]:
    return [d["key"] for d in load()["required_documents"]]


def agreement_is_placeholder() -> bool:
    return PLACEHOLDER_MARKER in load()["agreement"]["body"]


def location() -> dict:
    return load()["location"]
