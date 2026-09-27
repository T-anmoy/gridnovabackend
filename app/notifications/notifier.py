"""
Pluggable notification layer.

`Notifier` is the abstract interface — a single `notify(event_type, payload)` method.
Concrete implementations:
  - LogNotifier: logs at WARNING (safe, zero-dependency default).
  - SlackWebhookNotifier: POSTs to SLACK_WEBHOOK_URL; never raises if the POST fails.
  - EmailNotifier: sends email through Resend's REST API (RESEND_API_KEY), wrapping
    whichever of the two above is active so they keep running unchanged.

The active notifier is chosen by whether SLACK_WEBHOOK_URL is set (and wrapped in
EmailNotifier when RESEND_API_KEY and NOTIFY_FROM_EMAIL are). Named convenience
methods (proposal_approved, meeting_booked, …) are kept on the base class for existing
callers and route through notify(), so nothing that already calls the notifier breaks.
"""
from __future__ import annotations

import html
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

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

    def proposal_sent(self, proposal_id: int, customer_email: str, accept_path: str = "") -> None:
        payload = {"proposal_id": proposal_id, "customer_email": customer_email}
        if accept_path:
            payload["accept_path"] = accept_path
        self.notify("proposal_sent", payload)

    def director_capacity_alert(self, lead_id: int, capacity_kw: float) -> None:
        self.notify("director_capacity_alert", {"lead_id": lead_id, "capacity_kw": capacity_kw})

    def meeting_booked(self, meeting_id: int, lead_id: int, employee_id: str, scheduled_at: str) -> None:
        self.notify("meeting_booked",
                    {"meeting_id": meeting_id, "lead_id": lead_id,
                     "employee_id": employee_id, "scheduled_at": scheduled_at})

    def new_lead(self, lead_id: int, source: str, customer_name: Optional[str], created_by: str) -> None:
        self.notify("new_lead", {"lead_id": lead_id, "source": source,
                                 "customer_name": customer_name, "created_by": created_by})

    def lead_qualified(self, lead_id: int, source: str, customer_name: Optional[str]) -> None:
        self.notify("lead_qualified", {"lead_id": lead_id, "source": source, "customer_name": customer_name})

    def customer_accepted(self, proposal_id: int, lead_id: int, version: int,
                          customer_name: Optional[str]) -> None:
        self.notify("customer_accepted", {"proposal_id": proposal_id, "lead_id": lead_id,
                                          "version": version, "customer_name": customer_name})

    def stale_lead_digest(self, leads: list[dict]) -> None:
        """One summary per stale-lead run. Each item: lead_id, name, source, stage,
        days_quiet, assigned_employee_id, threshold."""
        self.notify("stale_lead_digest", {"count": len(leads), "leads": leads})

    def send_proposal_to_customer(
        self,
        proposal_id: int,
        version: int,
        customer_email: Optional[str],
        customer_name: Optional[str],
        accept_url: Optional[str],
        pdf_url: Optional[str],
    ) -> tuple[bool, Optional[str]]:
        """Email the customer their proposal. Returns (sent, skipped_reason). Only the
        email notifier can; every other notifier reports why it did not."""
        return False, "email_not_configured"


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


RESEND_API_URL = "https://api.resend.com/emails"

# Estimate-form and contact-form leads are deliberately absent: the website itself
# emails info@gridnova.in for both, so a second staff email would be noise. (The
# contact form's own email arrives with the frontend change that also adds its
# backend mirror.)
STAFF_EMAIL_EVENTS = ("new_lead", "lead_qualified", "meeting_booked",
                      "manager_signoff_required", "customer_accepted", "stale_lead_digest",
                      "formal_proposal_requested")
_NO_STAFF_EMAIL_SOURCES = ("website_estimate_form", "website_contact")

_SOURCE_LABELS = {
    "website_chat": "website chat",
    "website_estimate_form": "website estimate form",
    "website_booking": "website booking",
    "website_contact": "website contact form",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "cold_call": "phone call",
    "referral": "referral",
    "other": "other",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _name(payload: dict) -> str:
    return payload.get("customer_name") or "(name not yet known)"


def _staff_message(event_type: str, payload: dict, base_url: str) -> Optional[tuple[str, list[str], str, str]]:
    """(subject, lines, link_label, link_url) for a staff email, or None. Lines are
    plain text; the caller escapes them for HTML."""
    lead_url = f"{base_url}/dashboard/leads/{payload.get('lead_id')}"
    source = _SOURCE_LABELS.get(str(payload.get("source")), str(payload.get("source")))
    if event_type == "new_lead":
        return (f"New lead ({source}): {_name(payload)}",
                [f"A new lead came in from {source}.", f"Customer: {_name(payload)}",
                 f"Lead #{payload.get('lead_id')}, entered by {payload.get('created_by')}."],
                "Open the lead", lead_url)
    if event_type == "lead_qualified":
        return (f"Lead qualified via {source}: {_name(payload)}",
                [f"Lead #{payload.get('lead_id')} has every detail needed for a proposal draft.",
                 f"Customer: {_name(payload)}"],
                "Open the lead", lead_url)
    if event_type == "meeting_booked":
        return (f"Meeting booked: {payload.get('scheduled_at')}",
                [f"A meeting was booked for {payload.get('scheduled_at')}.",
                 f"Lead #{payload.get('lead_id')}, with {payload.get('employee_id')}."],
                "Open meetings", f"{base_url}/dashboard/meetings")
    if event_type == "formal_proposal_requested":
        who = _name(payload)
        intro = (f"{payload.get('requested_by')} asked Grid Nova to prepare the formal proposal for {who} "
                 f"(associate-sourced customer #{payload.get('onboarding_id')}, project "
                 f"{payload.get('project_reference_id')}).")
        if payload.get("missing"):
            missing = "; ".join(payload["missing"])
            return (f"Formal proposal blocked: {who} — {missing}",
                    [intro, f"It cannot be drafted yet: {missing}.",
                     "Add the missing details, then prepare the proposal."],
                    "Open the lead", lead_url)
        return (f"Formal proposal requested: {who}",
                [intro, "The customer record has everything a formal proposal needs."],
                "Open the lead", lead_url)
    proposal_url = f"{base_url}/dashboard/proposals/{payload.get('proposal_id')}"
    if event_type == "manager_signoff_required":
        value = payload.get("total_value")
        value_line = f"Proposal value: INR {value:,.0f}." if value else "Proposal value: not yet set."
        return (f"Manager sign-off needed: proposal #{payload.get('proposal_id')}",
                [f"Proposal #{payload.get('proposal_id')} (lead #{payload.get('lead_id')}) "
                 "requires manager sign-off before it can be sent.", value_line],
                "Open the proposal", proposal_url)
    if event_type == "customer_accepted":
        return (f"Proposal accepted: {_name(payload)}",
                [f"{_name(payload)} accepted proposal v{payload.get('version')} online.",
                 f"Proposal #{payload.get('proposal_id')}, lead #{payload.get('lead_id')}."],
                "Open the proposal", proposal_url)
    return None


_THRESHOLD_LABELS = {"lead_stale_48h": "48 hours", "lead_stale_5d": "5 days"}


def _render_stale_digest(leads: list[dict], base_url: str) -> tuple[str, str, str]:
    """(subject, html, text) for the stale-lead digest. Every value is escaped."""
    subject = f"{len(leads)} lead{'s' if len(leads) != 1 else ''} with no activity"
    intro = "These open leads have had no activity past a follow-up threshold:"
    rows_html, rows_text = [], []
    for item in leads:
        name = item.get("name") or "(name not yet known)"
        source = _SOURCE_LABELS.get(str(item.get("source")), str(item.get("source")))
        assignee = item.get("assigned_employee_id") or "unassigned"
        threshold = _THRESHOLD_LABELS.get(str(item.get("threshold")), str(item.get("threshold")))
        url = f"{base_url}/dashboard/leads/{item.get('lead_id')}"
        detail = (f"{source} · stage {item.get('stage')} · quiet {item.get('days_quiet')} days "
                  f"(past {threshold}) · {assignee}")
        rows_html.append(f'<li><a href="{_esc(url)}">{_esc(name)}</a> — {_esc(detail)}</li>')
        rows_text.append(f"- {name} — {detail}\n  {url}")
    html_body = f"<div><p>{_esc(intro)}</p><ul>{''.join(rows_html)}</ul></div>"
    text_body = intro + "\n\n" + "\n".join(rows_text)
    return subject, html_body, text_body


def _render(lines: list[str], link_label: Optional[str] = None, link_url: Optional[str] = None) -> tuple[str, str]:
    """(html, text) bodies. Every interpolated value is escaped — customer names and
    anything else a visitor typed end up here."""
    body = "".join(f"<p>{_esc(line)}</p>" for line in lines)
    text = "\n\n".join(lines)
    if link_url:
        body += f'<p><a href="{_esc(link_url)}">{_esc(link_label)}</a></p>'
        text += f"\n\n{link_label}: {link_url}"
    return f"<div>{body}</div>", text


def _report_failure(event_type: str, detail: str, exc: Optional[BaseException] = None) -> None:
    logger.warning("Email notification failed for %s: %s", event_type, detail)
    try:
        import sentry_sdk
        if sentry_sdk.is_initialized():
            if exc is not None:
                sentry_sdk.capture_exception(exc)
            else:
                sentry_sdk.capture_message(f"Email notification failed for {event_type}: {detail}", level="warning")
    except Exception:  # Sentry must never be the thing that breaks the caller
        pass


class EmailNotifier(Notifier):
    """Emails via Resend's REST API, on top of the existing log/Slack notifier (which
    still runs for every event). Staff emails need STAFF_NOTIFY_EMAILS; the customer
    proposal email needs only the key and sender. Never raises into the caller."""

    def __init__(self, api_key: str, from_email: str, staff_emails: list[str],
                 frontend_base_url: str, inner: Notifier) -> None:
        self._api_key = api_key
        self._from = from_email
        self._staff = staff_emails
        self._base_url = frontend_base_url.rstrip("/")
        self._inner = inner

    def notify(self, event_type: str, payload: dict) -> None:
        self._inner.notify(event_type, payload)
        if event_type == "onboarding_overdue":
            self._send_onboarding_overdue(payload)
            return
        if not self._staff or event_type not in STAFF_EMAIL_EVENTS:
            return
        if payload.get("source") in _NO_STAFF_EMAIL_SOURCES:
            return
        try:
            if event_type == "stale_lead_digest":
                if not payload.get("leads"):
                    return
                subject, html_body, text_body = _render_stale_digest(payload["leads"], self._base_url)
            else:
                message = _staff_message(event_type, payload, self._base_url)
                if message is None:
                    return
                subject, lines, link_label, link_url = message
                html_body, text_body = _render(lines, link_label, link_url)
        except Exception as exc:
            _report_failure(event_type, f"could not build the email: {exc}", exc)
            return
        self._send(self._staff, subject, html_body, text_body, event_type)

    def _send_onboarding_overdue(self, payload: dict) -> None:
        """An associate-sourced customer's verification call is overdue: email the
        Preferred Associates the job escalated to, or staff at the Admin step."""
        if payload.get("escalated_to") == "admin":
            to = self._staff
        else:
            to = [r["email"] for r in payload.get("recipients", []) if r.get("email")]
        if not to:
            return
        try:
            lines = [f"The verification call for associate-sourced customer #{payload.get('onboarding_id')} "
                     f"is overdue by {payload.get('overdue_hours')} hours.",
                     f"Score band: {payload.get('band')} (score {payload.get('score')}). "
                     f"It was due at {payload.get('response_due_at')}.",
                     "Please make sure Grid Nova staff contact the customer."]
            html_body, text_body = _render(lines)
        except Exception as exc:
            _report_failure("onboarding_overdue", f"could not build the email: {exc}", exc)
            return
        self._send(to, f"Overdue: verification call for customer #{payload.get('onboarding_id')}",
                   html_body, text_body, "onboarding_overdue")

    def send_proposal_to_customer(self, proposal_id, version, customer_email, customer_name,
                                  accept_url, pdf_url) -> tuple[bool, Optional[str]]:
        from app.memory.memory_manager import is_placeholder_email

        if not customer_email:
            return False, "no_customer_email"
        if is_placeholder_email(customer_email):
            return False, "placeholder_customer_email"
        if not accept_url:
            return False, "no_accept_link"
        lines = [
            f"Dear {customer_name or 'Customer'},",
            f"Your proposal from Grid Nova Energy (version {version}) is ready to review.",
            "You can read it and accept it online using the link below.",
        ]
        if pdf_url:
            lines.append(f"A PDF copy is available here: {pdf_url}")
        lines.append("If you have any questions, please contact your Grid Nova representative.")
        html_body, text_body = _render(lines, "Review and accept your proposal", accept_url)
        sent = self._send([customer_email], "Your proposal from Grid Nova Energy",
                          html_body, text_body, "proposal_sent_customer_email")
        return (True, None) if sent else (False, "send_failed")

    def _send(self, to: list[str], subject: str, html_body: str, text_body: str, event_type: str) -> bool:
        try:
            response = httpx.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"from": self._from, "to": to, "subject": subject, "html": html_body, "text": text_body},
                timeout=10,
            )
        except Exception as exc:
            _report_failure(event_type, f"request error: {exc}", exc)
            return False
        if response.status_code >= 300:
            _report_failure(event_type, f"Resend returned HTTP {response.status_code}")
            return False
        logger.info("Email notification sent for %s", event_type)
        return True


def get_notifier() -> Notifier:
    """Select the active notifier based on configuration. Email wraps whichever of
    Slack/log would otherwise be active, so those keep behaving exactly as before."""
    from app.config import settings
    inner: Notifier
    if settings.slack_webhook_url:
        inner = SlackWebhookNotifier(settings.slack_webhook_url)
    else:
        inner = LogNotifier()
    if settings.resend_api_key and settings.notify_from_email:
        return EmailNotifier(
            api_key=settings.resend_api_key,
            from_email=settings.notify_from_email,
            staff_emails=settings.staff_notify_email_list,
            frontend_base_url=settings.frontend_base_url,
            inner=inner,
        )
    return inner


notifier: Notifier = get_notifier()
