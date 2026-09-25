"""The widened stale-lead rule (any open stage, measured from the lead's last
activity) and the one-per-run staff digest email."""
from datetime import datetime, timedelta, timezone

import pytest

import app.notifications.notifier as notifier_module
from app.jobs.stale_leads import check_stale_leads
from app.memory.models import PipelineStage, TimelineEventType
from app.notifications.notifier import RESEND_API_URL, EmailNotifier, LogNotifier
from tests.conftest import backdate_lead, seed_employees

STAFF = ["ops@gridnova.in"]
BASE = "https://www.gridnova.in"


class _R:
    status_code = 200


@pytest.fixture
def sent(monkeypatch):
    mails = []

    def _post(url, headers=None, json=None, timeout=None):
        assert url == RESEND_API_URL
        mails.append(json)
        return _R()

    monkeypatch.setattr(notifier_module.httpx, "post", _post)
    monkeypatch.setattr(notifier_module, "notifier", EmailNotifier(
        api_key="re_x", from_email="n@gridnova.in", staff_emails=STAFF, frontend_base_url=BASE, inner=LogNotifier()))
    return mails


def _quiet_lead(memory, db_session, *, email, name="Quiet Customer", hours=60, stage=None,
                source=None, assigned=None):
    customer = memory.get_or_create_customer(email=email, name=name)
    kwargs = {"source": source} if source else {}
    lead = memory.create_lead(customer_id=customer.id, **kwargs)
    if stage is not None:
        lead.pipeline_stage = stage
    if assigned:
        seed_employees(db_session, assigned)
        lead.assigned_employee_id = assigned
    db_session.commit()
    backdate_lead(db_session, lead.id, datetime.now(timezone.utc) - timedelta(hours=hours))
    return lead.id


def _digests(sent):
    return [m for m in sent if "no activity" in m["subject"]]


class TestWidenedRule:
    def test_a_lead_nobody_ever_messaged_goes_stale(self, memory, db_session, sent):
        from app.memory.models import LeadSource
        _quiet_lead(memory, db_session, email="form@example.com", source=LeadSource.website_estimate_form)
        assert check_stale_leads(db_session)["lead_stale_48h"] == 1

    @pytest.mark.parametrize("stage", [PipelineStage.qualified, PipelineStage.proposal_draft,
                                       PipelineStage.proposal_sent, PipelineStage.negotiation])
    def test_every_open_stage_counts(self, memory, db_session, sent, stage):
        _quiet_lead(memory, db_session, email=f"{stage.value}@example.com", stage=stage)
        assert check_stale_leads(db_session)["lead_stale_48h"] == 1

    def test_measured_from_creation_when_nothing_else_happened(self, memory, db_session, sent):
        _quiet_lead(memory, db_session, email="young@example.com", hours=10)
        assert check_stale_leads(db_session)["lead_stale_48h"] == 0


def _stage_change(memory, lead_id):
    memory.set_lead_stage(lead_id, PipelineStage.negotiation, performed_by="emp-admin-1", reason="call")


def _assignment(memory, lead_id):
    memory.reassign_lead(lead_id, "emp-sales-1", performed_by="emp-admin-1")


def _note(memory, lead_id):
    memory.update_lead_fields(lead_id=lead_id, new_fields={"notes": "Called, will decide next week"},
                              performed_by="emp-sales-1")


def _meeting(memory, lead_id):
    memory.add_timeline_event(lead_id=lead_id, event_type=TimelineEventType.meeting_scheduled,
                              description="Meeting", created_by="website")


def _proposal_action(memory, lead_id):
    memory.create_proposal_draft(lead_id=lead_id, content={}, total_value=None,
                                 requires_manager_signoff=False, performed_by="emp-sales-1")


def _site_assessment(memory, lead_id):
    memory.create_proposal_inputs(
        lead_id=lead_id, performed_by="emp-sales-1", customer_type="individual",
        project_family="rooftop_solar", state="West Bengal", district="Kolkata", pin_code="700001",
        site_type="rooftop_residential", connection_type="on_grid")


def _customer_message(memory, lead_id):
    memory.record_customer_message(lead_id)


@pytest.mark.parametrize("activity", [_stage_change, _assignment, _note, _meeting, _proposal_action,
                                      _site_assessment, _customer_message])
def test_recent_activity_resets_the_clock(memory, db_session, sent, activity):
    seed_employees(db_session, "emp-sales-1", "emp-admin-1")
    lead_id = _quiet_lead(memory, db_session, email="busy@example.com", hours=24 * 6)
    activity(memory, lead_id)
    db_session.commit()
    counts = check_stale_leads(db_session)
    assert counts["lead_stale_48h"] == 0 and counts["lead_stale_5d"] == 0


class TestDigest:
    def test_one_email_listing_every_stale_lead(self, memory, db_session, sent):
        a = _quiet_lead(memory, db_session, email="a@example.com", name="Anita", hours=60, assigned="emp-sales-1")
        b = _quiet_lead(memory, db_session, email="b@example.com", name="Bikash", hours=24 * 6)
        _quiet_lead(memory, db_session, email="fresh@example.com", name="Fresh", hours=5)
        check_stale_leads(db_session)

        [digest] = _digests(sent)
        assert digest["to"] == STAFF
        assert digest["subject"] == "2 leads with no activity"
        for lead_id in (a, b):
            assert f"{BASE}/dashboard/leads/{lead_id}" in digest["html"]
        assert "Anita" in digest["text"] and "Bikash" in digest["text"] and "Fresh" not in digest["text"]
        assert "emp-sales-1" in digest["text"] and "unassigned" in digest["text"]
        assert "website chat" in digest["text"] and "stage new" in digest["text"]
        assert "past 48 hours" in digest["text"] and "past 5 days" in digest["text"]
        assert "quiet 2.5 days" in digest["text"]

    def test_names_are_escaped(self, memory, db_session, sent):
        _quiet_lead(memory, db_session, email="x@example.com", name='<img src=x onerror="alert(1)">')
        check_stale_leads(db_session)
        [digest] = _digests(sent)
        assert "<img" not in digest["html"] and "&lt;img" in digest["html"]

    def test_nothing_sent_when_nothing_is_stale(self, memory, db_session, sent):
        _quiet_lead(memory, db_session, email="fresh@example.com", hours=3)
        check_stale_leads(db_session)
        assert sent == []

    def test_no_duplicates_across_runs(self, memory, db_session, sent):
        _quiet_lead(memory, db_session, email="dup@example.com", hours=60)
        check_stale_leads(db_session)
        check_stale_leads(db_session)
        assert len(_digests(sent)) == 1

    def test_escalation_is_reported_once_more(self, memory, db_session, sent):
        start = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
        customer = memory.get_or_create_customer(email="esc@example.com", name="Esc")
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        backdate_lead(db_session, lead.id, start)
        check_stale_leads(db_session, now=start + timedelta(hours=49))
        check_stale_leads(db_session, now=start + timedelta(hours=50))
        check_stale_leads(db_session, now=start + timedelta(days=6))
        subjects = [d["text"] for d in _digests(sent)]
        assert len(subjects) == 2
        assert "past 48 hours" in subjects[0] and "past 5 days" in subjects[1]

    def test_no_staff_list_means_no_digest(self, memory, db_session, monkeypatch):
        mails = []
        monkeypatch.setattr(notifier_module.httpx, "post", lambda *a, **k: mails.append(1))
        monkeypatch.setattr(notifier_module, "notifier", EmailNotifier(
            api_key="re_x", from_email="n@gridnova.in", staff_emails=[], frontend_base_url=BASE,
            inner=LogNotifier()))
        _quiet_lead(memory, db_session, email="nostaff@example.com")
        assert check_stale_leads(db_session)["lead_stale_48h"] == 1
        assert mails == []
