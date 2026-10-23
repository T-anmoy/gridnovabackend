"""An associate's ESTIMATED commission on one of their customers (BE-10, "what will I
earn?"). An estimate only: nothing is written, and the commission an associate is paid
still comes only from the ledger (app/commissions/service.py), entry by entry as
payments are verified.

Worked out with the ledger's own rules, never a copy of them:
  * the value — the accepted proposal's total (basis `accepted_proposal`), else the
    customer's latest list-priced indicative estimate (basis `indicative_estimate`; when
    it shows both packages, config estimate.indicative_package_when_several, C5-EST),
    else there is no estimate (null);
  * the commission basis — service.basis_for / service.basis_of (config commissions.basis:
    pre-GST under basic_pro_rata), exactly what the ledger records for a payment of that
    value;
  * the rate — the associate's personal_sale rate on the agreement date (today until
    the agreement is signed): rates.resolve, the ledger's own lookup — their individual
    rate when an Admin set one (BE-12), else the confirmed schedule in force then;
  * the amount — service.commission_of.

With no confirmed schedule the rate and amount are null (pending_reason
`placeholder_rates`); a custom-quoted proposal has no basis (`basis_unavailable`) —
the same reasons the ledger records. Only the customer's owner earns a personal sale,
so only the owner is shown one.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.commissions import config, periods, rates, service
from app.commissions.serialize import pct, s
from app.memory.models import Associate, CommissionRole, CustomerOnboarding, OnboardingStatus


def _indicative(db: Session, onboarding: CustomerOnboarding) -> Optional[tuple[Decimal, dict]]:
    """(final price, its {"basic", "final"}) from the latest list-priced indicative
    estimate, or None."""
    from app.onboarding import estimate as indicative

    for record in indicative.list_for(db, onboarding):
        result = record.result or {}
        packages = result.get("packages") or {}
        if result.get("price_status") != "list_price" or not packages:
            continue
        chosen = [p for p in (result.get("chosen_packages") or list(packages)) if p in packages]
        if not chosen:
            continue
        rule = config.estimate()["indicative_package_when_several"]
        if len(chosen) > 1 and rule in chosen:
            chosen = [rule]
        try:
            package = min(chosen, key=lambda p: Decimal(str(packages[p]["final"])))
            return Decimal(str(packages[package]["final"])), packages[package]
        except (KeyError, TypeError, ArithmeticError):
            continue
    return None


def estimated_commission(db: Session, onboarding: CustomerOnboarding,
                         rate_cache: Optional[dict] = None) -> Optional[dict]:
    """{amount, rate, rate_source, basis, value_used, basis_amount, pending_reason} or
    None. Writes nothing. `rate_cache` (rates.resolve's cache) saves queries across a
    page; open app/onboarding/preload.py's `preloaded` around a page as well."""
    from app.payments import service as payments

    if onboarding.status == OnboardingStatus.closed:
        return None
    owner = db.get(Associate, onboarding.owner_associate_id)
    if not service.earns(owner):
        return None
    value = payments.accepted_total(db, onboarding)
    if value is not None:
        source, basis_amount = "accepted_proposal", service.basis_for(db, onboarding, value)
    else:
        found = _indicative(db, onboarding)
        if found is None:
            return None
        value, pricing = found
        source, basis_amount = "indicative_estimate", service.basis_of(value, pricing)

    day = service.agreement_date(db, onboarding) or periods.today_ist()
    resolved = rates.resolve(db, owner, CommissionRole.personal_sale, day,
                             cache=rate_cache if rate_cache is not None else {})
    percent = resolved.percent if resolved is not None else None
    amount = None
    if percent is not None and basis_amount is not None:
        amount = service.commission_of(basis_amount, percent)
    reason = ("basis_unavailable" if basis_amount is None else
              "placeholder_rates" if percent is None else None)
    return {"amount": s(amount), "rate": pct(percent), "rate_source": resolved.source if resolved else None,
            "basis": source, "value_used": s(value),
            "basis_amount": s(basis_amount), "pending_reason": reason}
