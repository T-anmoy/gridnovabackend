"""config/archive.yaml — loaded once and validated at startup (app.main).

Holds the role-to-field visibility map for archived and live projects (registry entry
ARC-2, unconfirmed) and the importer's limits. A field is in exactly one group; a role is
given a list of groups.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "archive.yaml"
# A role not listed in config/archive.yaml (the Network Manager, BE-17) sees no field.
ROLES = ("admin", "management", "accounts", "operations", "engineer", "sales")


class ArchiveConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise ArchiveConfigError(f"{_PATH.name}: {message}")

    vis = data.get("visibility") or {}
    groups, roles = vis.get("groups") or {}, vis.get("roles") or {}
    need(groups and all(isinstance(v, list) and v for v in groups.values()), "visibility.groups must list fields per group")
    seen: dict[str, str] = {}
    for group, fields in groups.items():
        for name in fields:
            need(name not in seen, f"field {name!r} is in both {seen.get(name)!r} and {group!r}")
            seen[name] = group
    need(set(roles) == set(ROLES), f"visibility.roles must give exactly {ROLES}")
    for role, held in roles.items():
        need(isinstance(held, list) and set(held) <= set(groups), f"visibility.roles.{role} names an unknown group")
    imp = data.get("import") or {}
    for key in ("max_file_mb", "max_rows", "max_report_issues"):
        need(isinstance(imp.get(key), int) and not isinstance(imp.get(key), bool) and imp[key] > 0,
             f"import.{key} must be a positive whole number")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_PATH, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def import_limits() -> dict:
    return load()["import"]


def field_groups() -> dict[str, str]:
    """field name → group."""
    return {name: group for group, names in load()["visibility"]["groups"].items() for name in names}


def groups_for(role) -> frozenset:
    """The groups a role — or a viewer holding several roles (their union) — may see.
    An unknown role sees nothing."""
    from app.auth.access import viewer_roles

    roles = load()["visibility"]["roles"]
    return frozenset(g for key in viewer_roles(role) for g in roles.get(key, ()))


def visible_fields(role) -> frozenset:
    held = groups_for(role)
    return frozenset(name for name, group in field_groups().items() if group in held)
