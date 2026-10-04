"""config/pricing_policy.yaml — how the price list is applied (applicability, size
rounding, public display). Loaded once, validated at startup. Every value is a
registered, still-unconfirmed default (config/placeholder_registry.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "pricing_policy.yaml"
ROUNDINGS = ("ceil",)
DISPLAYS = ("exact", "hidden")


class PricingPolicyError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond, message: str) -> None:
        if not cond:
            raise PricingPolicyError(f"{_PATH.name}: {message}")

    app = data.get("applicability") or {}
    need(isinstance(app.get("states"), list) and app["states"], "applicability.states must list states")
    need(isinstance(app.get("segments"), list) and app["segments"], "applicability.segments must list segments")
    need(isinstance(app.get("connection_types"), list) and app["connection_types"]
         and set(app["connection_types"]) <= {"on_grid", "off_grid", "hybrid"},
         "applicability.connection_types must be among on_grid, off_grid, hybrid")
    for key in ("housing_society_allowed", "battery_allowed", "require_known_phase"):
        need(isinstance(app.get(key), bool), f"applicability.{key} must be true/false")
    need(data.get("size_rounding") in ROUNDINGS, f"size_rounding must be one of {ROUNDINGS}")
    need(data.get("public_price_display") in DISPLAYS, f"public_price_display must be one of {DISPLAYS}")
    need(data.get("as_recommended_package_in_proposal") == "none",
         "as_recommended_package_in_proposal: only `none` is implemented")
    need(isinstance(data.get("nova_may_quote_list_prices"), bool), "nova_may_quote_list_prices must be true/false")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def applicability() -> dict:
    return load()["applicability"]
