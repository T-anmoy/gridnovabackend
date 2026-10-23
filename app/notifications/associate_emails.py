"""Emails to associates about their OWN customers (NOTIFY_ASSOCIATES, placeholder A1):

  * the formal proposal is approved and ready to hand over;
  * an execution stage was sent back, with the reviewer's note;
  * a payment was verified, with the receipt link;
  * an Engineer reviewed the customer's site survey (S1), with what to do next;
  * BE-12: an Admin changed the associate's own commission rate (the rates and the date
    only — never the reason, which is internal).

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
    """(email, customer name, customer page URL in the associate's portal). The
    portal's customer page is keyed by the ONBOARDING id
    (app/(site)/[lang]/associates/portal/customers/[id] reads it as onboardingId)."""
    from app.config import settings
    if not settings.notify_associates:
        return None
    associate = db.get(Associate, onboarding.owner_associate_id)
    if associate is None or not (associate.email or "").strip():
        return None
    customer = db.get(Customer, onboarding.customer_id)
    section = "preferred" if associate.associate_type == AssociateType.preferred_associate else "associates"
    base = (settings.frontend_base_url or "").rstrip("/")
    url = f"{base}/{section}/portal/customers/{onboarding.id}"
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


_SURVEY_OUTCOME_LINES = {
    "feasible": ["An Engineer reviewed the site survey for {name}: the installation is feasible."],
    "feasible_with_conditions": ["An Engineer reviewed the site survey for {name}: the installation is feasible "
                                 "with conditions.",
                                 "The conditions are listed on the customer's page."],
    "revisit_needed": ["An Engineer reviewed the site survey for {name} and needs parts of it checked again "
                       "on site.",
                       "The customer's page lists what to revisit. Update the survey and have it signed again."],
    "not_feasible": ["An Engineer reviewed the site survey for {name}: the installation is not feasible.",
                     "The Engineer's notes are on the customer's page."],
}


def survey_reviewed(db: Session, onboarding: CustomerOnboarding, outcome) -> None:
    try:
        target = _target(db, onboarding)
        if not target:
            return
        to, name, url = target
        value = outcome.value if hasattr(outcome, "value") else str(outcome)
        _queue(db, "associate_survey_reviewed", to, f"Site survey reviewed for {name}",
               [line.format(name=name) for line in _SURVEY_OUTCOME_LINES[value]], "Open customer", url)
    except Exception:
        logger.exception("associate survey-reviewed email not queued")


def _percent_text(value) -> str:
    text = format(value.normalize(), "f") if value is not None else ""
    return f"{text}%"


def commission_rate_changed(db: Session, associate: Associate, before, after, effective_from) -> None:
    """BE-12: "Your commission rate changes from X% to Y% from <date>." `before` /
    `after` are Decimals or None (no confirmed rate). Links to the associate's own
    commissions page."""
    from app.config import settings
    try:
        if not settings.notify_associates or not (associate.email or "").strip():
            return
        when = f"{effective_from.day} {effective_from.strftime('%B %Y')}"
        if before is not None and after is not None:
            line = f"Your commission rate changes from {_percent_text(before)} to {_percent_text(after)} from {when}."
        elif after is not None:
            line = f"Your commission rate is {_percent_text(after)} from {when}."
        else:
            line = f"Your commission rate changes from {when}; Grid Nova will publish the new rate in the portal."
        section = "preferred" if associate.associate_type == AssociateType.preferred_associate else "associates"
        url = f"{(settings.frontend_base_url or '').rstrip('/')}/{section}/portal/earnings"
        _queue(db, "associate_commission_rate_changed", associate.email.strip(), "Your commission rate is changing",
               [line, "The rate that applies to a customer is the one in force on the date their agreement is "
                      "signed. Commission already recorded does not change."],
               "Open your earnings", url)
    except Exception:
        logger.exception("associate commission-rate email not queued")
