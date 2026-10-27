"""Make someone an Admin when no Admin can do it from the dashboard yet (BE-17 bootstrap).

    python scripts/grant_admin.py --email shouvik@gridnova.in --name "Shouvik Biswas"

  * An employee with that email already exists → Admin is added to their roles (kept if
    already held). Their other roles are untouched.
  * Otherwise → a pending staff invitation with the Admin role (invited_by "bootstrap").
    With CLERK_SECRET_KEY set, Clerk emails the link; the person's first sign-in with that
    verified address creates them as an Admin. An existing pending invitation for the
    address is reused (its expiry refreshed), never duplicated.

Idempotent: running it twice changes nothing the second time except an invitation's
expiry. Every change is audit-logged with performed_by "bootstrap". No second approval
— this is how the first Admins exist at all.

Staging and production: refused unless run as the migration job (Cloud Run sets
CLOUD_RUN_JOB; RUNBOOK §8), so it cannot be run against the live database from a laptop.

Exit codes: 0 done, 2 refused, 1 error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
MIGRATION_JOB = "gridnova-migrate"
BOOTSTRAP = "bootstrap"


def allowed_here(environment: str, cloud_run_job: str | None) -> bool:
    if environment in ("development", "test"):
        return True
    return cloud_run_job == MIGRATION_JOB


def grant(db, email: str, name: str) -> str:
    """Do it; return a one-line description of what happened. Caller commits."""
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.auth import access
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Employee, StaffInvitation, StaffInvitationStatus
    from app.staff import invitations

    email = invitations.normalise_email(email)
    if "@" not in email:
        raise ValueError("a valid --email is required")
    memory = MemoryManager(db)
    emp = db.execute(select(Employee).where(func.lower(Employee.email) == email)).scalar_one_or_none()
    if emp is not None:
        held = memory.employee_roles(emp.id)
        if "admin" in held and emp.is_active:
            return f"{email} is already an active Admin ({emp.id}); nothing changed."
        if "admin" not in held:
            memory.replace_employee_roles(emp.id, sorted(set(held) | {"admin"}), performed_by=BOOTSTRAP)
        if not emp.is_active:
            memory.set_employee_active(emp.id, active=True, performed_by=BOOTSTRAP)
        return f"{email} ({emp.id}) now holds Admin; roles: {', '.join(memory.employee_roles(emp.id))}."

    pending = db.execute(select(StaffInvitation).where(StaffInvitation.email == email,
                                                       StaffInvitation.status.in_((StaffInvitationStatus.pending,
                                                                                   StaffInvitationStatus.expired)))
                         ).scalars().first()
    if pending is not None:
        if "admin" not in pending.roles:
            raise ValueError(f"{email} has a pending invitation without Admin ({', '.join(pending.roles)}); "
                             "revoke it from the dashboard first")
        before = pending.expires_at.isoformat()
        pending.status = StaffInvitationStatus.pending
        pending.expires_at = invitations._now() + timedelta(days=access.load().invitation_expiry_days)
        invitations._audit(db, pending, "invitation_resent", BOOTSTRAP, before={"expires_at": before},
                           after={"expires_at": pending.expires_at.isoformat()})
        return f"{email} already had an Admin invitation ({pending.id}); expiry refreshed to {pending.expires_at:%Y-%m-%d}."
    inv = invitations.issue(db, name=name or email, email=email, roles=["admin"], invited_by=BOOTSTRAP)
    return (f"Invited {email} as Admin ({inv.id}, Clerk: {inv.clerk_status}); "
            f"their first sign-in creates them. Expires {inv.expires_at:%Y-%m-%d}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make someone a Grid Nova Admin (bootstrap).")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args(argv)

    from app.config import settings

    if not allowed_here(settings.environment, os.environ.get("CLOUD_RUN_JOB")):
        print(f"grant_admin: refused — ENVIRONMENT={settings.environment} and this is not the {MIGRATION_JOB} "
              "job. Run it as the migration job (deploy/RUNBOOK.md §8).", file=sys.stderr)
        return EXIT_REFUSED

    from app.memory.db import SessionLocal
    from app.staff import StaffError

    with SessionLocal() as db:
        try:
            message = grant(db, args.email, args.name)
            db.commit()
        except (ValueError, StaffError) as exc:
            db.rollback()
            print(f"grant_admin: {getattr(exc, 'message', exc)}", file=sys.stderr)
            return EXIT_ERROR
    print(f"grant_admin: {message}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
