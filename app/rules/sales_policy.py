"""config/sales_policy.yaml — proposal validity, approver rule, public booking limits
and chat session lifetime. Loaded once, validated at startup. Every value is a
registered, still-unconfirmed default (config/placeholder_registry.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "sales_policy.yaml"


class SalesPolicyError(RuntimeError):
    pass


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate(data: dict) -> dict:
    def need(cond, message: str) -> None:
        if not cond:
            raise SalesPolicyError(f"{_PATH.name}: {message}")

    need(_positive_int(data.get("proposal_validity_days")), "proposal_validity_days must be a positive whole number")
    need(isinstance(data.get("require_different_approver_for_all_leads"), bool),
         "require_different_approver_for_all_leads must be true/false")
    booking = data.get("booking") or {}
    need(booking.get("max_duration_minutes") is None or _positive_int(booking.get("max_duration_minutes")),
         "booking.max_duration_minutes must be a positive whole number or null")
    need(_positive_int(booking.get("daily_cap_per_email")), "booking.daily_cap_per_email must be a positive whole number")
    chat = data.get("chat") or {}
    need(_positive_int(chat.get("session_ttl_days")), "chat.session_ttl_days must be a positive whole number")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def proposal_validity_days() -> int:
    return load()["proposal_validity_days"]


def require_different_approver_for_all_leads() -> bool:
    return load()["require_different_approver_for_all_leads"]


def max_booking_duration_minutes() -> int:
    from app.config import settings
    configured = load()["booking"]["max_duration_minutes"]
    return configured if configured is not None else settings.scheduling_default_duration_minutes


def booking_daily_cap_per_email() -> int:
    return load()["booking"]["daily_cap_per_email"]


def chat_session_ttl_days() -> int:
    return load()["chat"]["session_ttl_days"]
