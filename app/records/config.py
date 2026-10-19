"""config/nova_records.yaml — loaded once and validated at startup (app.main).

Limits (rate limit NR-1, daily cap NR-2), retention (NR-3) and the dashboard paths that
citations link to.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "nova_records.yaml"
_LIMITS = ("requests_per_minute", "daily_questions", "max_tool_calls", "max_rows", "near_candidate_cap",
           "near_default_km", "tool_timeout_seconds", "history_turns")
_PATHS = {"live_project": "{onboarding_id}", "lead": "{lead_id}", "service_request": "{id}", "archive": "{id}"}


class RecordsConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise RecordsConfigError(f"{_PATH.name}: {message}")

    limits = data.get("limits") or {}
    for key in _LIMITS:
        need(isinstance(limits.get(key), int) and not isinstance(limits.get(key), bool) and limits[key] > 0,
             f"limits.{key} must be a positive whole number")
    need(limits["max_tool_calls"] <= 5, "limits.max_tool_calls must be 5 or fewer")
    need(limits["max_rows"] <= 200, "limits.max_rows must be 200 or fewer")
    days = (data.get("retention") or {}).get("days")
    need(isinstance(days, int) and not isinstance(days, bool) and days > 0, "retention.days must be a positive whole number")
    paths = data.get("paths") or {}
    need(set(paths) == set(_PATHS), f"paths must be exactly {sorted(_PATHS)}")
    for key, slot in _PATHS.items():
        need(isinstance(paths.get(key), str) and paths[key].startswith("/") and slot in paths[key],
             f"paths.{key} must be a path containing {slot}")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_PATH, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def limit(name: str) -> int:
    return load()["limits"][name]


def retention_days() -> int:
    return load()["retention"]["days"]


def path(kind: str, **values) -> str:
    return load()["paths"][kind].format(**values)
