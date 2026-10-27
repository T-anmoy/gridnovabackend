"""config/access_control.yaml — the permission catalogue, the role bundles, the second-
approval switches and the alert routing. Loaded once and validated at startup (app.main).

Nothing here touches the database: the roles a person holds come from employee_roles
(app/auth/dependencies.py); this module only says what those roles mean.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from app.memory.models import EmployeeRole

_PATH = Path(__file__).resolve().parents[2] / "config" / "access_control.yaml"
ROLE_IDS = tuple(r.value for r in EmployeeRole)
SECOND_APPROVAL_ACTIONS = ("role.admin", "role.accounts", "commissions.standard_rate_publish",
                           "commissions.individual_rate_change", "payouts.batch_approve")


class AccessConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Permission:
    id: str
    description: str
    sensitive: bool


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    description: str
    permissions: frozenset
    requires_two_factor: bool
    can_assign_roles: frozenset


@dataclass(frozen=True)
class AccessConfig:
    permissions: dict          # id → Permission, in file order
    roles: dict                # id → Role, in file order
    exclusive_roles: frozenset
    invitation_expiry_days: int
    min_active_admins: int
    approval_expiry_hours: int
    second_approval: dict      # action → bool
    alerts: dict               # event → {"roles": frozenset, "assigned_employee": bool, "owner": bool}


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _build(data: dict) -> AccessConfig:
    def need(cond, message: str) -> None:
        if not cond:
            raise AccessConfigError(f"{_PATH.name}: {message}")

    raw_perms = data.get("permissions") or {}
    need(isinstance(raw_perms, dict) and raw_perms, "permissions must be a non-empty mapping")
    permissions = {}
    for pid, spec in raw_perms.items():
        need(isinstance(spec, dict) and isinstance(spec.get("description"), str) and spec["description"].strip(),
             f"permission {pid!r} needs a description")
        need(isinstance(spec.get("sensitive", False), bool), f"permission {pid!r}: sensitive must be true or false")
        permissions[pid] = Permission(pid, spec["description"].strip(), bool(spec.get("sensitive", False)))

    raw_roles = data.get("roles") or {}
    need(set(raw_roles) == set(ROLE_IDS), f"roles must define exactly {ROLE_IDS}")
    roles = {}
    for rid, spec in raw_roles.items():
        need(isinstance(spec, dict), f"role {rid!r} must be a mapping")
        for key in ("label", "description"):
            need(isinstance(spec.get(key), str) and spec[key].strip(), f"role {rid!r} needs a {key}")
        granted = spec.get("permissions")
        exclude = set(spec.get("exclude") or [])
        need(exclude <= set(permissions), f"role {rid!r} excludes an unknown permission")
        if granted == "all":
            held = set(permissions) - exclude
        else:
            need(isinstance(granted, list), f"role {rid!r}: permissions must be a list or 'all'")
            unknown = set(granted) - set(permissions)
            need(not unknown, f"role {rid!r} grants unknown permissions {sorted(unknown)}")
            need(len(granted) == len(set(granted)), f"role {rid!r} lists a permission twice")
            need(not exclude, f"role {rid!r}: exclude only applies to permissions: all")
            held = set(granted)
        need(isinstance(spec.get("requires_two_factor"), bool), f"role {rid!r}: requires_two_factor must be set")
        assignable = spec.get("can_assign_roles")
        if assignable == "all":
            assignable = set(ROLE_IDS)
        need(isinstance(assignable, (list, set)) and set(assignable) <= set(ROLE_IDS),
             f"role {rid!r}: can_assign_roles must be 'all' or a list of roles")
        need(not assignable or "staff.manage" in held, f"role {rid!r} assigns roles without staff.manage")
        roles[rid] = Role(rid, spec["label"].strip(), spec["description"].strip(), frozenset(held),
                          spec["requires_two_factor"], frozenset(assignable))

    exclusive = data.get("exclusive_roles") or []
    need(isinstance(exclusive, list) and set(exclusive) <= set(ROLE_IDS), "exclusive_roles must list roles")

    staff = data.get("staff") or {}
    need(_positive_int(staff.get("invitation_expiry_days")), "staff.invitation_expiry_days must be a positive integer")
    need(_positive_int(staff.get("min_active_admins")), "staff.min_active_admins must be a positive integer")

    approvals = data.get("approvals") or {}
    need(_positive_int(approvals.get("expiry_hours")), "approvals.expiry_hours must be a positive integer")
    switches = approvals.get("require_second_approval") or {}
    need(set(switches) == set(SECOND_APPROVAL_ACTIONS),
         f"approvals.require_second_approval must set exactly {SECOND_APPROVAL_ACTIONS}")
    need(all(isinstance(v, bool) for v in switches.values()), "approvals.require_second_approval values are true/false")

    alerts = {}
    for event, spec in (data.get("alerts") or {}).items():
        need(isinstance(spec, dict) and isinstance(spec.get("roles"), list), f"alerts.{event} needs a roles list")
        need(set(spec["roles"]) <= set(ROLE_IDS), f"alerts.{event} names an unknown role")
        need(isinstance(spec.get("assigned_employee", False), bool), f"alerts.{event}.assigned_employee is true/false")
        need(isinstance(spec.get("owner", False), bool), f"alerts.{event}.owner is true/false")
        alerts[event] = {"roles": frozenset(spec["roles"]), "assigned_employee": bool(spec.get("assigned_employee")),
                         "owner": bool(spec.get("owner"))}

    return AccessConfig(permissions=permissions, roles=roles, exclusive_roles=frozenset(exclusive),
                        invitation_expiry_days=staff["invitation_expiry_days"],
                        min_active_admins=staff["min_active_admins"],
                        approval_expiry_hours=approvals["expiry_hours"], second_approval=dict(switches),
                        alerts=alerts)


@lru_cache(maxsize=1)
def load() -> AccessConfig:
    with open(_PATH, encoding="utf-8") as fh:
        return _build(yaml.safe_load(fh) or {})


def _ids(roles: Iterable) -> set[str]:
    return {getattr(r, "value", r) for r in roles}


def permissions_for(roles: Iterable) -> frozenset:
    cfg = load()
    out: set[str] = set()
    for rid in _ids(roles):
        role = cfg.roles.get(rid)
        if role is not None:
            out |= role.permissions
    return frozenset(out)


def granting_roles(roles: Iterable, permission: str) -> set[str]:
    """Which of these roles grant `permission`."""
    cfg = load()
    return {rid for rid in _ids(roles) if rid in cfg.roles and permission in cfg.roles[rid].permissions}


def is_sensitive(permission: str) -> bool:
    p = load().permissions.get(permission)
    return bool(p and p.sensitive)


def needs_two_factor(roles: Iterable, permission: str) -> bool:
    """A sensitive permission needs a second factor unless at least one held role that
    grants it does not require one."""
    if not is_sensitive(permission):
        return False
    cfg = load()
    granting = granting_roles(roles, permission)
    return bool(granting) and all(cfg.roles[r].requires_two_factor for r in granting)


def requires_two_factor(roles: Iterable) -> bool:
    cfg = load()
    return any(cfg.roles[r].requires_two_factor for r in _ids(roles) if r in cfg.roles)


def assignable_roles(roles: Iterable) -> frozenset:
    """The roles a holder of `roles` may grant, remove, invite to or deactivate."""
    cfg = load()
    out: set[str] = set()
    for rid in _ids(roles):
        if rid in cfg.roles:
            out |= cfg.roles[rid].can_assign_roles
    return frozenset(out)


def combination_problem(roles: Iterable) -> str | None:
    """Why this set of roles cannot be held together, or None."""
    ids = _ids(roles)
    if not ids:
        return "A person must hold at least one role."
    unknown = ids - set(ROLE_IDS)
    if unknown:
        return f"Unknown role(s): {', '.join(sorted(unknown))}."
    clash = sorted(ids & load().exclusive_roles)
    if len(clash) > 1:
        return (f"{' and '.join(clash)} cannot be held together: each limits what its holder sees, "
                "and together they would conflict.")
    return None


def second_approval_required(action: str) -> bool:
    return bool(load().second_approval.get(action))


def role_label(role) -> str:
    rid = getattr(role, "value", role)
    r = load().roles.get(rid)
    return r.label if r else rid


def primary_role(roles: Iterable) -> str:
    """The `employees.role` kept for compatibility: the first held role in the file's order
    (admin before management before … accounts)."""
    ids = _ids(roles)
    for rid in load().roles:
        if rid in ids:
            return rid
    raise ValueError("no roles")


def matrix() -> dict:
    cfg = load()
    return {
        "permissions": [{"id": p.id, "description": p.description, "sensitive": p.sensitive}
                        for p in cfg.permissions.values()],
        "roles": [{"id": r.id, "label": r.label, "description": r.description,
                   "requires_two_factor": r.requires_two_factor,
                   "can_assign_roles": sorted(r.can_assign_roles, key=ROLE_IDS.index),
                   "permissions": [p for p in cfg.permissions if p in r.permissions]}
                  for r in cfg.roles.values()],
        "exclusive_roles": sorted(cfg.exclusive_roles, key=ROLE_IDS.index),
    }


def viewer_roles(viewer) -> frozenset:
    """The role ids of a viewer: an AuthenticatedEmployee (all held roles), anything with a
    `role` attribute (tests, the eval harness), or a bare role."""
    held = getattr(viewer, "roles", None)
    if held:
        return frozenset(_ids(held))
    return frozenset(_ids([getattr(viewer, "role", viewer)]))


def viewer_holds(viewer, permission: str) -> bool:
    """`viewer.holds(permission)` when the viewer is a signed-in employee (two-step
    verification applies), else whether its roles grant the permission."""
    if hasattr(viewer, "holds"):
        return viewer.holds(permission)
    if permission not in load().permissions:
        raise KeyError(f"Unknown permission {permission!r}")
    return permission in permissions_for(viewer_roles(viewer))


# The record-level narrowing a role applies to live projects (app/projects/view.py,
# Nova Records): Sales its own or unassigned leads, Operations projects in execution,
# a Network Manager and the Proposal Desk none; every other role all. Several roles: the
# widest wins.
_LIVE_RULES = {"sales": "assigned", "operations": "execution", "network_manager": "none", "proposal_desk": "none"}


def live_project_rule(viewer) -> str:
    kinds = {_LIVE_RULES.get(r, "all") for r in viewer_roles(viewer)}
    for kind in ("all", "assigned", "execution"):
        if kind in kinds:
            return kind
    return "none"
