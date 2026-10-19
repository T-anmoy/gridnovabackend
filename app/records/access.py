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

KYC fields (Aadhaar, PAN, bank, identity documents) are never read by any tool.
"""
from __future__ import annotations

from app.archive import config as visibility
from app.memory.models import EmployeeRole

A, M, S, E, O, C = (EmployeeRole.admin, EmployeeRole.management, EmployeeRole.sales, EmployeeRole.engineer,
                    EmployeeRole.operations, EmployeeRole.accounts)


def role_of(viewer) -> EmployeeRole:
    role = getattr(viewer, "role", viewer)
    return role if isinstance(role, EmployeeRole) else EmployeeRole(role)


def fields(viewer) -> frozenset:
    return visibility.visible_fields(role_of(viewer))


def sees(viewer, field: str) -> bool:
    return field in fields(viewer)


def sees_money(viewer) -> bool:
    return "contract_value_inr" in fields(viewer)


def reads_leads(viewer) -> bool:
    return role_of(viewer) in (A, M, S, E)


def lead_contact(viewer) -> bool:
    """Customer name/email/phone on a lead (an Engineer reads technical fields only)."""
    return role_of(viewer) in (A, M, S)


def reads_customer_memory(viewer) -> bool:
    return role_of(viewer) in (A, M, S)


def reads_payments(viewer) -> bool:
    return sees_money(viewer)


def reads_commissions(viewer) -> bool:
    return role_of(viewer) in (A, C)


def reads_service(viewer) -> bool:
    return role_of(viewer) in (A, M, S, E, O)


def reads_associates(viewer) -> bool:
    return sees(viewer, "associate_id")


def sees_prices(viewer) -> bool:
    return role_of(viewer) in (A, S)
