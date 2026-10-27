"""What each staff role may read through Nova Records — derived from the rules the rest
of the dashboard already applies, never a second copy of them:

  * project fields      config/archive.yaml through app/archive/config.py (BE-8, ARC-2)
  * which live projects app/projects/view.py can_view_live (Sales own/unassigned leads,
                        Operations projects in execution)
  * leads               app/auth/scoping.py (Sales own + unassigned; Engineer technical
                        fields only; Operations and Accounts none)
  * customer memory     AuthenticatedEmployee.can_query_customer_memory (/memory/ask)
  * payments            Admin, Management, Accounts — the roles config/archive.yaml gives
                        the money group. Narrower than the payments routes for Sales
                        (which read their own leads' payments there): Nova Records shows
                        Sales no money at all.
  * commissions         Admin and Accounts (BE-9 brief; narrower than the commission
                        routes, where Management also reads)
  * service requests    every role that reads execution, Accounts none (BE-5)
  * list prices         Admin and Sales (the roles that see /pricing/breakdown today)

BE-17: each rule is the permission behind it (config/access_control.yaml), so a person
holding several roles reads the union.

KYC fields (Aadhaar, PAN, bank, identity documents) are never read by any tool.
"""
from __future__ import annotations

from app.archive import config as visibility
from app.auth.access import viewer_holds
from app.memory.models import EmployeeRole


def role_of(viewer) -> EmployeeRole:
    """The viewer's primary role — for prompts and stored turns, never for a decision."""
    role = getattr(viewer, "role", viewer)
    return role if isinstance(role, EmployeeRole) else EmployeeRole(role)


def fields(viewer) -> frozenset:
    return visibility.visible_fields(viewer)


def sees(viewer, field: str) -> bool:
    return field in fields(viewer)


def sees_money(viewer) -> bool:
    return "contract_value_inr" in fields(viewer)


def reads_leads(viewer) -> bool:
    return viewer_holds(viewer, "leads.view_all") or viewer_holds(viewer, "leads.view_assigned")


def lead_contact(viewer) -> bool:
    """Customer name/email/phone on a lead (an Engineer reads technical fields only)."""
    return viewer_holds(viewer, "leads.view_commercial")


def reads_customer_memory(viewer) -> bool:
    return viewer_holds(viewer, "customers.memory_query")


def reads_payments(viewer) -> bool:
    return sees_money(viewer)


def reads_commissions(viewer) -> bool:
    return viewer_holds(viewer, "records.commissions.read")


def reads_service(viewer) -> bool:
    return viewer_holds(viewer, "execution.view")


def reads_associates(viewer) -> bool:
    return sees(viewer, "associate_id")


def sees_prices(viewer) -> bool:
    return viewer_holds(viewer, "pricing.adjust")


def memory_narrowed_to_assigned(viewer) -> bool:
    """Customer memory as Sales reads it: customers with a lead that is theirs or unassigned."""
    return viewer_holds(viewer, "leads.view_assigned") and not viewer_holds(viewer, "leads.view_all")

