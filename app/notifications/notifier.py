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
import threading
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

    def send_receipt_to_customer(self, customer_email: Optional[str], customer_name: Optional[str],
                                 receipt_number: str, receipt_url: str) -> tuple[bool, Optional[str]]:
        """Email the customer a link to their payment receipt. (sent, reason-if-not)."""
        return False, "email_not_configured"

    def send_handover_to_customer(self, customer_email: Optional[str], customer_name: Optional[str],
                                  project_reference: str, pack_url: str) -> tuple[bool, Optional[str]]:
        """Email the customer a link to their handover pack and warranty (BE-5)."""
        return False, "email_not_configured"

    def send_associate_email(self, to: str, subject: str, lines: list[str], link_label: Optional[str] = None,
                             link_url: Optional[str] = None, event_type: str = "associate_email") -> bool:
        """One plain email to an associate (app/notifications/associate_emails.py)."""
        self.notify(event_type, {"to": to, "subject": subject})
        return False

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
RESEND_TIMEOUT_SECONDS = 10

# One HTTP client per process for Resend: its connection pool (and TLS session) is
# reused across emails instead of a new connection per message.
_http_lock = threading.Lock()
_http: Optional[httpx.Client] = None


def http_client() -> httpx.Client:
    global _http
    with _http_lock:
        if _http is None:
            _http = httpx.Client(timeout=RESEND_TIMEOUT_SECONDS)
        return _http

# Estimate-form and contact-form leads are deliberately absent: the website itself
# emails info@gridnova.in for both, so a second staff email would be noise. (The
# contact form's own email arrives with the frontend change that also adds its
# backend mirror.)
STAFF_EMAIL_EVENTS = ("new_lead", "lead_qualified", "meeting_booked",
                      "manager_signoff_required", "customer_accepted", "stale_lead_digest",
                      "formal_proposal_requested", "stage_submitted", "execution_stuck", "dcr_mismatch",
                      "survey_review_overdue",
                      "catalogue_unmatched",
                      "payment_recorded", "payment_dishonoured", "receipt_blocked", "payments_attention",
                      "commission_queries_overdue", "handover_blocked", "service_request_raised",
                      "service_requests_overdue", "associate_application_submitted")
# Events for one team rather than all staff: sent to that team's own recipients
# (ACCOUNTS_NOTIFY_EMAILS / OPERATIONS_NOTIFY_EMAILS), else to STAFF_NOTIFY_EMAILS.
EVENT_AUDIENCE = {"stage_submitted": ("operations",), "execution_stuck": ("operations",),
                  "dcr_mismatch": ("operations", "staff"), "catalogue_unmatched": ("operations",),
                  "payment_recorded": ("accounts",),
                  "payment_dishonoured": ("accounts", "staff"), "receipt_blocked": ("accounts",),
                  "payments_attention": ("accounts",), "commission_queries_overdue": ("accounts",),
                  "handover_blocked": ("operations",), "service_request_raised": ("operations",),
                  "service_requests_overdue": ("operations",)}
_HANDOVER_REASONS = {"held": "its text still contains placeholders (O&M terms or company details not yet supplied)",
                     "no_customer_email": "the customer has no email address on file",
                     "render_failed": "the handover PDF could not be generated",
                     "no_public_api_base_url": "PUBLIC_API_BASE_URL is not set"}
_RECEIPT_REASONS = {"held": "it would contain placeholder text (company or wording not yet supplied)",
                    "no_customer_email": "the customer has no email address on file",
                    "render_failed": "the receipt PDF could not be generated",
                    "no_public_api_base_url": "PUBLIC_API_BASE_URL is not set"}
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
    lead_url = (f"{base_url}/dashboard/onboardings/{payload['onboarding_id']}" if payload.get("onboarding_id")
                else f"{base_url}/dashboard/leads/{payload.get('lead_id')}")
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
    if event_type == "stage_submitted":
        who = payload.get("customer_name") or "a customer"
        return (f"Stage to check: {payload.get('stage_label')} — {who}",
                [f"{payload.get('submitted_by')} submitted \"{payload.get('stage_label')}\" for {who} "
                 f"(associate-sourced customer #{payload.get('onboarding_id')}).",
                 "Check the evidence, then verify it or send it back with a note."],
                "Open the customer", lead_url)
    if event_type == "dcr_mismatch":
        who = payload.get("customer_name") or "a customer"
        return (f"DCR mismatch: {who} ({payload.get('project_reference_id')})",
                [f"A non-DCR module was registered on {who}'s PM Surya Ghar project "
                 f"(associate-sourced customer #{payload.get('onboarding_id')}).",
                 "The subsidy cannot be claimed until the module is corrected or an Admin records why it may proceed."],
                "Open the customer", lead_url)
    if event_type == "catalogue_unmatched":
        who = payload.get("customer_name") or "a customer"
        count = payload.get("count") or 1
        return (f"Units to match: {who} ({payload.get('project_reference_id')})",
                [f"{count} installed unit(s) on {who}'s project (associate-sourced customer "
                 f"#{payload.get('onboarding_id')}) were registered with the brand and model as printed, because the "
                 "catalogue has no matching item.",
                 "Match each unit to a catalogue item (or add the item first). The serial register cannot be "
                 "verified until every unit is matched."],
                "Open the customer", lead_url)
    if event_type in ("payment_recorded", "payment_dishonoured"):
        who = payload.get("customer_name") or "a customer"
        amount = f"INR {payload.get('amount')} by {str(payload.get('mode', '')).replace('_', ' ').upper()}"
        if event_type == "payment_recorded":
            return (f"Payment to verify: {who} — {amount}",
                    [f"{payload.get('recorded_by')} recorded a payment of {amount} for {who} "
                     f"(associate-sourced customer #{payload.get('onboarding_id')}).",
                     "Confirm it has reached Grid Nova's bank, then verify it."],
                    "Open the customer", lead_url)
        return (f"Payment dishonoured: {who} — {amount}",
                [f"A payment of {amount} for {who} (customer #{payload.get('onboarding_id')}) was dishonoured.",
                 f"Reason: {payload.get('reason')}"],
                "Open the customer", lead_url)
    if event_type == "receipt_blocked":
        reason = _RECEIPT_REASONS.get(str(payload.get("reason")), str(payload.get("reason")))
        return (f"Receipt {payload.get('receipt_number')} not sent to the customer",
                [f"Receipt {payload.get('receipt_number')} (customer #{payload.get('onboarding_id')}) was not "
                 f"emailed because {reason}.", "The payment itself is verified."],
                "Open the customer", lead_url)
    if event_type == "handover_blocked":
        reason = _HANDOVER_REASONS.get(str(payload.get("reason")), str(payload.get("reason")))
        who = payload.get("customer_name") or "a customer"
        return (f"Handover pack not sent: {who} ({payload.get('project_reference_id')})",
                [f"The handover pack for {who} (customer #{payload.get('onboarding_id')}) was not emailed because "
                 f"{reason}.", "The handover itself is recorded."], "Open the customer", lead_url)
    if event_type == "service_request_raised":
        who = payload.get("customer_name") or "a customer"
        warranty = {True: "within warranty", False: "outside warranty"}.get(payload.get("within_warranty"),
                                                                           "warranty not known")
        return (f"Service request: {who} ({payload.get('project_reference_id')})",
                [f"A service request ({str(payload.get('category')).replace('_', ' ')}) was raised for {who} by "
                 f"{payload.get('raised_via')} ({warranty}).", f"Request #{payload.get('request_id')}."],
                "Open the customer", lead_url)
    if event_type == "service_requests_overdue":
        rows = payload.get("requests") or []
        return (f"{len(rows)} service request{'s' if len(rows) != 1 else ''} past the target time",
                [f"Request #{r.get('request_id')} — {r.get('customer_name') or 'a customer'} "
                 f"({r.get('project_reference_id')}), {r.get('hours_overdue')} hours past" for r in rows],
                "Open the dashboard", f"{base_url}/dashboard")
    if event_type == "associate_application_submitted":
        kind = str(payload.get("application_type", "")).replace("_", " ")
        return (f"New {kind} application: {payload.get('name')}",
                [f"{payload.get('name')} applied to join the network as a {kind} ({payload.get('state')}).",
                 f"Application #{payload.get('application_id')} is waiting for review."],
                "Open applications", f"{base_url}/dashboard/associate-applications")
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


def _render_survey_review_overdue(payload: dict, base_url: str) -> tuple[str, str, str]:
    """(subject, html, text) for site surveys waiting past the review target."""
    surveys = payload.get("surveys") or []
    subject = f"{len(surveys)} site survey{'s' if len(surveys) != 1 else ''} waiting for an Engineer's review"
    intro = (f"These site surveys have waited longer than {payload.get('review_target_hours'):g} hours for an "
             "Engineer's feasibility review:")
    rows_html, rows_text = [], []
    for item in surveys:
        url = f"{base_url}/dashboard/onboardings/{item.get('onboarding_id')}"
        name = item.get("customer_name") or f"Customer #{item.get('onboarding_id')}"
        detail = f"{item.get('client_code')} · waiting {item.get('hours_waiting')} hours"
        if item.get("revised_after_review"):
            detail += " · changed after its last review"
        rows_html.append(f'<li><a href="{_esc(url)}">{_esc(name)}</a> — {_esc(detail)}</li>')
        rows_text.append(f"- {name} — {detail}\n  {url}")
    html_body = f"<div><p>{_esc(intro)}</p><ul>{''.join(rows_html)}</ul></div>"
    text_body = intro + "\n\n" + "\n".join(rows_text)
    return subject, html_body, text_body


def _render_execution_stuck(projects: list[dict], base_url: str) -> tuple[str, str, str]:
    """(subject, html, text) for projects that have waited past their stage's threshold."""
    subject = f"{len(projects)} project{'s' if len(projects) != 1 else ''} stuck in execution"
    intro = "These projects have waited longer than expected for their next execution stage:"
    rows_html, rows_text = [], []
    for item in projects:
        url = f"{base_url}/dashboard/onboardings/{item.get('onboarding_id')}"
        name = item.get("customer_name") or f"Customer #{item.get('onboarding_id')}"
        detail = (f"waiting for {item.get('next_stage_label')} · {item.get('days_in_stage')} days "
                  f"(expected within {item.get('stuck_threshold_days')})")
        rows_html.append(f'<li><a href="{_esc(url)}">{_esc(name)}</a> — {_esc(detail)}</li>')
        rows_text.append(f"- {name} — {detail}\n  {url}")
    return subject, f"<div><p>{_esc(intro)}</p><ul>{''.join(rows_html)}</ul></div>", \
        intro + "\n\n" + "\n".join(rows_text)


def _render_payments_attention(payload: dict, base_url: str) -> tuple[str, str, str]:
    overdue, waiting = payload.get("overdue_milestones") or [], payload.get("awaiting_verification") or []
    subject = f"Payments needing attention: {len(overdue)} overdue, {len(waiting)} awaiting verification"
    rows_html, rows_text = [], []
    for item in overdue:
        url = f"{base_url}/dashboard/onboardings/{item.get('onboarding_id')}"
        line = (f"{item.get('customer_name') or 'Customer'} — {item.get('label')} overdue, INR "
                f"{item.get('outstanding')} outstanding")
        rows_html.append(f'<li><a href="{_esc(url)}">{_esc(line)}</a></li>')
        rows_text.append(f"- {line}\n  {url}")
    for item in waiting:
        url = f"{base_url}/dashboard/onboardings/{item.get('onboarding_id')}"
        line = f"Payment #{item.get('payment_id')} — INR {item.get('amount')} by {item.get('mode')}, not yet verified"
        rows_html.append(f'<li><a href="{_esc(url)}">{_esc(line)}</a></li>')
        rows_text.append(f"- {line}\n  {url}")
    return subject, f"<div><ul>{''.join(rows_html)}</ul></div>", "\n".join(rows_text)


def _render_commission_queries_overdue(payload: dict, base_url: str) -> tuple[str, str, str]:
    items = payload.get("queries") or []
    subject = f"Commission queries past their response date: {len(items)}"
    url = f"{base_url}/dashboard/commission-queries"
    rows_html, rows_text = [], []
    for item in items:
        line = (f"Query #{item.get('query_id')} from {item.get('associate_name') or item.get('associate_id')} "
                f"on commission entry #{item.get('entry_id')} — due {item.get('due_at')}")
        rows_html.append(f"<li>{_esc(line)}</li>")
        rows_text.append(f"- {line}")
    intro = "These associate queries have passed the response time in the Commission & Payout Policy."
    return subject, (f"<div><p>{_esc(intro)}</p><ul>{''.join(rows_html)}</ul>"
                     f'<p><a href="{_esc(url)}">Open the query queue</a></p></div>'), \
        intro + "\n\n" + "\n".join(rows_text) + f"\n\nOpen the query queue: {url}"


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
                 frontend_base_url: str, inner: Notifier, role_emails: Optional[dict] = None) -> None:
        self._api_key = api_key
        self._from = from_email
        self._staff = staff_emails
        self._base_url = frontend_base_url.rstrip("/")
        self._inner = inner
        # {"accounts": [...], "operations": [...]} — a team with none falls back to staff.
        self._roles = role_emails or {}

    def _fixed(self, event_type: str) -> list[str]:
        """The configured addresses (shared mailboxes) for an event."""
        audience = EVENT_AUDIENCE.get(event_type)
        if not audience:
            return list(self._staff)
        to: list[str] = []
        for team in audience:
            for email in self._roles.get(team) or self._staff:
                if email not in to:
                    to.append(email)
        return to

    def _recipients(self, event_type: str, payload: Optional[dict] = None) -> list[str]:
        """The fixed addresses plus the active holders of the event's roles
        (config/access_control.yaml alerts; app/notifications/alerts.py)."""
        from app.notifications import alerts

        return alerts.recipients(event_type, payload or {}, self._fixed(event_type))

    def notify(self, event_type: str, payload: dict) -> None:
        self._inner.notify(event_type, payload)
        if event_type == "onboarding_overdue":
            self._send_onboarding_overdue(payload)
            return
        if event_type == "approval_requested":
            self._send_approval_requested(payload)
            return
        if event_type not in STAFF_EMAIL_EVENTS:
            return
        to = self._recipients(event_type, payload)
        if not to or event_type not in STAFF_EMAIL_EVENTS:
            return
        if payload.get("source") in _NO_STAFF_EMAIL_SOURCES:
            return
        try:
            if event_type == "stale_lead_digest":
                if not payload.get("leads"):
                    return
                subject, html_body, text_body = _render_stale_digest(payload["leads"], self._base_url)
            elif event_type == "payments_attention":
                subject, html_body, text_body = _render_payments_attention(payload, self._base_url)
            elif event_type == "execution_stuck":
                if not payload.get("projects"):
                    return
                subject, html_body, text_body = _render_execution_stuck(payload["projects"], self._base_url)
            elif event_type == "survey_review_overdue":
                if not payload.get("surveys"):
                    return
                subject, html_body, text_body = _render_survey_review_overdue(payload, self._base_url)
            elif event_type == "commission_queries_overdue":
                if not payload.get("queries"):
                    return
                subject, html_body, text_body = _render_commission_queries_overdue(payload, self._base_url)
            else:
                message = _staff_message(event_type, payload, self._base_url)
                if message is None:
                    return
                subject, lines, link_label, link_url = message
                html_body, text_body = _render(lines, link_label, link_url)
        except Exception as exc:
            _report_failure(event_type, f"could not build the email: {exc}", exc)
            return
        self._send(to, subject, html_body, text_body, event_type)

    def _send_onboarding_overdue(self, payload: dict) -> None:
        """An associate-sourced customer's verification call is overdue: email the
        Preferred Associates the job escalated to, or staff at the Admin step."""
        staff = payload.get("escalated_to") == "admin"
        if staff:
            to = self._recipients("onboarding_overdue", payload)
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
            # Staff get the dashboard link; Preferred Associates cannot open the dashboard.
            link = f"{self._base_url}/dashboard/onboardings/{payload.get('onboarding_id')}" if staff else None
            html_body, text_body = _render(lines, "Open the customer" if link else None, link)
        except Exception as exc:
            _report_failure("onboarding_overdue", f"could not build the email: {exc}", exc)
            return
        self._send(to, f"Overdue: verification call for customer #{payload.get('onboarding_id')}",
                   html_body, text_body, "onboarding_overdue")

    def _send_approval_requested(self, payload: dict) -> None:
        """A change waits for a second approver: email the people who could approve it."""
        to = [e for e in payload.get("recipients") or [] if e]
        if not to:
            _report_failure("approval_requested", "no eligible approver has an email address")
            return
        lines = [f"{payload.get('requested_by')} asked for: {payload.get('summary')}.",
                 "Nothing changes until someone other than the requester approves it.",
                 f"The request expires at {payload.get('expires_at')}."]
        html_body, text_body = _render(lines, "Review the request", f"{self._base_url}/dashboard/approvals")
        self._send(to, f"Approval needed: {payload.get('summary')}", html_body, text_body, "approval_requested")

    def send_associate_email(self, to, subject, lines, link_label=None, link_url=None,
                             event_type="associate_email") -> bool:
        self._inner.notify(event_type, {"to": to, "subject": subject})
        html_body, text_body = _render(lines, link_label, link_url)
        return self._send([to], subject, html_body, text_body, event_type)

    def send_receipt_to_customer(self, customer_email, customer_name, receipt_number,
                                 receipt_url) -> tuple[bool, Optional[str]]:
        lines = [f"Dear {customer_name or 'Customer'},",
                 f"Thank you for your payment to Grid Nova Energy. Your receipt {receipt_number} is ready.",
                 "You can download it using the link below."]
        html_body, text_body = _render(lines, "Download your receipt", receipt_url)
        sent = self._send([customer_email], f"Payment receipt {receipt_number} — Grid Nova Energy",
                          html_body, text_body, "receipt_customer_email")
        return (True, None) if sent else (False, "send_failed")

    def send_handover_to_customer(self, customer_email, customer_name, project_reference,
                                  pack_url) -> tuple[bool, Optional[str]]:
        lines = [f"Dear {customer_name or 'Customer'},",
                 f"Your solar system ({project_reference}) has been handed over. Your handover pack and warranty "
                 "details are ready.",
                 "You can download them using the link below, and use the same link to raise a service request."]
        html_body, text_body = _render(lines, "Download your handover pack", pack_url)
        sent = self._send([customer_email], f"Your handover pack and warranty — {project_reference} — Grid Nova Energy",
                          html_body, text_body, "handover_customer_email")
        return (True, None) if sent else (False, "send_failed")

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
            response = http_client().post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"from": self._from, "to": to, "subject": subject, "html": html_body, "text": text_body},
                timeout=RESEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            _report_failure(event_type, f"request error: {exc}", exc)
            return False
        if response.status_code >= 300:
            _report_failure(event_type, f"Resend returned HTTP {response.status_code}")
            return False
        logger.info("Email notification sent for %s", event_type)
        return True


def _own_list(raw: str) -> list[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


def fixed_recipient_count(event_type: str) -> int:
    """How many configured shared addresses receive this event (GET /access/alerts)."""
    from app.config import settings

    staff = settings.staff_notify_email_list
    teams = {"accounts": _own_list(settings.accounts_notify_emails),
             "operations": _own_list(settings.operations_notify_emails)}
    if event_type in ("approval_requested",):
        return 0
    audience = EVENT_AUDIENCE.get(event_type)
    if not audience:
        return len(staff)
    return len({e for team in audience for e in (teams.get(team) or staff)})


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
            role_emails={"accounts": _own_list(settings.accounts_notify_emails),
                         "operations": _own_list(settings.operations_notify_emails)},
        )
    return inner


notifier: Notifier = get_notifier()
