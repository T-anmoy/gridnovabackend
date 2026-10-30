"""Phase 3A (corrected workflow) — the onboarding-time indicative estimate, the formal
proposal as a separate later step, in-person acceptance, the separate approver, and the
agreement."""
from datetime import date, timedelta

from app.associates.service import today_ist
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.memory.models import (
    AuditLog, CustomerAgreement, CustomerOnboarding, IndicativeEstimate, Lead, PipelineStage, ProjectStage,
    ProjectStageEvent, Proposal, ProposalStatus,
)
from app.onboarding import progress
from tests.proposal_document_support import complete_document
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, headers, kyc_ready, make_associate, onboarding_body, onboarding_ready,
    territories, upload_required_documents, complete_survey, confirm_site_location, review_survey,
)

PDF = b"%PDF-1.4\n" + b"%" * 64


def _new(client, db_session, **kw):
    gna = make_associate(db_session)
    o = client.post(BASE, json=onboarding_body(**{"customer_email": "cust@example.test", **kw}), headers=headers(gna))
    assert o.status_code == 201, o.text
    return gna, o.json()


def _assessed(client, db_session, **kw):
    """Verified, with the site assessment approved (by an Engineer — no draft either way)."""
    gna, o = _new(client, db_session, **kw)
    oid = o["onboarding_id"]
    upload_required_documents(client, gna, oid)
    confirm_site_location(client, gna, oid)
    if "connection_phase" in kw:
        complete_survey(client, gna, oid)
    else:
        # No phase chosen: a customer from before the survey (S1) — a survey would
        # need the phase, so it is grandfathered instead of given an invented one.
        from app.memory.models import CustomerOnboarding
        db_session.get(CustomerOnboarding, oid).survey_grandfathered = True
        db_session.commit()
    assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
    assert client.post(f"{DASH}/onboardings/{oid}/verification-call",
                       json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN).status_code == 200
    r = client.patch(f"/api/v1/proposal-inputs/{o['site_assessment']['proposal_inputs_id']}/review",
                     json={"status": "approved_for_proposal"}, headers=ENG)
    assert r.status_code == 200
    if "connection_phase" in kw:   # the Engineer's feasibility review of the survey (S1 Step 5)
        assert review_survey(client, oid).status_code == 200
    return gna, o


def _prepared(client, db_session, **kw):
    gna, o = _assessed(client, db_session, **kw)
    r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal", headers=SALES)
    assert r.status_code == 200, r.text
    return gna, o, r.json()["formal_proposal"]["proposal_id"]


def _approved(client, db_session, **kw):
    gna, o, pid = _prepared(client, db_session, **kw)
    complete_document(client, pid)   # BE-13: what staff fill in before approval
    assert client.post(f"{DASH}/proposals/{pid}/approve", headers=ADMIN).status_code == 200
    return gna, o, pid


def _stages(timeline) -> dict:
    return {s["stage"]: s for s in timeline["stages"]}


def _proposals(db_session):
    return db_session.execute(select(Proposal)).scalars().all()


# ── The onboarding-time indicative estimate ──────────────────────────────────────

class TestIndicativeEstimate:
    def test_prices_the_requested_size_and_keeps_it(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, connection_phase="single_phase")
        r = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["indicative"] is True and body["inputs"]["sizing_method"] == "requested_size"
        assert body["inputs"]["segment"] == "residential"  # from rooftop_residential
        assert body["result"]["recommended_size_kw"] == 3 and body["result"]["net_cost"] > 0
        # The price-list snapshot (PM-A): both packages, the version and the chosen package(s).
        assert (body["price_status"], body["priced_kw"], body["price_list_version"]) == ("list_price", 3, 1)
        assert body["result"]["packages"]["NOVA_ASSURED"]["final"] == "201465.00"
        assert body["result"]["packages"]["NOVA_SIGNATURE"]["final"] == "225640.80"
        assert body["chosen_packages"] == ["NOVA_ASSURED", "NOVA_SIGNATURE"]   # no package chosen
        assert db_session.execute(select(IndicativeEstimate)).scalar_one().created_by == f"associate:{gna}"
        assert db_session.execute(select(AuditLog).where(AuditLog.entity_type == "indicative_estimate")).scalar_one()
        # Not a proposal, and not a progress stage.
        assert _proposals(db_session) == []
        stages = {e.stage for e in db_session.execute(select(ProjectStageEvent)).scalars()}
        assert stages == {ProjectStage.registered}

    def test_same_numbers_as_the_public_solar_calculator(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, desired_output_kw=None, avg_monthly_units_kwh=400,
                      connection_phase="single_phase")
        mine = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna)).json()
        public = client.post("/api/v1/pricing/instant-estimate", json={
            "segment": "residential", "state": "WEST BENGAL", "sizing_method": "by_units",
            "monthly_units_kwh": 400, "connection_phase": "single_phase"}).json()
        assert mine["inputs"]["sizing_method"] == "by_units"
        assert mine["result"]["recommended_size_kw"] == public["recommended_size_kw"]
        assert mine["result"]["net_cost"] == public["net_cost"] is not None
        assert mine["result"]["packages"] == public["packages"]

    @pytest.mark.parametrize("phase,kw,reason", [("single_phase", 3.5, "non_integer_size"),
                                                 ("single_phase", 11, "size_above_max"),
                                                 ("three_phase", 2, "three_phase_below_min")])
    def test_requested_size_must_be_in_the_price_list(self, client, db_session, onboarding_ready, phase, kw, reason):
        gna, o = _new(client, db_session, connection_phase=phase, desired_output_kw=kw, sanctioned_load_kw=20)
        r = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "unsupported_size"
        assert r.json()["detail"]["reason"] == reason

    def test_requested_size_outside_the_price_list_scope_is_a_custom_quote(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, connection_phase="single_phase", desired_output_kw=15,
                      sanctioned_load_kw=20, connection_type="hybrid", battery_storage=True)
        body = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna)).json()
        assert body["price_status"] == "custom_quote" and "battery_or_off_grid" in body["result"]["custom_quote_reasons"]

    @pytest.mark.parametrize("package,expected", [("nova_assured", ["NOVA_ASSURED"]),
                                                  ("nova_signature", ["NOVA_SIGNATURE"]),
                                                  ("as_recommended", ["NOVA_ASSURED", "NOVA_SIGNATURE"])])
    def test_chosen_package_comes_from_the_onboarding(self, client, db_session, onboarding_ready, package, expected):
        gna, o = _new(client, db_session, connection_phase="single_phase", equipment_package=package)
        body = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna)).json()
        assert body["chosen_packages"] == expected and set(body["result"]["packages"]) == {"NOVA_ASSURED",
                                                                                          "NOVA_SIGNATURE"}

    def test_an_estimate_from_before_the_price_list_stays_readable(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        old = IndicativeEstimate(onboarding_id=o["onboarding_id"], inputs={"segment": "residential"},
                                 result={"recommended_size_kw": 3.0, "net_cost": 123456.0}, created_by="x")
        db_session.add(old)
        db_session.commit()
        [item] = client.get(f"{BASE}/{o['onboarding_id']}/estimates", headers=headers(gna)).json()["items"]
        assert item["result"] == {"recommended_size_kw": 3.0, "net_cost": 123456.0}
        assert item["price_status"] is None and item["price_list_version"] is None

    def test_segment_must_be_chosen_when_the_site_type_does_not_say(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, site_type="ground_mounted")
        url = f"{BASE}/{o['onboarding_id']}/estimates"
        r = client.post(url, json={}, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "segment_required"
        assert client.post(url, json={"segment": "farm"}, headers=headers(gna)).json()["detail"]["code"] == "invalid_segment"
        assert client.post(url, json={"segment": "commercial"}, headers=headers(gna)).status_code == 201

    def test_needs_something_to_size_from(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, desired_output_kw=None, avg_monthly_units_kwh=None)
        r = client.post(f"{BASE}/{o['onboarding_id']}/estimates", json={}, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "not_enough_to_size"

    def test_history_and_visibility(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        url = f"{BASE}/{o['onboarding_id']}/estimates"
        client.post(url, json={}, headers=headers(gna))
        client.post(url, json={"access_difficulty": "difficult"}, headers=headers(gna))
        assert len(client.get(url, headers=headers(gna)).json()["items"]) == 2
        assert len(client.get(f"{DASH}/onboardings/{o['onboarding_id']}/estimates", headers=MGMT).json()["items"]) == 2
        head = make_associate(db_session, level="state_head", territory=19)
        assert client.get(url, headers=headers(head)).status_code == 200
        assert client.post(url, json={}, headers=headers(head)).status_code == 403


# ── Requesting and preparing the formal proposal ─────────────────────────────────

class TestFormalProposal:
    def test_assessment_approval_never_drafts_for_associate_customers(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        oid = o["onboarding_id"]
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        client.post(f"{DASH}/onboardings/{oid}/verification-call",
                    json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
        r = client.patch(f"/api/v1/proposal-inputs/{o['site_assessment']['proposal_inputs_id']}/review",
                         json={"status": "approved_for_proposal"}, headers=SALES)
        assert r.json()["proposal_draft"]["triggered"] is False
        assert "prepared separately" in r.json()["proposal_draft"]["reason"]
        assert _proposals(db_session) == []

    def test_associate_request_records_but_does_not_draft(self, client, db_session, onboarding_ready):
        gna, o = _assessed(client, db_session)
        r = client.post(f"{BASE}/{o['onboarding_id']}/request-formal-proposal", headers=headers(gna))
        assert r.status_code == 200 and r.json()["request"] == {"ready": True, "missing_fields": []}
        assert r.json()["progress"]["current_stage"] == "proposal_requested"
        assert _proposals(db_session) == []
        again = client.post(f"{BASE}/{o['onboarding_id']}/request-formal-proposal", headers=headers(gna))
        assert again.status_code == 409 and again.json()["detail"]["code"] == "already_requested"

    def test_request_rules(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        r = client.post(f"{BASE}/{o['onboarding_id']}/request-formal-proposal", headers=headers(gna))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_assessed"
        gna, o = _assessed(client, db_session, n=2, customer_email=None)
        head = make_associate(db_session, level="state_head", territory=19)
        url = f"{BASE}/{o['onboarding_id']}/request-formal-proposal"
        assert client.post(url, headers=headers(head)).status_code == 403
        r = client.post(url, headers=headers(gna))
        assert r.json()["request"] == {"ready": False, "missing_fields": ["email"]}

    def test_staff_prepare_after_the_request(self, client, db_session, onboarding_ready):
        gna, o = _assessed(client, db_session)
        client.post(f"{BASE}/{o['onboarding_id']}/request-formal-proposal", headers=headers(gna))
        r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal", headers=SALES)
        assert r.status_code == 200, r.text
        prepared = r.json()["formal_proposal"]
        proposal = db_session.get(Proposal, prepared["proposal_id"])
        assert proposal.drafted_by == "emp-sales-1" and prepared["status"] == "draft"
        stages = _stages(r.json()["progress"])
        assert stages["proposal_requested"]["recorded_by"] == f"associate:{gna}"
        assert stages["proposal_prepared"]["evidence"] == {"proposal_id": proposal.id, "version": 1,
                                                          "drafted_by": "emp-sales-1"}
        assert r.json()["progress"]["next_stage"] == "proposal_issued"
        # The associate sees it, without a price or PDF until it is approved.
        seen = client.get(f"{BASE}/{o['onboarding_id']}/formal-proposal", headers=headers(gna)).json()
        assert seen["prepared"] and seen["status"] == "draft" and seen["total_value"] is None and seen["pdf"] is None
        assert seen["ready_to_hand_over"] is False

    def test_staff_can_prepare_without_an_associate_request(self, client, db_session, onboarding_ready):
        gna, o, pid = _prepared(client, db_session)
        t = progress.timeline(db_session, db_session.get(CustomerOnboarding, o["onboarding_id"]))
        assert _stages(t)["proposal_requested"]["evidence"] == {
            "requested_by": "emp-sales-1", "via": "staff drafted the formal proposal"}

    def test_prepare_rules(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        url = f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal"
        assert client.post(url, headers=SALES).json()["detail"]["code"] == "not_assessed"
        gna, o = _assessed(client, db_session, n=2, customer_email=None)
        url = f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal"
        r = client.post(url, headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "formal_proposal_not_ready"
        assert "email" in r.json()["detail"]["message"]
        for role in (ENG, MGMT):
            assert client.post(url, headers=role).status_code == 403
        gna, o, _ = _prepared(client, db_session, n=3)
        r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/formal-proposal", headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_prepared"


class TestSeparateApprover:
    def test_drafter_cannot_approve_an_associate_proposal(self, client, db_session, onboarding_ready):
        gna, o, pid = _prepared(client, db_session)
        r = client.post(f"{DASH}/proposals/{pid}/approve", headers=SALES)
        assert r.status_code == 403 and "different employee" in r.json()["detail"]
        complete_document(client, pid)
        assert client.post(f"{DASH}/proposals/{pid}/approve", headers=ADMIN).status_code == 200

    @pytest.mark.parametrize("all_leads, expected", [(False, 200), (True, 403)])
    def test_website_lead_self_approval_follows_the_setting(self, client, db_session, memory, onboarding_ready,
                                                            monkeypatch, all_leads, expected):
        """BE-1 C9: require_different_approver_for_all_leads (config/sales_policy.yaml)."""
        from app.rules import sales_policy
        monkeypatch.setattr(sales_policy, "require_different_approver_for_all_leads", lambda: all_leads)
        customer = memory.get_or_create_customer(f"self{int(all_leads)}@example.test", "Self Approver")
        lead = memory.create_lead(customer.id)
        from app.memory.lead_fields import apply_lead_fields
        apply_lead_fields(memory, lead, {"name": "Self", "email": f"self{int(all_leads)}@example.test",
                                         "project_type": "residential", "location": "Kolkata", "capacity_kw": 3},
                          performed_by="emp-sales-1", via="test")
        db_session.commit()
        pid = client.post(f"{DASH}/proposals/draft", json={"lead_id": lead.id, "customer_id": customer.id},
                          headers=SALES).json()["proposal_id"]
        complete_document(client, pid)
        r = client.post(f"{DASH}/proposals/{pid}/approve", headers=SALES)
        assert r.status_code == expected, r.text
        if expected == 403:
            assert r.json()["detail"] == "You drafted this proposal. A different employee must approve it."
            assert client.post(f"{DASH}/proposals/{pid}/approve", headers=ADMIN).status_code == 200

    def test_non_associate_proposals_are_unchanged(self, client, db_session, memory, onboarding_ready):
        customer = memory.get_or_create_customer("walkin@example.test", "Walk In")
        lead = memory.create_lead(customer.id)
        from app.memory.lead_fields import apply_lead_fields
        apply_lead_fields(memory, lead, {"name": "Walk In", "email": "walkin@example.test",
                                         "project_type": "residential", "location": "Kolkata", "capacity_kw": 3},
                          performed_by="emp-sales-1", via="test")
        db_session.commit()
        pid = client.post(f"{DASH}/proposals/draft", json={"lead_id": lead.id, "customer_id": customer.id},
                          headers=SALES).json()["proposal_id"]
        complete_document(client, pid)
        assert client.post(f"{DASH}/proposals/{pid}/approve", headers=SALES).status_code == 200
        assert db_session.execute(select(ProjectStageEvent)).first() is None


# ── Accepting: online or in person ───────────────────────────────────────────────

def _in_person(client, who_headers, oid, pid, **overrides):
    body = {"proposal_id": pid, "delivered_on": today_ist().isoformat(),
            "note": "Explained the proposal at the customer's home; customer agreed.",
            "reference": "Printed copy v1 handed over, verbal acknowledgement", **overrides}
    base = BASE if "X-Associate-Id" in who_headers else DASH + "/onboardings"
    return client.post(f"{base}/{oid}/proposal/in-person-acceptance", json=body, headers=who_headers)


class TestAcceptance:
    def test_in_person_by_the_associate(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, pid = _approved(client, db_session)
        seen = client.get(f"{BASE}/{o['onboarding_id']}/formal-proposal", headers=headers(gna)).json()
        assert seen["ready_to_hand_over"] is True and seen["total_value"] > 0
        r = _in_person(client, headers(gna), o["onboarding_id"], pid)
        assert r.status_code == 200, r.text
        db_session.expire_all()
        proposal = db_session.get(Proposal, pid)
        assert proposal.status == ProposalStatus.accepted and proposal.date_sent.date() == today_ist()
        stages = _stages(r.json()["progress"])
        assert stages["proposal_issued"]["evidence"]["via"] == "handed over in person"
        assert stages["accepted"]["evidence"]["via"] == "in person"
        assert stages["accepted"]["evidence"]["reference"] == "Printed copy v1 handed over, verbal acknowledgement"
        assert stages["accepted"]["recorded_by"] == f"associate:{gna}"
        assert r.json()["progress"]["next_stage"] == "agreement_signed"
        assert db_session.get(Lead, o["lead_id"]).pipeline_stage == PipelineStage.won
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "customer_accepted")).scalar_one()
        assert audit.performed_by == f"associate:{gna}" and audit.after_value["via"] == "in person"

    def test_in_person_by_staff(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, pid = _approved(client, db_session)
        r = _in_person(client, SALES, o["onboarding_id"], pid)
        assert r.status_code == 200 and _stages(r.json()["progress"])["accepted"]["recorded_by"] == "emp-sales-1"
        assert _in_person(client, ENG, o["onboarding_id"], pid).status_code == 403

    def test_online_path_says_so(self, client, db_session, memory, sendable_proposal_content, onboarding_ready):
        gna, o, pid = _approved(client, db_session)
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=SALES).status_code == 200
        memory.mark_customer_accepted(pid)
        db_session.commit()
        t = progress.timeline(db_session, db_session.get(CustomerOnboarding, o["onboarding_id"]))
        assert _stages(t)["proposal_issued"]["evidence"]["via"] == "emailed with the online accept link"
        assert _stages(t)["accepted"]["evidence"]["via"] == "online accept link"
        assert t["next_stage"] == "agreement_signed"

    def test_sent_online_then_accepted_in_person(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, pid = _approved(client, db_session)
        client.post(f"{DASH}/proposals/{pid}/send", headers=SALES)
        r = _in_person(client, headers(gna), o["onboarding_id"], pid)
        stages = _stages(r.json()["progress"])
        assert stages["proposal_issued"]["evidence"]["via"] == "emailed with the online accept link"
        assert stages["accepted"]["evidence"]["via"] == "in person"

    def test_in_person_twice_and_after_another_acceptance(self, client, db_session, memory,
                                                           sendable_proposal_content, onboarding_ready):
        """BE-1 S5: the second in-person acceptance is refused, and once one proposal
        on the customer is accepted no later version can be accepted in person."""
        gna, o, pid = _approved(client, db_session)
        assert _in_person(client, headers(gna), o["onboarding_id"], pid).status_code == 200
        r = _in_person(client, headers(gna), o["onboarding_id"], pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_accepted"
        v1 = db_session.get(Proposal, pid)
        v2 = memory.create_proposal_draft(lead_id=o["lead_id"], content=v1.content, total_value=v1.total_value,
                                          requires_manager_signoff=False, performed_by="emp-sales-1")
        memory.transition_proposal_status(v2.id, ProposalStatus.approved, performed_by="emp-admin-1",
                                          approved_by_employee_id="emp-admin-1")
        db_session.commit()
        r = _in_person(client, headers(gna), o["onboarding_id"], v2.id)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_superseded"

    def test_guards(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, pid = _prepared(client, db_session)
        oid = o["onboarding_id"]
        r = _in_person(client, headers(gna), oid, pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_approved"  # still a draft
        complete_document(client, pid)
        client.post(f"{DASH}/proposals/{pid}/approve", headers=ADMIN)
        assert _in_person(client, headers(gna), oid, pid, reference="").status_code == 422
        r = _in_person(client, headers(gna), oid, pid, reference="   ")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "reference_required"
        r = _in_person(client, headers(gna), oid, pid, delivered_on=(today_ist() + timedelta(days=1)).isoformat())
        assert r.json()["detail"]["code"] == "invalid_delivered_on"
        r = _in_person(client, headers(gna), oid, pid, delivered_on=(today_ist() - timedelta(days=40)).isoformat())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "expired"
        assert _in_person(client, headers(gna), oid, 99999).status_code == 404
        head = make_associate(db_session, level="state_head", territory=19)
        assert _in_person(client, headers(head), oid, pid).status_code == 403
        assert _in_person(client, headers(gna), oid, pid).status_code == 200
        r = _in_person(client, headers(gna), oid, pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_accepted"

    def test_send_guard_applies_in_person_too(self, client, db_session, onboarding_ready):
        """The guard that stops the emailed send stops in-person acceptance too. A BE-13
        document cannot be approved incomplete, so the gap is made after approval."""
        gna, o, pid = _approved(client, db_session)
        proposal = db_session.get(Proposal, pid)
        content = dict(proposal.content)
        content["document"] = {**content["document"],
                               "addressee": {**content["document"]["addressee"], "name": None}}
        proposal.content = content
        db_session.commit()
        r = _in_person(client, headers(gna), o["onboarding_id"], pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "missing_required"
        assert r.json()["detail"]["missing"] == [{"field": "addressee.name", "message": "the customer's name"}]

    def test_accepted_can_no_longer_be_hand_recorded(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/progress/accepted", json={"note": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_recorded_by_its_action"


# ── The agreement (after acceptance, on the formal proposal only) ────────────────

class TestAgreement:
    def _accepted(self, client, db_session):
        gna, o, pid = _approved(client, db_session)
        assert _in_person(client, headers(gna), o["onboarding_id"], pid).status_code == 200
        return gna, o, pid

    def test_generate_needs_acceptance(self, client, db_session, onboarding_ready):
        gna, o = _assessed(client, db_session)
        r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/agreement", headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_accepted"

    def test_full_lifecycle(self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, pid = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            assert client.post(f"{DASH}/onboardings/{oid}/agreement", headers=MGMT).status_code == 403
            r = client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES)
        assert r.status_code == 200 and r.json()["generated"] and r.json()["proposal_id"] == pid
        seen = client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json()
        assert seen["generated"] is True and "agreement_pdf" in seen["links"]
        url = f"{BASE}/{oid}/agreement/signed"
        bad = client.post(url, data={"stamp_paper_number": "WB 123/45", "stamp_paper_date": "2999-01-01"},
                          files={"file": ("s.pdf", PDF, "application/pdf")}, headers=headers(gna))
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_stamp_paper_date"
        bad = client.post(url, data={"stamp_paper_number": "!!", "stamp_paper_date": "2026-09-01"},
                          files={"file": ("s.pdf", PDF, "application/pdf")}, headers=headers(gna))
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_stamp_paper_number"
        ok = client.post(url, data={"stamp_paper_number": "WB 123/45", "stamp_paper_date": "2026-09-01"},
                         files={"file": ("signed.pdf", PDF, "application/pdf")}, headers=headers(gna))
        assert ok.status_code == 200 and ok.json()["signed"] and ok.json()["links"]["signed_scan"]["url"]
        t = progress.timeline(db_session, db_session.get(CustomerOnboarding, oid))
        assert _stages(t)["agreement_signed"]["evidence"]["stamp_paper_number"] == "WB 123/45"
        # Recording the signed agreement starts execution (Phase 4A); this associate
        # has the sales capability, so Grid Nova's own team executes.
        from app.memory.models import ProjectExecution
        record = db_session.execute(select(ProjectExecution)).scalar_one()
        assert record.onboarding_id == oid and record.execution_mode.value == "grid_nova"
        again = client.post(url, data={"stamp_paper_number": "X1", "stamp_paper_date": "2026-09-01"},
                            files={"file": ("s.pdf", PDF, "application/pdf")}, headers=headers(gna))
        assert again.status_code == 409
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            r = client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_signed"

    def test_staff_upload_and_database_rule(self, client, db_session, sendable_proposal_content, onboarding_ready):
        from sqlalchemy.exc import IntegrityError
        gna, o, _ = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            client.post(f"{DASH}/onboardings/{oid}/agreement", headers=ADMIN)
        row = db_session.execute(select(CustomerAgreement)).scalar_one()
        row.signed_scan_key = "x"
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()
        r = client.post(f"{DASH}/onboardings/{oid}/agreement/signed",
                        data={"stamp_paper_number": "A1", "stamp_paper_date": "2026-09-02"},
                        files={"file": ("s.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["signed_uploaded_by"] == "emp-admin-1"

    def test_agreement_html(self, client, db_session, sendable_proposal_content, onboarding_ready):
        from app.onboarding import agreement
        gna, o, pid = self._accepted(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, o["onboarding_id"])
        html = agreement.agreement_html(db_session, onboarding, db_session.get(Proposal, pid), date(2026, 9, 27))
        assert "1. Scope. Grid Nova will design, supply, install, test and commission the System" in html
        assert "[PLACEHOLDER" not in html
        assert "Customer 1" in html and "CN-000001" in html and "margin-top: 100.0mm" in html

    def test_real_pdf_render(self, client, db_session, sendable_proposal_content, onboarding_ready):
        try:
            import weasyprint  # noqa: F401
        except OSError:
            pytest.skip("WeasyPrint native libraries (pango/gobject) not loadable here")
        from app.associates.documents import render_pdf
        from app.onboarding import agreement
        gna, o, pid = self._accepted(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, o["onboarding_id"])
        pdf = render_pdf(agreement.agreement_html(db_session, onboarding, db_session.get(Proposal, pid),
                                                  date(2026, 9, 27)))
        assert pdf.startswith(b"%PDF-")


def _approve_agreement_wording(client, legal=True):
    from app.placeholders import registry
    entry = registry.load().by_id["ONB-5"]
    return client.post("/api/v1/dashboard/placeholders/ONB-5/approve", headers=ADMIN,
                       json={"content_sha256": registry.sha256(registry.current_text(entry)),
                             "legal_review_confirmed": legal})


@pytest.mark.launch_texts_unapproved
class TestAgreementWording:
    """BE-22: no agreement PDF is produced, served or signed from wording an Admin has
    not approved exactly as it stands (registry ONB-5)."""

    def _accepted(self, client, db_session):
        gna, o, pid = _approved(client, db_session)
        assert _in_person(client, headers(gna), o["onboarding_id"], pid).status_code == 200
        return gna, o, pid

    def test_no_pdf_while_the_wording_is_not_approved(self, client, db_session, sendable_proposal_content,
                                                      onboarding_ready):
        gna, o, _ = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        with patch("app.associates.documents.render_pdf") as render:
            r = client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agreement_wording_not_approved"
        render.assert_not_called()
        assert db_session.execute(select(CustomerAgreement)).first() is None
        seen = client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json()
        assert seen == {"generated": False, "signed": False, "wording_approved": False, "links": {}}

    def test_without_the_legal_tick_it_stays_refused(self, client, db_session, sendable_proposal_content,
                                                     onboarding_ready):
        _, o, _ = self._accepted(client, db_session)
        assert _approve_agreement_wording(client, legal=False).status_code == 422
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/agreement", headers=SALES)
        assert r.status_code == 409

    def test_approved_wording_generates_and_records_its_hash(self, client, db_session, sendable_proposal_content,
                                                             onboarding_ready):
        from app.placeholders import registry
        gna, o, _ = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        assert _approve_agreement_wording(client).status_code == 200
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement") as render:
            r = client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES)
        assert r.status_code == 200 and r.json()["wording_approved"] is True and "agreement_pdf" in r.json()["links"]
        html = render.call_args.args[0]
        assert "13. Whole agreement." in html and "[PLACEHOLDER" not in html
        record = db_session.execute(select(CustomerAgreement)).scalar_one()
        entry = registry.load().by_id["ONB-5"]
        assert record.wording_sha256 == registry.sha256(registry.current_text(entry))
        seen = client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json()
        assert "agreement_pdf" in seen["links"] and seen["needs_regeneration"] is False

    def test_revoked_wording_withholds_the_link_and_refuses_the_signed_copy(
            self, client, db_session, sendable_proposal_content, onboarding_ready):
        gna, o, _ = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        _approve_agreement_wording(client)
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            assert client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES).status_code == 200
        r = client.post("/api/v1/dashboard/placeholders/ONB-5/revoke", headers=ADMIN, json={"note": "Counsel edits."})
        assert r.status_code == 200
        for seen in (client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json(),
                     client.get(f"{DASH}/onboardings/{oid}/agreement", headers=ADMIN).json()):
            assert seen["links"] == {} and seen["needs_regeneration"] is True and seen["wording_approved"] is False
        up = client.post(f"{BASE}/{oid}/agreement/signed",
                         data={"stamp_paper_number": "WB 1", "stamp_paper_date": "2026-09-01"},
                         files={"file": ("signed.pdf", PDF, "application/pdf")}, headers=headers(gna))
        assert up.status_code == 409 and up.json()["detail"]["code"] == "agreement_wording_not_approved"
        assert db_session.execute(select(CustomerAgreement)).scalar_one().signed_scan_key is None

    def test_an_agreement_printed_before_approval_must_be_regenerated(self, client, db_session,
                                                                      sendable_proposal_content, onboarding_ready):
        gna, o, _ = self._accepted(client, db_session)
        oid = o["onboarding_id"]
        _approve_agreement_wording(client)
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES)
        record = db_session.execute(select(CustomerAgreement)).scalar_one()
        record.wording_sha256 = None              # generated before BE-22, from the placeholder
        db_session.commit()
        assert client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json()["links"] == {}
        with patch("app.associates.documents.render_pdf", return_value=b"%PDF-agreement"):
            assert client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES).status_code == 200
        assert "agreement_pdf" in client.get(f"{BASE}/{oid}/agreement", headers=headers(gna)).json()["links"]

    def test_changed_wording_lapses_the_approval(self, client, db_session, sendable_proposal_content,
                                                 onboarding_ready, monkeypatch):
        from app.onboarding import config as onboarding_config
        _, o, _ = self._accepted(client, db_session)
        _approve_agreement_wording(client)
        cfg = onboarding_config.load()["agreement"]
        monkeypatch.setitem(cfg, "body", cfg["body"] + "\n14. A clause nobody approved.")
        with patch("app.associates.documents.render_pdf") as render:
            r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/agreement", headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agreement_wording_not_approved"
        render.assert_not_called()
