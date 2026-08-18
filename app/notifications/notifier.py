"""
Pluggable notification layer.

`Notifier` is the abstract interface — a single `notify(event_type, payload)` method.
Concrete implementations:
  - LogNotifier: logs at WARNING (safe, zero-dependency default).
  - SlackWebhookNotifier: POSTs to SLACK_WEBHOOK_URL; never raises if the POST fails.

The active notifier is chosen by whether SLACK_WEBHOOK_URL is set. Named convenience
methods (proposal_approved, meeting_booked, …) are kept on the base class for existing
callers and route through notify(), so nothing that already calls the notifier breaks.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def notify(self, event_type: str, payload: dict) -> None:
        """Deliver a notification. Implementations must never raise to the caller."""
        raise NotImplementedError

    # ── Named convenience notifications (existing surface) ───────────────────────
    def proposal_ready_for_review(self, proposal_id: int, lead_id: int, employee_id: str) -> None:
        self.notify("proposal_ready_for_review",
                    {"proposal_id": proposal_id, "lead_id": lead_id, "assigned_to": employee_id})

    def manager_signoff_required(self, proposal_id: int, lead_id: int, total_value: float) -> None:
        self.notify("manager_signoff_required",
                    {"proposal_id": proposal_id, "lead_id": lead_id, "total_value": total_value})

    def proposal_approved(self, proposal_id: int, lead_id: int) -> None:
        self.notify("proposal_approved", {"proposal_id": proposal_id, "lead_id": lead_id})

    def proposal_sent(self, proposal_id: int, customer_email: str) -> None:
        self.notify("proposal_sent", {"proposal_id": proposal_id, "customer_email": customer_email})

    def director_capacity_alert(self, lead_id: int, capacity_kw: float) -> None:
        self.notify("director_capacity_alert", {"lead_id": lead_id, "capacity_kw": capacity_kw})

    def meeting_booked(self, meeting_id: int, lead_id: int, employee_id: str, scheduled_at: str) -> None:
        self.notify("meeting_booked",
                    {"meeting_id": meeting_id, "lead_id": lead_id,
                     "employee_id": employee_id, "scheduled_at": scheduled_at})


class LogNotifier(Notifier):
    """Default notifier — structured WARNING-level log line. No external dependencies."""

    def notify(self, event_type: str, payload: dict) -> None:
        logger.warning("NOTIFICATION: %s", event_type, extra={"event_type": event_type, **payload})


class SlackWebhookNotifier(Notifier):
    """Posts a formatted message to a Slack incoming webhook. Never breaks the caller:
    a blank URL is a silent no-op, and a failed POST is logged, not raised."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    def notify(self, event_type: str, payload: dict) -> None:
        # Always log too, so notifications are observable regardless of Slack delivery.
        logger.warning("NOTIFICATION: %s", event_type, extra={"event_type": event_type, **payload})
        if not self._webhook_url:
            return
        try:
            import httpx
            fields = "\n".join(f"• {k}: {v}" for k, v in payload.items())
            text = f"*Grid Nova — {event_type}*\n{fields}"
            httpx.post(self._webhook_url, json={"text": text}, timeout=5)
        except Exception as exc:  # never break the calling code path on a delivery failure
            logger.warning("Slack notification delivery failed for %s: %s", event_type, exc)


def get_notifier() -> Notifier:
    """Select the active notifier based on configuration."""
    from app.config import settings
    if settings.slack_webhook_url:
        return SlackWebhookNotifier(settings.slack_webhook_url)
    return LogNotifier()


notifier: Notifier = get_notifier()
