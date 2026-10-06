"""Emails to associates about their OWN customers (NOTIFY_ASSOCIATES, placeholder A1):

  * the formal proposal is approved and ready to hand over;
  * an execution stage was sent back, with the reviewer's note;
  * a payment was verified, with the receipt link.

Sent after the commit, never raising. Skipped cleanly when NOTIFY_ASSOCIATES is off or
the associate has no email. Each email names only that customer and links to that
customer's page in the associate's portal (FRONTEND_BASE_URL + /associates/portal/... or
/preferred/portal/... by associate type); the payment email also carries the receipt's
own public link.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import Associate, AssociateType, Customer, CustomerOnboarding

logger = logging.getLogger(__name__)


def _target(db: Session, onboarding: CustomerOnboarding) -> Optional[tuple[str, str, str]]:
    """(email, customer name, customer page URL in the associate's portal)."""
    from app.config import settings
    if not settings.notify_associates:
        return None
    associate = db.get(Associate, onboarding.owner_associate_id)
    if associate is None or not (associate.email or "").strip():
        return None
    customer = db.get(Customer, onboarding.customer_id)
    section = "preferred" if associate.associate_type == AssociateType.preferred_associate else "associates"
    base = (settings.frontend_base_url or "").rstrip("/")
    url = f"{base}/{section}/portal/customers/{onboarding.customer_id}"
    return associate.email.strip(), (customer.name if customer and customer.name else "your customer"), url


def _queue(db: Session, event: str, to: str, subject: str, lines: list[str], link_label=None, link_url=None) -> None:
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module

    def send():
        notifier_module.notifier.send_associate_email(to=to, subject=subject, lines=lines, link_label=link_label,
                                                      link_url=link_url, event_type=event)
    MemoryManager(db).notify_after_commit(send, event)


def proposal_ready(db: Session, lead_id: int) -> None:
    try:
        onboarding = db.execute(select(CustomerOnboarding).where(
            CustomerOnboarding.lead_id == lead_id)).scalar_one_or_none()
        target = onboarding and _target(db, onboarding)
        if not target:
            return
        to, name, url = target
        _queue(db, "associate_proposal_ready", to, f"Proposal ready for {name}",
               [f"Grid Nova has approved the formal proposal for {name}.",
                "Open the customer's page in the Grid Nova portal to hand it over."],
               "Open customer", url)
    except Exception:
        logger.exception("associate proposal-ready email not queued")


def stage_rejected(db: Session, onboarding: CustomerOnboarding, submission) -> None:
    from app.onboarding.progress import LABELS
    try:
        target = _target(db, onboarding)
        if not target:
            return
        to, name, url = target
        label = LABELS[submission.stage]
        _queue(db, "associate_stage_rejected", to, f"Please resubmit: {label} for {name}",
               [f"Grid Nova checked \"{label}\" for {name} and sent it back.",
                f"Note from Grid Nova: {submission.review_note}",
                "Open the customer's page in the Grid Nova portal, fix what is noted and submit it again."],
               "Open customer", url)
    except Exception:
        logger.exception("associate stage-rejected email not queued")


def payment_verified(db: Session, payment, receipt_url: Optional[str]) -> None:
    """After the receipt is rendered. The link is included only when the receipt is
    one the customer may see (not held for placeholder content)."""
    from app.payments.service import s
    try:
        onboarding = db.get(CustomerOnboarding, payment.onboarding_id) if payment.onboarding_id is not None else None
        target = onboarding and _target(db, onboarding)
        if not target:
            return
        to, name, url = target
        lines = [f"Grid Nova has confirmed the payment of INR {s(payment.amount)} from {name}."]
        if receipt_url:
            lines.append("The customer's receipt is available at the link below.")
        lines.append(f"Customer page: {url}")
        _queue(db, "associate_payment_verified", to, f"Payment confirmed for {name}", lines,
               "Download the receipt" if receipt_url else None, receipt_url)
    except Exception:
        logger.exception("associate payment-verified email not queued")
