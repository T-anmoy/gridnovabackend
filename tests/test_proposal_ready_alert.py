"""BE-19 — `proposal_ready_for_review` (BE-17 left it with no callers): sent when a drafted
proposal is waiting for approval, routed to Admins and the lead's assigned employee."""
import pytest

import app.notifications.notifier as notifier_module
from app.notifications.notifier import LogNotifier, _staff_message
from tests.conftest import admin_headers
from tests.onboarding_support import (  # noqa: F401 — fixtures
    kyc_ready, onboarding_ready, territories,
)
from tests.test_proposal_inputs_auto_draft import _new_lead, _qualify, _submit_inputs
from tests.test_proposal_request_agreement import _assessed
from tests.onboarding_support import DASH, SALES


class Capture(LogNotifier):
    def __init__(self):
        self.events = []

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))


@pytest.fixture
def capture(monkeypatch):
    c = Capture()
    monkeypatch.setattr(notifier_module, "notifier", c)
    return c


def _ready(capture):
    return [p for e, p in capture.events if e == "proposal_ready_for_review"]


def test_formal_proposal_drafted_for_an_associate_customer(client, db_session, onboarding_ready, capture):
    _, o = _assessed(client, db_session)
    r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal", headers=SALES)
    assert r.status_code == 200, r.text
    [payload] = _ready(capture)
    assert payload["proposal_id"] == r.json()["formal_proposal"]["proposal_id"]
    assert payload["assigned_to"] == "emp-sales-1" and payload["onboarding_id"] == o["onboarding_id"]
    assert payload["version"] == 1 and payload["customer_name"]


def test_site_assessment_approve_and_draft(client, memory, db_session, capture):
    lead_id = _new_lead(memory, db_session, "ready-alert@example.com")
    _qualify(memory, db_session, lead_id)
    inputs_id = _submit_inputs(client, lead_id)
    r = client.patch(f"/api/v1/proposal-inputs/{inputs_id}/review", json={"status": "approved_for_proposal"},
                     headers=admin_headers())
    draft = r.json()["proposal_draft"]
    assert draft["triggered"] is True
    [payload] = _ready(capture)
    assert payload["proposal_id"] == draft["proposal_id"] and payload["lead_id"] == lead_id


def test_no_alert_when_nothing_was_drafted(client, memory, db_session, capture):
    lead_id = _new_lead(memory, db_session, "not-ready@example.com")   # not qualified
    inputs_id = _submit_inputs(client, lead_id)
    client.patch(f"/api/v1/proposal-inputs/{inputs_id}/review", json={"status": "approved_for_proposal"},
                 headers=admin_headers())
    assert _ready(capture) == []


def test_email_text_link_and_role_routing():
    from app.auth import access
    from app.notifications.notifier import STAFF_EMAIL_EVENTS
    assert "proposal_ready_for_review" in STAFF_EMAIL_EVENTS
    spec = access.load().alerts["proposal_ready_for_review"]
    assert spec["roles"] == frozenset({"admin"}) and spec["assigned_employee"] is True
    subject, lines, label, url = _staff_message(
        "proposal_ready_for_review", {"proposal_id": 9, "lead_id": 4, "assigned_to": "emp-sales-1",
                                      "customer_name": "Ruma Das", "version": 2}, "https://www.gridnova.in")
    assert subject == "Proposal to approve: Ruma Das"
    assert "Proposal #9 v2 for Ruma Das (lead #4) was drafted by emp-sales-1" in lines[0]
    assert url == "https://www.gridnova.in/dashboard/proposals/9"
