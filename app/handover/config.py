"""config/handover_warranty.yaml — loaded once, validated at startup (app.main).

Values Grid Nova has not supplied are marked in the file and registered in the
placeholder registry (HW-* entries); this module only checks the file is usable.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "handover_warranty.yaml"
UNIT_CATEGORIES = ("module", "inverter", "other")
ACK_METHODS = ("signature", "in_person")


class HandoverConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise HandoverConfigError(f"{_PATH.name}: {message}")

    def whole(v, positive=True) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and (v > 0 if positive else v >= 0)

    h, w, s = (data.get(k) or {} for k in ("handover", "warranty", "service"))
    need(isinstance(h.get("pack_required"), bool), "handover.pack_required must be true or false")
    need(whole(h.get("final_payment_through_milestone")), "handover.final_payment_through_milestone must be a milestone number")
    need(h.get("link_expiry_days") is None or whole(h["link_expiry_days"]),
         "handover.link_expiry_days must be a positive whole number of days or null")
    need(h.get("acknowledgement_methods") and set(h["acknowledgement_methods"]) <= set(ACK_METHODS),
         f"handover.acknowledgement_methods must be among {ACK_METHODS}")
    need(whole(h.get("signature_max_kb")), "handover.signature_max_kb must be a positive number")
    need(whole(w.get("system_warranty_years")), "warranty.system_warranty_years must be a positive whole number")
    defaults = w.get("unit_default_years") or {}
    need(set(defaults) == set(UNIT_CATEGORIES) and all(v is None or whole(v) for v in defaults.values()),
         f"warranty.unit_default_years must give each of {UNIT_CATEGORIES} as whole years or null")
    need(w.get("panel_performance_life_years") is None or whole(w["panel_performance_life_years"]),
         "warranty.panel_performance_life_years must be whole years or null")
    need(whole(w.get("om_free_months"), positive=False), "warranty.om_free_months must be a whole number of months")
    need(isinstance(w.get("om_after_free_terms"), str) and w["om_after_free_terms"].strip(),
         "warranty.om_after_free_terms is required")
    need(s.get("sla_hours") is None or whole(s["sla_hours"]), "service.sla_hours must be a positive whole number or null")
    contact = s.get("contact") or {}
    need(set(contact) <= {"phone", "email"}, "service.contact may give phone and email only")
    need(whole(s.get("associate_notice_days")), "service.associate_notice_days must be a positive whole number")
    need(whole(s.get("max_photos")), "service.max_photos must be a positive whole number")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_PATH, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def handover() -> dict:
    return load()["handover"]


def warranty() -> dict:
    return load()["warranty"]


def service() -> dict:
    return load()["service"]
