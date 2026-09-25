"""Email notifications through Resend's REST API (app/notifications/notifier.py
EmailNotifier), with httpx mocked — nothing leaves the test process."""
from datetime import datetime, timedelta, timezone

import pytest

import app.notifications.notifier as notifier_module
from app.config import settings
from app.memory.models import ProposalStatus
from app.notifications.notifier import (
    RESEND_API_URL, EmailNotifier, LogNotifier, SlackWebhookNotifier, get_notifier,
)
from app.scheduling import google_calendar
from app.security.encryption import encrypt, generate_key
from app.timezones import format_ist_datetime
from tests.conftest import admin_headers, sales_headers

FRONTEND = "https://www.gridnova.in"
STAFF = ["ops@gridnova.in", "sales@gridnova.in"]
DASH = "/api/v1/dashboard"


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture
def sent(monkeypatch):
    """Every email the notifier tries to send, as the JSON body Resend would get."""
    calls = []

    def _post(url, headers=None, json=None, timeout=None):
        assert url == RESEND_API_URL
        assert headers == {"Authorization": "Bearer re_test_key"}
        calls.append(json)
        return _Response(200)

    monkeypatch.setattr(notifier_module.httpx, "post", _post)
    return calls


def _email_notifier(staff=STAFF):
    return EmailNotifier(api_key="re_test_key", from_email="Grid Nova <notifications@gridnova.in>",
                         staff_emails=staff, frontend_base_url=FRONTEND + "/", inner=LogNotifier())


@pytest.fixture
def email_on(monkeypatch):
    """Make the EmailNotifier the active notifier everywhere the app looks it up."""
    def _install(staff=STAFF):
        n = _email_notifier(staff)
        monkeypatch.setattr(notifier_module, "notifier", n)
        monkeypatch.setattr("app.api.routes.dashboard.notifier", n)
        return n
    monkeypatch.setattr(settings, "frontend_base_url", FRONTEND)
    return _install


def _staff(sent):
    return [m for m in sent if m["to"] == STAFF]


def _lead(memory, db_session, email="n@example.com", name="Nisha", **fields):
    customer = memory.get_or_create_customer(email=email, name=name)
    lead = memory.create_lead(customer_id=customer.id)
    base = {"name": name, "email": email, "project_type": "rooftop", "location": "Kolkata", "capacity_kw": 6}
    memory.update_lead_fields(lead_id=lead.id, new_fields={**base, **fields}, performed_by="t")
    db_session.commit()
    return customer.id, lead.id


def _approved(memory, db_session, lead_id, content=None, total_value=150_000):
    p = memory.create_proposal_draft(lead_id=lead_id, content=content or {}, total_value=total_value,
                                     requires_manager_signoff=False, performed_by="emp-1")
    memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                      performed_by="emp-1", approved_by_employee_id="emp-admin-1")
    db_session.commit()
    return p.id


# ── Selection ───────────────────────────────────────────────────────────────────

class TestSelection:
    def test_email_wraps_log_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "resend_api_key", "re_x")
        monkeypatch.setattr(settings, "notify_from_email", "Grid Nova <n@gridnova.in>")
        monkeypatch.setattr(settings, "staff_notify_emails", " a@gridnova.in, ,b@gridnova.in ")
        monkeypatch.setattr(settings, "slack_webhook_url", "")
        n = get_notifier()
        assert isinstance(n, EmailNotifier) and isinstance(n._inner, LogNotifier)
        assert n._staff == ["a@gridnova.in", "b@gridnova.in"]

    def test_email_wraps_slack_when_both_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "resend_api_key", "re_x")
        monkeypatch.setattr(settings, "notify_from_email", "n@gridnova.in")
        monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
        assert isinstance(get_notifier()._inner, SlackWebhookNotifier)

    @pytest.mark.parametrize("key, sender", [("", "n@gridnova.in"), ("re_x", "")])
    def test_unchanged_without_key_and_sender(self, monkeypatch, key, sender):
        monkeypatch.setattr(settings, "resend_api_key", key)
        monkeypatch.setattr(settings, "notify_from_email", sender)
        monkeypatch.setattr(settings, "slack_webhook_url", "")
        assert isinstance(get_notifier(), LogNotifier)

    def test_every_event_still_logs(self, sent, caplog):
        with caplog.at_level("WARNING", logger="app.notifications.notifier"):
            _email_notifier().new_lead(lead_id=1, source="referral", customer_name="A", created_by="x")
        assert any("new_lead" in r.getMessage() for r in caplog.records)
        assert len(sent) == 1


# ── Staff events ────────────────────────────────────────────────────────────────

class TestStaffEvents:
    def test_manual_lead(self, client, sent, email_on):
        email_on()
        r = client.post(f"{DASH}/leads", json={"name": "Ravi", "phone": "9876500000", "source": "referral"},
                        headers=sales_headers())
        assert r.status_code == 201
        [mail] = _staff(sent)
        assert mail["subject"] == "New lead (referral): Ravi"
        assert f"{FRONTEND}/dashboard/leads/{r.json()['lead_id']}" in mail["html"]
        assert mail["from"] == "Grid Nova <notifications@gridnova.in>"

    def test_estimate_form_lead_sends_no_staff_email(self, client, sent, email_on):
        email_on()
        r = client.post("/api/v1/public/estimate-request", json={"name": "E", "email": "e@example.com"})
        assert r.status_code == 200
        assert sent == []

    def test_estimate_form_source_is_filtered_even_if_notified(self, sent):
        _email_notifier().new_lead(lead_id=1, source="website_estimate_form", customer_name="E", created_by="w")
        assert sent == []

    def test_chat_lead_emails_once_on_qualification(self, client, sent, email_on):
        email_on()
        r1 = client.post("/api/v1/chat/message", json={"message": "Hi, I want solar for my home."})
        assert sent == []  # a first message is not a lead worth an email yet
        ids = {"lead_id": r1.json()["lead_id"], "customer_id": r1.json()["customer_id"]}
        r2 = client.post("/api/v1/chat/message", json={
            "message": ("My name is Rohan Mehta, email rohan.mehta@example.com. I need a "
                        "rooftop solar system, about 8kW capacity, located in Pune, Maharashtra."),
            **ids,
        })
        assert r2.json()["is_qualified"] is True
        [mail] = _staff(sent)
        assert mail["subject"] == "Lead qualified via website chat: Rohan Mehta"
        client.post("/api/v1/chat/message", json={"message": "When can you visit?", **ids})
        assert len(_staff(sent)) == 1  # not again on later turns

    def test_booking_sends_new_lead_and_meeting_emails_in_ist(self, client, memory, db_session,
                                                              sent, email_on, monkeypatch):
        email_on()
        monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
        memory.get_or_create_employee("emp-cal", "Cal", "cal@gridnova.in", "sales")
        db_session.commit()
        memory.update_employee_google_credentials(employee_id="emp-cal", encrypted_refresh_token=encrypt("r"),
                                                  calendar_id="primary", performed_by="t")
        db_session.commit()
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(google_calendar, "insert_event",
                            lambda **k: {"google_event_id": "ev", "meet_link": "https://meet.google.com/x"})
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        r = client.post("/api/v1/public/booking",
                        json={"name": "Booker", "email": "booker@example.com", "scheduled_at": slot.isoformat()})
        assert r.status_code == 200, r.text
        subjects = [m["subject"] for m in _staff(sent)]
        assert "New lead (website booking): Booker" in subjects
        assert f"Meeting booked: {format_ist_datetime(slot)}" in subjects

    def test_manager_signoff_required_on_draft(self, client, memory, db_session, sent, email_on):
        email_on()
        cid, lid = _lead(memory, db_session, capacity_kw=1500)  # over the 1 MW capacity threshold
        r = client.post(f"{DASH}/proposals/draft", json={"lead_id": lid, "customer_id": cid},
                        headers=admin_headers())
        assert r.json()["requires_manager_signoff"] is True
        [mail] = _staff(sent)
        assert mail["subject"] == f"Manager sign-off needed: proposal #{r.json()['proposal_id']}"
        assert "not yet set" in mail["text"]

    def test_manager_signoff_required_when_pricing_crosses_the_threshold(self, client, memory, db_session,
                                                                        sent, email_on):
        email_on()
        cid, lid = _lead(memory, db_session)
        pid = client.post(f"{DASH}/proposals/draft", json={"lead_id": lid, "customer_id": cid},
                          headers=admin_headers()).json()["proposal_id"]
        assert _staff(sent) == []
        over = settings.proposal_value_manager_approval_threshold + 1
        client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", json={"new_value": over, "reason": "scope"},
                     headers=admin_headers())
        [mail] = _staff(sent)
        assert f"INR {over:,.0f}" in mail["text"]
        client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", json={"new_value": over + 10, "reason": "tweak"},
                     headers=admin_headers())
        assert len(_staff(sent)) == 1  # already required — not re-announced

    @pytest.mark.usefixtures("known_employees")
    def test_customer_accepted(self, client, memory, db_session, sent, email_on):
        email_on()
        _, lid = _lead(memory, db_session, name="Accepting Customer")
        pid = _approved(memory, db_session, lid)
        token = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).json()["accept_token"]
        client.post(f"/api/v1/proposals/accept/{token}")
        mail = next(m for m in _staff(sent) if m["subject"].startswith("Proposal accepted"))
        assert mail["subject"] == "Proposal accepted: Accepting Customer"
        assert f"{FRONTEND}/dashboard/proposals/{pid}" in mail["html"]

    def test_no_staff_list_means_no_staff_email(self, client, sent, email_on):
        email_on(staff=[])
        client.post(f"{DASH}/leads", json={"name": "Ravi", "phone": "9876500001", "source": "other"},
                    headers=admin_headers())
        assert sent == []


# ── Customer proposal email ─────────────────────────────────────────────────────

@pytest.mark.usefixtures("known_employees")
class TestCustomerProposalEmail:
    def test_sent_with_the_website_accept_link_and_no_pdf_when_there_is_none(self, client, memory, db_session,
                                                                          sent, email_on):
        email_on()
        _, lid = _lead(memory, db_session, email="cust@example.com", name="Priya")
        pid = _approved(memory, db_session, lid)
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        body = r.json()
        assert body["customer_email_sent"] is True and body["customer_email_skipped_reason"] is None
        [mail] = [m for m in sent if m["to"] == ["cust@example.com"]]
        assert f"{FRONTEND}/proposal/accept/{body['accept_token']}" in mail["html"]
        assert "/api/v1/" not in mail["text"]
        assert "PDF" not in mail["text"]
        assert "Dear Priya" in mail["text"] and "version 1" in mail["text"]
        assert "INR" not in mail["text"]  # no price is restated in the email

    def test_pdf_link_is_the_public_endpoint_never_the_storage_url(self, client, memory, db_session,
                                                                    sent, email_on, monkeypatch):
        email_on()
        monkeypatch.setattr(settings, "public_api_base_url", "https://api.gridnova.in/")
        _, lid = _lead(memory, db_session, email="pdf@example.com")
        pid = _approved(memory, db_session, lid)
        storage_url = "https://acct.r2.cloudflarestorage.com/gridnova/proposals/1.pdf"
        memory.get_proposal(pid).pdf_url = storage_url
        db_session.commit()
        token = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).json()["accept_token"]
        [mail] = [m for m in sent if m["to"] == ["pdf@example.com"]]
        assert f"https://api.gridnova.in/api/v1/public/proposals/{token}/pdf" in mail["text"]
        assert storage_url not in mail["text"] and storage_url not in mail["html"]
        assert "r2.cloudflarestorage.com" not in mail["html"]

    def test_no_pdf_link_without_a_public_api_url(self, client, memory, db_session, sent, email_on, monkeypatch):
        email_on()
        monkeypatch.setattr(settings, "public_api_base_url", "")
        _, lid = _lead(memory, db_session, email="nopub@example.com")
        pid = _approved(memory, db_session, lid)
        memory.get_proposal(pid).pdf_url = "https://acct.r2.cloudflarestorage.com/gridnova/proposals/2.pdf"
        db_session.commit()
        client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        [mail] = [m for m in sent if m["to"] == ["nopub@example.com"]]
        assert "PDF" not in mail["text"] and "cloudflarestorage" not in mail["text"]

    def test_placeholder_email_is_skipped(self, client, memory, db_session, sent, email_on):
        email_on()
        customer = memory.create_placeholder_customer()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        pid = _approved(memory, db_session, lead.id)
        body = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).json()
        assert body["customer_email_sent"] is False
        assert body["customer_email_skipped_reason"] == "placeholder_customer_email"
        assert not any(m["to"] == [customer.email] for m in sent)

    def test_not_configured_reports_why(self, client, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.api.routes.dashboard.notifier", LogNotifier())
        _, lid = _lead(memory, db_session, email="nocfg@example.com")
        pid = _approved(memory, db_session, lid)
        body = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).json()
        assert body["customer_email_sent"] is False
        assert body["customer_email_skipped_reason"] == "email_not_configured"


# ── Failure handling and escaping ───────────────────────────────────────────────

class TestNeverRaises:
    @pytest.mark.usefixtures("known_employees")
    def test_transport_error_on_send_still_sends_the_proposal(self, client, memory, db_session,
                                                              email_on, monkeypatch):
        email_on()

        def _boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(notifier_module.httpx, "post", _boom)
        _, lid = _lead(memory, db_session, email="down@example.com")
        pid = _approved(memory, db_session, lid)
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["status"] == "sent"
        assert r.json()["customer_email_sent"] is False
        assert r.json()["customer_email_skipped_reason"] == "send_failed"

    @pytest.mark.parametrize("failure", ["raise", 500, 422])
    def test_staff_event_failure_is_logged_not_raised(self, monkeypatch, caplog, failure):
        def _post(*a, **k):
            if failure == "raise":
                raise RuntimeError("boom")
            return _Response(failure)
        monkeypatch.setattr(notifier_module.httpx, "post", _post)
        with caplog.at_level("WARNING", logger="app.notifications.notifier"):
            _email_notifier().customer_accepted(proposal_id=1, lead_id=2, version=1, customer_name="A")
        assert any("Email notification failed for customer_accepted" in r.getMessage() for r in caplog.records)

    def test_failure_is_reported_to_sentry_when_active(self, monkeypatch):
        import sentry_sdk
        captured = []
        monkeypatch.setattr(sentry_sdk, "is_initialized", lambda: True)
        monkeypatch.setattr(sentry_sdk, "capture_exception", lambda exc: captured.append(exc))
        monkeypatch.setattr(notifier_module.httpx, "post",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _email_notifier().new_lead(lead_id=1, source="other", customer_name="A", created_by="x")
        assert len(captured) == 1 and str(captured[0]) == "boom"

    def test_manual_lead_succeeds_when_email_fails(self, client, email_on, monkeypatch):
        email_on()
        monkeypatch.setattr(notifier_module.httpx, "post", lambda *a, **k: _Response(503))
        r = client.post(f"{DASH}/leads", json={"name": "R", "phone": "9876500002", "source": "other"},
                        headers=admin_headers())
        assert r.status_code == 201


class TestEscaping:
    NASTY = '<script>alert("x")</script> & <b>Bold</b>'

    def test_staff_email_escapes_customer_supplied_text(self, sent):
        _email_notifier().new_lead(lead_id=1, source="referral", customer_name=self.NASTY, created_by="x")
        [mail] = sent
        assert "<script>" not in mail["html"] and "<b>Bold</b>" not in mail["html"]
        assert "&lt;script&gt;" in mail["html"] and "&amp;" in mail["html"]

    def test_customer_email_escapes_the_name(self, sent):
        ok, reason = _email_notifier().send_proposal_to_customer(
            proposal_id=1, version=1, customer_email="x@example.com", customer_name=self.NASTY,
            accept_url=f"{FRONTEND}/proposal/accept/tok", pdf_url=None,
        )
        assert ok is True and reason is None
        [mail] = sent
        assert "<script>" not in mail["html"] and "&lt;script&gt;" in mail["html"]
