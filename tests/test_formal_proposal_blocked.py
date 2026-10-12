"""Part 2 — staff always learn when a formal proposal request is blocked, and can fix
it in one action."""
import pytest
from sqlalchemy import select

import app.notifications.notifier as notifier_module
from app.memory.models import AuditLog, Customer, CustomerOnboarding, Lead, Proposal
from app.notifications.notifier import LogNotifier
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, headers, kyc_ready, make_associate, onboarding_body, onboarding_ready,
    territories, upload_required_documents, complete_survey, confirm_site_location, review_survey,
)


class Capture(LogNotifier):
    def __init__(self):
        self.events = []

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))


@pytest.fixture
def captured(monkeypatch):
    c = Capture()
    monkeypatch.setattr(notifier_module, "notifier", c)
    return c


def _assessed(client, db_session, n=1, **kw):
    gna = make_associate(db_session)
    o = client.post(BASE, json=onboarding_body(n=n, **kw), headers=headers(gna)).json()
    oid = o["onboarding_id"]
    upload_required_documents(client, gna, oid)
    confirm_site_location(client, gna, oid)
    complete_survey(client, gna, oid)
    client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
    client.post(f"{DASH}/onboardings/{oid}/verification-call",
                json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
    client.patch(f"/api/v1/proposal-inputs/{o['site_assessment']['proposal_inputs_id']}/review",
                 json={"status": "approved_for_proposal"}, headers=ENG)
    assert review_survey(client, oid).status_code == 200   # the Engineer's feasibility review (S1 Step 5)
    return gna, oid


def _blocked(client, db_session, n=1):
    """An associate requested the formal proposal for a customer with no email."""
    gna, oid = _assessed(client, db_session, n=n)
    r = client.post(f"{BASE}/{oid}/request-formal-proposal", headers=headers(gna))
    assert r.json()["request"]["missing_fields"] == ["email"]
    return gna, oid


class TestStaffFormalProposalGet:
    def test_matches_the_associate_view(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        associate_view = client.get(f"{BASE}/{oid}/formal-proposal", headers=headers(gna)).json()
        r = client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=ADMIN)
        assert r.status_code == 200 and r.json() == associate_view
        assert associate_view == {"prepared": False, "missing_fields": ["email"]}

    def test_reflects_a_prepared_proposal(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "asha@example.test"},
                    headers=SALES)
        r = client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=ADMIN)
        assert r.status_code == 200
        assert r.json()["prepared"] is True and r.json()["proposal_id"]

    def test_sales_keeps_its_own_plus_unassigned_rule(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        assert client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=SALES).status_code == 200
        onboarding = db_session.get(CustomerOnboarding, oid)
        lead = db_session.get(Lead, onboarding.lead_id)
        lead.assigned_employee_id = "emp-sales-1"
        db_session.commit()
        other_sales = {"X-Employee-Id": "emp-sales-2", "X-Employee-Role": "sales"}
        assert client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=other_sales).status_code == 403
        assert client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=SALES).status_code == 200

    def test_role_without_view_access_is_refused(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        assert client.get(f"{DASH}/onboardings/{oid}/formal-proposal", headers=ENG).status_code == 403


class TestMarker:
    def test_on_detail_and_list_for_staff_and_the_associate(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        detail = client.get(f"{DASH}/onboardings/{oid}", headers=SALES).json()
        blocked = detail["formal_proposal_blocked"]
        assert blocked["missing_fields"] == ["email"] and blocked["missing"] == ["no email on file"]
        assert blocked["requested_by"] == f"associate:{gna}" and blocked["requested_at"]
        listed = client.get(f"{DASH}/onboardings", headers=SALES).json()["items"][0]
        assert listed["formal_proposal_blocked"] == blocked
        assert client.get(f"{BASE}/{oid}", headers=headers(gna)).json()["formal_proposal_blocked"] == blocked

    def test_not_blocked_when_ready_or_not_requested(self, client, db_session, onboarding_ready):
        gna, ready = _assessed(client, db_session, n=1, customer_email="ok@example.test")
        client.post(f"{BASE}/{ready}/request-formal-proposal", headers=headers(gna))
        _, not_requested = _assessed(client, db_session, n=2)
        for oid in (ready, not_requested):
            assert client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()["formal_proposal_blocked"] is None

    def test_needs_attention_filter(self, client, db_session, onboarding_ready):
        _, blocked_a = _blocked(client, db_session, n=1)
        _, blocked_b = _blocked(client, db_session, n=2)
        _, fine = _assessed(client, db_session, n=3)
        r = client.get(f"{DASH}/onboardings", params={"needs_attention": True}, headers=ADMIN).json()
        assert r["total"] == 2 and {i["onboarding_id"] for i in r["items"]} == {blocked_a, blocked_b}
        r = client.get(f"{DASH}/onboardings", params={"needs_attention": False}, headers=ADMIN).json()
        assert [i["onboarding_id"] for i in r["items"]] == [fine]
        r = client.get(f"{DASH}/onboardings", params={"needs_attention": True, "page_size": 1, "page": 2},
                       headers=ADMIN).json()
        assert r["total"] == 2 and len(r["items"]) == 1


class TestStaffEmail:
    def test_event_names_the_customer_and_what_is_missing(self, client, db_session, onboarding_ready, captured):
        gna, oid = _blocked(client, db_session)
        [payload] = [p for e, p in captured.events if e == "formal_proposal_requested"]
        assert payload["customer_name"] == "Customer 1" and payload["missing"] == ["no email on file"]
        assert payload["onboarding_id"] == oid and payload["project_reference_id"].startswith("GNE-WB-")

    def test_only_after_commit(self, client, db_session, onboarding_ready, captured, monkeypatch):
        gna, oid = _assessed(client, db_session)

        def _boom(*a, **k):
            raise RuntimeError("commit-time failure")
        from app.onboarding import progress
        real = progress.record
        monkeypatch.setattr(progress, "record",
                            lambda db, o, stage, *a, **k: (real(db, o, stage, *a, **k), _boom())[0])
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app, raise_server_exceptions=False).post(f"{BASE}/{oid}/request-formal-proposal",
                                                                headers=headers(gna))
        assert r.status_code == 500
        assert not [e for e, _ in captured.events if e == "formal_proposal_requested"]

    def test_email_text_and_recipients(self, monkeypatch):
        import httpx
        from app.notifications.notifier import EmailNotifier, STAFF_EMAIL_EVENTS
        assert "formal_proposal_requested" in STAFF_EMAIL_EVENTS
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        n = EmailNotifier("key", "Grid Nova <n@x.test>", ["staff@x.test"], "https://app.test", Capture())
        base = {"onboarding_id": 4, "lead_id": 9, "requested_by": "associate:GNA-WB-26-00001",
                "customer_name": "Asha Roy", "project_reference_id": "GNE-WB-ROF-2026-00004"}
        n.notify("formal_proposal_requested", {**base, "missing": ["no email on file"]})
        n.notify("formal_proposal_requested", {**base, "missing": []})
        assert [m["to"] for m in sent] == [["staff@x.test"], ["staff@x.test"]]
        assert sent[0]["subject"] == "Formal proposal blocked: Asha Roy — no email on file"
        assert "It cannot be drafted yet: no email on file." in sent[0]["text"]
        assert "https://app.test/dashboard/onboardings/4" in sent[0]["text"]
        assert sent[1]["subject"] == "Formal proposal requested: Asha Roy"


class TestFixInPlace:
    def test_set_the_email_and_prepare_in_one_call(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        url = f"{DASH}/onboardings/{oid}/formal-proposal"
        assert client.post(url, headers=SALES).json()["detail"]["code"] == "formal_proposal_not_ready"
        r = client.post(url, json={"customer_email": "Asha@Example.test"}, headers=SALES)
        assert r.status_code == 200, r.text
        proposal = db_session.get(Proposal, r.json()["formal_proposal"]["proposal_id"])
        assert proposal.drafted_by == "emp-sales-1"
        onboarding = db_session.get(CustomerOnboarding, oid)
        db_session.expire_all()
        assert db_session.get(Customer, onboarding.customer_id).email == "asha@example.test"
        assert client.get(f"{DASH}/onboardings/{oid}", headers=SALES).json()["formal_proposal_blocked"] is None
        entry = db_session.execute(select(AuditLog).where(AuditLog.action == "customer_email_set")).scalar_one()
        assert entry.performed_by == "emp-sales-1" and entry.after_value == {"email": "asha@example.test"}
        assert r.json()["progress"]["current_stage"] == "proposal_prepared"

    def test_refusals(self, client, db_session, memory, onboarding_ready):
        memory.get_or_create_customer("taken@example.test", "Someone Else")
        db_session.commit()
        gna, oid = _blocked(client, db_session)
        url = f"{DASH}/onboardings/{oid}/formal-proposal"
        r = client.post(url, json={"customer_email": "taken@example.test"}, headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "email_in_use"
        assert client.post(url, json={"customer_email": "not-an-email"}, headers=SALES).status_code == 422
        assert client.post(url, json={"customer_email": "a@example.test"}, headers=MGMT).status_code == 403
        # Nothing was changed by the refused attempts.
        onboarding = db_session.get(CustomerOnboarding, oid)
        assert db_session.get(Customer, onboarding.customer_id).email.endswith("@pending.gridnova.internal")

    def test_never_replaces_a_real_email(self, client, db_session, onboarding_ready):
        gna, oid = _assessed(client, db_session, customer_email="real@example.test")
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "other@example.test"},
                        headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "email_already_set"
        # The same address is simply accepted.
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "real@example.test"},
                        headers=SALES)
        assert r.status_code == 200


class TestDraftFailure:
    """The email is saved, then the draft itself fails: the marker must switch to a
    distinct "draft_failed" reason — never silently clear."""

    def _fail_drafts(self, monkeypatch=None, *, raises=False):
        """A scoped patch (a context manager) when no monkeypatch is given, so undoing
        it never touches the harness's own patches."""
        from unittest.mock import patch
        from app.orchestrator import orchestrator as orchestrator_module

        def _fake(**kwargs):
            if raises:
                raise RuntimeError("boom")
            return {"error": "Agent 'proposal_agent' is disabled by governance."}
        if monkeypatch is None:
            return patch.object(orchestrator_module.orchestrator, "request_proposal_draft", _fake)
        monkeypatch.setattr(orchestrator_module.orchestrator, "request_proposal_draft", _fake)

    def test_missing_information_reason(self, client, db_session, onboarding_ready):
        gna, oid = _blocked(client, db_session)
        blocked = client.get(f"{DASH}/onboardings/{oid}", headers=SALES).json()["formal_proposal_blocked"]
        assert blocked["reason"] == "missing_information" and blocked["draft_error"] is None

    def test_failed_draft_after_the_email_is_saved(self, client, db_session, onboarding_ready, monkeypatch):
        gna, oid = _blocked(client, db_session)
        self._fail_drafts(monkeypatch)
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "asha@example.test"},
                        headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "draft_failed"
        assert "disabled by governance" in r.json()["detail"]["message"]
        detail = client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()
        assert detail["customer"]["email"] == "asha@example.test"  # the fix itself was kept
        blocked = detail["formal_proposal_blocked"]
        assert blocked["reason"] == "draft_failed" and blocked["missing_fields"] == []
        assert blocked["draft_error"] == "Agent 'proposal_agent' is disabled by governance."
        assert blocked["draft_failed_at"] and blocked["requested_by"] == f"associate:{gna}"
        listed = client.get(f"{DASH}/onboardings", params={"needs_attention": True}, headers=ADMIN).json()
        assert [i["onboarding_id"] for i in listed["items"]] == [oid]
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "formal_draft_failed")).scalar_one()

    def test_an_exception_is_recorded_not_a_500(self, client, db_session, onboarding_ready, monkeypatch):
        gna, oid = _blocked(client, db_session)
        self._fail_drafts(monkeypatch, raises=True)
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "asha@example.test"},
                        headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "draft_failed"
        blocked = client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()["formal_proposal_blocked"]
        assert blocked["reason"] == "draft_failed" and "RuntimeError" in blocked["draft_error"]

    def test_a_retry_that_succeeds_clears_it(self, client, db_session, onboarding_ready, monkeypatch):
        gna, oid = _blocked(client, db_session)
        with self._fail_drafts():
            client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "asha@example.test"},
                        headers=SALES)
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", headers=SALES)
        assert r.status_code == 200, r.text
        assert client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()["formal_proposal_blocked"] is None
        db_session.expire_all()
        o = db_session.get(CustomerOnboarding, oid)
        assert o.formal_draft_failed_at is None and o.formal_draft_failure is None

    def test_a_draft_from_the_proposals_route_also_clears_it(self, client, db_session, onboarding_ready,
                                                              monkeypatch):
        gna, oid = _blocked(client, db_session)
        with self._fail_drafts():
            client.post(f"{DASH}/onboardings/{oid}/formal-proposal", json={"customer_email": "asha@example.test"},
                        headers=SALES)
        o = db_session.get(CustomerOnboarding, oid)
        r = client.post(f"{DASH}/proposals/draft", json={"lead_id": o.lead_id, "customer_id": o.customer_id},
                        headers=SALES)
        assert r.status_code == 200 and r.json()["proposal_id"]
        assert client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()["formal_proposal_blocked"] is None
