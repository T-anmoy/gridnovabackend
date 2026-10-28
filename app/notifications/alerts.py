"""Staff alert recipients by role (BE-17, config/access_control.yaml `alerts`).

Recipients of a staff email = the fixed addresses the notifier already had for the event
(STAFF_NOTIFY_EMAILS / ACCOUNTS_NOTIFY_EMAILS / OPERATIONS_NOTIFY_EMAILS — shared
mailboxes keep receiving) + every active employee holding one of the event's roles + the
lead's assigned employee when the event says so, the Studio proposal's owner when the
event says so (BE-18). Deduplicated, case-insensitively. A
deactivated employee's address is never used, even when it is also a fixed address. When
an event reaches nobody, a warning is logged once per event type.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import select

from app.auth import access

logger = logging.getLogger(__name__)

_warned: set[str] = set()


def _usable(email: Optional[str]) -> bool:
    """A deliverable address: `.local` is reserved (RFC 6762) — the placeholder
    `<id>@no-email.local` given to an employee whose token carried no email, and test rows."""
    return bool(email) and "@" in email and not email.lower().rstrip(".").endswith(".local")


def _people(db, event_type: str, payload: dict) -> tuple[list[str], set[str]]:
    """(addresses of the role holders and assigned employee, addresses of deactivated employees)."""
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Employee, Lead

    spec = access.load().alerts.get(event_type)
    memory = MemoryManager(db)
    out: list[str] = []
    if spec:
        for role in sorted(spec["roles"]):
            out += [e.email for e in memory.holders_of_role(role) if _usable(e.email)]
        if spec["assigned_employee"] and payload.get("lead_id"):
            try:
                lead = db.get(Lead, int(payload["lead_id"]))
            except (TypeError, ValueError):
                lead = None
            if lead is not None and lead.assigned_employee_id:
                emp = db.get(Employee, lead.assigned_employee_id)
                if emp is not None and emp.is_active and _usable(emp.email):
                    out.append(emp.email)
        if spec.get("owner") and payload.get("owner_id"):
            emp = db.get(Employee, str(payload["owner_id"]))
            if emp is not None and emp.is_active and _usable(emp.email):
                out.append(emp.email)
    inactive = {e.lower() for e in db.execute(select(Employee.email).where(Employee.is_active.is_(False))).scalars()
                if e}
    return out, inactive


def merge(fixed: Iterable[str], people: Iterable[str], inactive: set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for email in [*fixed, *people]:
        key = email.strip().lower()
        if not key or key in seen or key in inactive:
            continue
        seen.add(key)
        out.append(email.strip())
    return out


def recipients(event_type: str, payload: dict, fixed: list[str]) -> list[str]:
    """Never raises: a lookup failure falls back to the fixed addresses alone."""
    from app.memory.db import SessionLocal

    try:
        with SessionLocal() as db:
            people, inactive = _people(db, event_type, payload)
    except Exception:
        logger.exception("Could not look up alert recipients by role; using the fixed addresses",
                         extra={"event_type": event_type})
        people, inactive = [], set()
    to = merge(fixed, people, inactive)
    if not to and event_type not in _warned:
        _warned.add(event_type)
        logger.warning("Staff alert %s reaches nobody: no active holder of its roles and no fixed address",
                       event_type, extra={"event_type": event_type})
    return to


def describe(db, *, with_addresses: bool = False) -> list[dict]:
    """For GET /access/alerts: per event, the roles, how many active people hold them,
    and how many fixed addresses also receive it. `with_addresses` (BE-19, a viewer with
    staff.manage) adds `addresses`: the fixed addresses and each role holder's address
    actually emailed today (deduplicated, deactivated people left out) — the lead's
    assigned employee and a Studio owner vary per record and are not listed."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications.notifier import fixed_recipients

    memory = MemoryManager(db)
    inactive = None
    out = []
    for event, spec in access.load().alerts.items():
        people = {r: [e.email for e in memory.holders_of_role(r) if _usable(e.email)] for r in sorted(spec["roles"])}
        holders = {r: len(v) for r, v in people.items()}
        fixed_list = fixed_recipients(event)
        fixed = len(fixed_list)
        row = {"event": event, "roles": sorted(spec["roles"], key=access.ROLE_IDS.index),
               "assigned_employee": spec["assigned_employee"], "owner": spec.get("owner", False),
               "active_holders": holders,
               "fixed_addresses": fixed,
               "reaches_someone": bool(fixed or any(holders.values()) or event == "approval_requested"
                                          or spec.get("owner", False))}
        if with_addresses:
            if inactive is None:
                from app.memory.models import Employee
                inactive = {e.lower() for e in db.execute(select(Employee.email).where(
                    Employee.is_active.is_(False))).scalars() if e}
            row["addresses"] = {"fixed": merge(fixed_list, [], inactive),
                                "by_role": {r: merge([], v, inactive) for r, v in people.items()}}
        out.append(row)
    return out
