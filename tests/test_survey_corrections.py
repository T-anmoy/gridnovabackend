"""S1 Step 6 — change control: correction requests after verification, applying and
rejecting them (segregation of duties, locking), re-verification of open proposals,
and the block on every path that puts a proposal in a customer's hands."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.memory.memory_manager import MemoryManager, ProposalReverificationError
from app.memory.models import (
    AuditLog, CustomerOnboarding, OnboardingStatus, Proposal, ProposalInputs, ProposalStatus, SurveyCorrectionRequest,
    TimelineEvent,
)
from tests.conftest import TEST_DATABASE_URL
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, complete_survey, headers, kyc_ready, make_associate, onboarding_ready,
    review_survey, sign_survey, territories,
)
from tests.test_proposal_request_agreement import _approved, _in_person
from tests.test_survey_review import _submitted, _verified

ENG2 = {"X-Employee-Id": "emp-eng-2", "X-Employee-Role": "engineer"}
OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
PG_ONLY = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")


def _url(oid):
    return f"{BASE}/{oid}/survey/corrections"


def _ask(client, gna, oid, changes=None, reason="Measured the terrace again"):
    return client.post(_url(oid), json={"changes": changes or {"roof_area_sqft": 950}, "reason": reason},
                       headers=headers(gna))


def _apply(client, cid, who=ENG, **body):
    return client.post(f"{DASH}/survey-corrections/{cid}/apply", json=body, headers=who)


def _detail(r):
    return r.json()["detail"]


def _inputs(db_session, oid) -> ProposalInputs:
    db_session.expire_all()
    return db_session.get(ProposalInputs, db_session.get(CustomerOnboarding, oid).proposal_inputs_id)


def _proposal(db_session, oid, status=ProposalStatus.approved, version=1, **kw) -> int:
    onboarding = db_session.get(CustomerOnboarding, oid)
    if status in (ProposalStatus.approved, ProposalStatus.sent):
        kw.setdefault("approved_by_employee_id", "emp-admin-1")
    p = Proposal(lead_id=onboarding.lead_id, version=version, status=status, content={}, total_value=100000.0, **kw)
    db_session.add(p)
    db_session.commit()
    return p.id


class TestRequesting:
    def test_verified_owner_asks_and_lists(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        r = _ask(client, gna, oid)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "pending" and body["changes"] == {"roof_area_sqft": 950}
        assert "requested_by" not in body
        listed = client.get(_url(oid), headers=headers(gna)).json()["items"]
        assert [c["correction_id"] for c in listed] == [body["correction_id"]]
        lead_id = db_session.get(CustomerOnboarding, oid).lead_id
        assert db_session.execute(select(TimelineEvent).where(
            TimelineEvent.lead_id == lead_id, TimelineEvent.event_type == "survey_correction_requested")).scalar_one()
        assert db_session.execute(select(AuditLog).where(
            AuditLog.action == "survey_correction_requested")).scalar_one().performed_by == f"associate:{gna}"

    def test_one_pending_at_a_time(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        assert _ask(client, gna, oid).status_code == 201
        r = _ask(client, gna, oid, {"floors": 3})
        assert r.status_code == 409 and _detail(r)["code"] == "correction_pending"
        assert db_session.query(SurveyCorrectionRequest).count() == 1

    def test_draft_and_submitted_are_edited_directly(self, client, db_session, onboarding_ready):
        gna, oid = _submitted(client, db_session)
        r = _ask(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "edit_directly"

    def test_closed_is_history(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        db_session.get(CustomerOnboarding, oid).status = OnboardingStatus.closed
        db_session.commit()
        r = _ask(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "not_editable"

    @pytest.mark.parametrize("changes, code", [
        ({}, "no_changes"),
        ({"customer_name": "X"}, "not_a_survey_field"),
        ({"desired_output_kw": 5}, "not_a_survey_field"),
        ({"consumer_number": "999"}, "field_not_correctable"),
        ({"floors": -1}, "invalid_value"),
        ({"roof_material": "glass"}, "invalid_value"),
        ({"consumption_history": [{"month": "2099-01", "units_kwh": 10}]}, "future_month"),
        ({"distance_panel_to_inverter_m": 400}, "out_of_range"),
    ])
    def test_same_validation_as_an_edit(self, client, db_session, onboarding_ready, changes, code):
        gna, oid = _verified(client, db_session)
        r = client.post(_url(oid), json={"changes": changes, "reason": "r"}, headers=headers(gna))
        assert r.status_code == 422 and _detail(r)["code"] == code, r.text

    def test_reason_required(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        r = _ask(client, gna, oid, reason="  ")
        assert r.status_code == 422 and _detail(r)["code"] == "reason_required"

    def test_territory_viewer_reads_but_cannot_ask(self, client, db_session, onboarding_ready):
        from tests.onboarding_support import ALIPURDUAR
        gna, oid = _verified(client, db_session)
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        assert _ask(client, head, oid).status_code == 403
        assert client.get(_url(oid), headers=headers(head)).status_code == 200


class TestDeciding:
    def test_apply_writes_derives_clears_signatures_and_reopens_review(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        review_survey(client, oid)
        sections = [{"label": "Main", "length_m": 10, "width_m": 8}]
        cid = _ask(client, gna, oid, {"roof_sections": sections, "water_available": False}).json()["correction_id"]
        r = _apply(client, cid, note="Matches the photos")
        assert r.status_code == 200, r.text
        assert r.json()["correction"]["status"] == "applied" and r.json()["correction"]["reviewed_by"] == "emp-eng-1"
        assert r.json()["flagged_proposal_ids"] == []
        inputs = _inputs(db_session, oid)
        assert inputs.water_available is False
        assert inputs.roof_sections[0]["width_m"] == "8.00"
        assert inputs.shadow_free_area_sqft == pytest.approx(861.1)   # 80 m² in sq ft — derived, not sent
        assert inputs.customer_signature_ref is None and inputs.associate_signature_ref is None
        assert inputs.survey_revised_after_review is True
        assert db_session.get(CustomerOnboarding, oid).status == OnboardingStatus.verified
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "survey_corrected")).scalar_one()
        assert audit.before_value["water_available"] is True and audit.after_value["water_available"] is False
        assert audit.after_value["signatures"] == "cleared_after_edit" and audit.after_value["correction_id"] == cid
        lead_id = db_session.get(CustomerOnboarding, oid).lead_id
        assert db_session.execute(select(TimelineEvent).where(
            TimelineEvent.lead_id == lead_id, TimelineEvent.event_type == "survey_corrected")).scalar_one()
        # The verified customer signs again (it stays verified), and the to-do says so first.
        home = client.get("/api/v1/associate/home", headers=headers(gna)).json()["items"]
        assert next(i for i in home if i["kind"] == "survey_resign_needed")["params"]["unsigned"] == [
            "customer", "associate"]
        for party in ("customer", "associate"):
            assert sign_survey(client, gna, oid, party).status_code == 201
        assert db_session.get(CustomerOnboarding, oid).status == OnboardingStatus.verified
        assert sign_survey(client, gna, oid, "associate").status_code == 409   # nothing cleared now

    def test_reject_needs_a_note_and_changes_nothing(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        cid = _ask(client, gna, oid).json()["correction_id"]
        r = client.post(f"{DASH}/survey-corrections/{cid}/reject", json={"note": " "}, headers=ENG)
        assert r.status_code == 422 and _detail(r)["code"] == "note_required"
        r = client.post(f"{DASH}/survey-corrections/{cid}/reject", json={"note": "Photos show 800"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["correction"]["status"] == "rejected"
        assert _inputs(db_session, oid).roof_area_sqft == 800
        assert _apply(client, cid).status_code == 409
        # Rejected requests are kept, and a new one may follow.
        assert _ask(client, gna, oid, {"floors": 3}).status_code == 201
        assert len(client.get(_url(oid), headers=headers(gna)).json()["items"]) == 2

    def test_cannot_apply_twice(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        cid = _ask(client, gna, oid).json()["correction_id"]
        assert _apply(client, cid).status_code == 200
        r = _apply(client, cid, who=ADMIN)
        assert r.status_code == 409 and _detail(r)["code"] == "correction_not_pending"

    def test_requester_can_never_apply(self, db_session, client, onboarding_ready):
        from app.associates.service import AssociateError
        from app.survey import corrections
        gna, oid = _verified(client, db_session)
        cid = _ask(client, gna, oid).json()["correction_id"]
        with pytest.raises(AssociateError) as exc:
            corrections.apply(db_session, cid, f"associate:{gna}")
        assert exc.value.code == "segregation_of_duties" and exc.value.status_code == 403

    @pytest.mark.parametrize("who", [SALES, MGMT, OPS])
    def test_only_engineer_and_admin_decide(self, client, db_session, onboarding_ready, who):
        gna, oid = _verified(client, db_session)
        cid = _ask(client, gna, oid).json()["correction_id"]
        assert _apply(client, cid, who=who).status_code == 403
        assert client.post(f"{DASH}/survey-corrections/{cid}/reject", json={"note": "x"},
                           headers=who).status_code == 403

    def test_queue_filters_and_scopes_sales(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        cid = _ask(client, gna, oid).json()["correction_id"]
        q = client.get(f"{DASH}/survey-corrections", params={"status": "pending"}, headers=ENG).json()
        assert [i["correction_id"] for i in q["items"]] == [cid]
        assert q["items"][0]["client_code"].startswith("GNE-") and q["items"][0]["requested_by"] == f"associate:{gna}"
        assert client.get(f"{DASH}/survey-corrections", params={"status": "applied"}, headers=ENG).json()["total"] == 0
        # Assigned to someone else: Sales no longer sees it.
        from app.memory.models import Lead
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-sales-2")
        db_session.get(Lead, db_session.get(CustomerOnboarding, oid).lead_id).assigned_employee_id = "emp-sales-2"
        db_session.commit()
        assert client.get(f"{DASH}/survey-corrections", headers=SALES).json()["total"] == 0
        assert client.get(f"{DASH}/survey-corrections", headers=OPS).status_code == 403


class TestReverification:
    def _correct(self, client, gna, oid, who=ENG, changes=None):
        cid = _ask(client, gna, oid, changes).json()["correction_id"]
        r = _apply(client, cid, who=who)
        assert r.status_code == 200, r.text
        return cid, r.json()

    def test_open_proposals_are_flagged_and_accepted_ones_are_not(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        accepted = _proposal(db_session, oid, ProposalStatus.accepted, version=1,
                             accepted_at=datetime.now(timezone.utc))
        draft = _proposal(db_session, oid, ProposalStatus.draft, version=2)
        cid, result = self._correct(client, gna, oid)
        assert result["flagged_proposal_ids"] == [draft]
        db_session.expire_all()
        p = db_session.get(Proposal, draft)
        assert p.needs_reverification is True and p.reverification_flagged_at is not None
        assert p.reverification_reason["fields"] == ["roof_area_sqft"]
        assert p.reverification_reason["source"] == "correction" and p.reverification_reason["correction_id"] == cid
        assert db_session.get(Proposal, accepted).needs_reverification is False
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "reverification_flagged")).scalar_one()
        assert audit.entity_id == str(draft)
        # The dashboard shows it, Engineers included.
        for who in (SALES, ENG):
            assert client.get(f"{DASH}/proposals/{draft}", headers=who).json()["needs_reverification"] is True
        item = next(i for i in client.get(f"{DASH}/proposals", headers=ADMIN).json()["items"] if i["id"] == draft)
        assert item["reverification_reason"]["fields"] == ["roof_area_sqft"]

    def test_a_field_outside_the_list_flags_nothing(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        pid = _proposal(db_session, oid)
        _, result = self._correct(client, gna, oid, changes={"water_available": False})
        assert result["flagged_proposal_ids"] == []
        db_session.expire_all()
        assert db_session.get(Proposal, pid).needs_reverification is False

    def test_an_associate_edit_flags_too(self, client, db_session, onboarding_ready):
        gna, oid = _submitted(client, db_session)
        review_survey(client, oid, "revisit_needed", notes="n",
                      revisit_items=[{"section": "connection", "field": "sanctioned_load_kw", "note": "Check bill"}])
        pid = _proposal(db_session, oid, ProposalStatus.draft)
        assert client.patch(f"{BASE}/{oid}", json={"sanctioned_load_kw": 7}, headers=headers(gna)).status_code == 200
        db_session.expire_all()
        reason = db_session.get(Proposal, pid).reverification_reason
        assert reason["source"] == "revisit" and reason["fields"] == ["sanctioned_load_kw"]
        assert reason["correction_id"] is None

    def test_clear_rules(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        pid = _proposal(db_session, oid)
        self._correct(client, gna, oid, who=ENG)
        url = f"{DASH}/proposals/{pid}/reverification/clear"
        assert client.post(url, json={"note": "x"}, headers=SALES).status_code == 403
        assert client.post(url, json={"note": "x"}, headers=OPS).status_code == 403
        r = client.post(url, json={"note": "x"}, headers=ENG)   # the engineer who applied it
        assert r.status_code == 403 and _detail(r)["code"] == "segregation_of_duties"
        r = client.post(url, json={"note": "  "}, headers=MGMT)
        assert r.status_code == 422 and _detail(r)["code"] == "note_required"
        r = client.post(url, json={"note": "Sizing still fits the new area"}, headers=MGMT)
        assert r.status_code == 200, r.text
        assert r.json()["needs_reverification"] is False and r.json()["reverification_cleared_by"] == "emp-mgmt-1"
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "reverification_cleared")).scalar_one()
        assert audit.after_value["note"] == "Sizing still fits the new area"
        r = client.post(url, json={"note": "again"}, headers=ENG2)
        assert r.status_code == 409 and _detail(r)["code"] == "not_flagged"


class TestEveryPathIsBlocked:
    """One test per path that puts a proposal in a customer's hands."""

    def _flagged(self, client, db_session, *, send_first=False):
        gna, o, pid = _approved(client, db_session, connection_phase="single_phase")
        oid = o["onboarding_id"]
        token = None
        if send_first:
            assert client.post(f"{DASH}/proposals/{pid}/send", headers=SALES).status_code == 200
            db_session.expire_all()
            token = db_session.get(Proposal, pid).accept_token
        cid = _ask(client, gna, oid).json()["correction_id"]
        assert _apply(client, cid).status_code == 200
        db_session.expire_all()
        assert db_session.get(Proposal, pid).needs_reverification is True
        return gna, oid, pid, token

    def test_staff_send(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, oid, pid, _ = self._flagged(client, db_session)
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=SALES)
        assert r.status_code == 409
        assert _detail(r)["code"] == "needs_reverification" and _detail(r)["fields"] == ["roof_area_sqft"]
        assert client.post(f"{DASH}/proposals/{pid}/reverification/clear", json={"note": "ok"},
                           headers=ADMIN).status_code == 200
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=SALES).status_code == 200

    def test_online_acceptance(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, oid, pid, token = self._flagged(client, db_session, send_first=True)
        assert token
        for method in ("GET", "POST"):
            r = client.request(method, f"/api/v1/proposals/accept/{token}")
            assert r.status_code == 409 and _detail(r)["code"] == "needs_reverification"
            assert "fields" not in _detail(r)   # the customer is not told the internals
        db_session.expire_all()
        assert db_session.get(Proposal, pid).accepted_at is None
        client.post(f"{DASH}/proposals/{pid}/reverification/clear", json={"note": "ok"}, headers=ADMIN)
        assert client.post(f"/api/v1/proposals/accept/{token}").status_code == 200

    @pytest.mark.parametrize("by", ["associate", "staff"])
    def test_in_person_acceptance(self, client, db_session, sendable_proposal_content, onboarding_ready, by):
        gna, oid, pid, _ = self._flagged(client, db_session)
        r = _in_person(client, headers(gna) if by == "associate" else SALES, oid, pid)
        assert r.status_code == 409, r.text
        assert _detail(r)["code"] == "needs_reverification" and _detail(r)["fields"] == ["roof_area_sqft"]
        db_session.expire_all()
        assert db_session.get(Proposal, pid).status == ProposalStatus.approved

    def test_the_state_machine_refuses_as_a_last_line(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        approved = _proposal(db_session, oid, ProposalStatus.approved, needs_reverification=True)
        sent = _proposal(db_session, oid, ProposalStatus.sent, version=2, needs_reverification=True)
        memory = MemoryManager(db_session)
        with pytest.raises(ProposalReverificationError):
            memory.transition_proposal_status(approved, ProposalStatus.sent, performed_by="emp-admin-1")
        with pytest.raises(ProposalReverificationError):
            memory.mark_customer_accepted(sent)
        # Sending back for revision is still allowed.
        memory.transition_proposal_status(approved, ProposalStatus.revision_requested, performed_by="emp-admin-1")


@PG_ONLY
def test_two_reviewers_cannot_both_decide(client, db_session, onboarding_ready, _isolated_db):
    from app.associates.service import AssociateError
    from app.survey import corrections
    from tests.conftest import seed_employees
    from tests.test_concurrency_conflicts import _race
    seed_employees(db_session, "emp-eng-1", "emp-admin-1")
    gna, oid = _verified(client, db_session)
    cid = _ask(client, gna, oid).json()["correction_id"]

    def decide(i):
        session = _isolated_db()
        try:
            if i == 0:
                corrections.apply(session, cid, "emp-eng-1")
            else:
                corrections.reject(session, cid, "emp-admin-1", "no")
            session.commit()
            return "done"
        except AssociateError as exc:
            session.rollback()
            return exc.code
        finally:
            session.close()

    results, errors = _race(2, decide)
    assert errors == []
    assert sorted(results) == ["correction_not_pending", "done"]
