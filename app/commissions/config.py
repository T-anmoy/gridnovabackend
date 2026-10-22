"""config/commissions.yaml — loaded once, validated at startup (app.main).

Values Grid Nova has not confirmed are registered in config/placeholder_registry.yaml
(C5-*); this module only checks that the file is usable.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "commissions.yaml"

BASES = ("basic_pro_rata", "gross")
SEAT_HOLDER_AT = ("agreement_signed", "payment_verified")
AGREEMENT_DATE_SOURCES = ("stage_recorded", "stamp_paper_date")
MULTIPLE_HOLDERS = ("split_equally", "each_full_rate")


class CommissionConfigError(RuntimeError):
    pass


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise CommissionConfigError(f"{_PATH.name}: {message}")

    c = data.get("commissions") or {}
    need(isinstance(c.get("rate_change_notice_days"), int) and c["rate_change_notice_days"] >= 0,
         "commissions.rate_change_notice_days must be a whole number of days")
    need(c.get("basis") in BASES, f"commissions.basis must be one of {BASES}")
    need(c.get("territory_seat_holder_at") in SEAT_HOLDER_AT,
         f"commissions.territory_seat_holder_at must be one of {SEAT_HOLDER_AT}")
    need(c.get("agreement_date_source") in AGREEMENT_DATE_SOURCES,
         f"commissions.agreement_date_source must be one of {AGREEMENT_DATE_SOURCES}")
    need(c.get("multiple_territory_holders") in MULTIPLE_HOLDERS,
         f"commissions.multiple_territory_holders must be one of {MULTIPLE_HOLDERS}")
    for key in ("territory_on_own_customers", "deactivated_associates_earn"):
        need(isinstance(c.get(key), bool), f"commissions.{key} must be true or false")
    from app.pricing.price_list import PACKAGES
    e = data.get("estimate") or {}
    need(e.get("indicative_package_when_several") in ("lowest_final", *PACKAGES),
         f"estimate.indicative_package_when_several must be lowest_final or one of {PACKAGES}")
    q = data.get("queries") or {}
    for key in ("window_days", "response_working_days"):
        need(isinstance(q.get(key), int) and q[key] > 0, f"queries.{key} must be a positive whole number")
    days = q.get("working_weekdays")
    need(isinstance(days, list) and days and all(isinstance(d, int) and 0 <= d <= 6 for d in days)
         and len(set(days)) == len(days), "queries.working_weekdays must list distinct weekdays 0-6")
    p = data.get("payout") or {}
    need(isinstance(p.get("pay_by_day_of_following_month"), int) and 1 <= p["pay_by_day_of_following_month"] <= 28,
         "payout.pay_by_day_of_following_month must be a day 1-28")
    need(isinstance(p.get("recent_days"), int) and p["recent_days"] > 0, "payout.recent_days must be positive")
    t = data.get("tds") or {}
    for key in ("rate_percent", "no_pan_rate_percent"):
        v = _decimal(t.get(key))
        need(v.is_finite() and Decimal(0) <= v <= Decimal(100), f"tds.{key} must be a percentage 0-100")
    v = _decimal(t.get("fy_threshold_inr"))
    need(v.is_finite() and v >= 0, "tds.fy_threshold_inr must be a non-negative amount")
    v = _decimal(t.get("rounding"))
    need(v in (Decimal("0.01"), Decimal("1")), "tds.rounding must be \"0.01\" or \"1\"")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    return _validate(yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {})


def commissions() -> dict:
    return load()["commissions"]


def estimate() -> dict:
    return load()["estimate"]


def queries() -> dict:
    return load()["queries"]


def payout() -> dict:
    return load()["payout"]


def tds() -> dict:
    return load()["tds"]
