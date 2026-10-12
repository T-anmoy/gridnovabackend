"""S1 Step 5 — the Engineer's feasibility review: who may record it, what it needs, what
it stores, the revisit loop back to the associate, and the gate it puts on the formal
proposal."""
import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, CustomerOnboarding, OnboardingStatus, ProposalInputs, TimelineEvent
from tests.execution_support import notifications  # noqa: F401 — fixture
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, complete_survey, confirm_site_location, headers, kyc_ready,
    make_associate, onboarding_body, onboarding_ready, review_survey, territories, upload_required_documents,
)


def _submitted(client, db_session, n=1, **kw):
    gna = make_associate(db_session)
    r = client.post(BASE, json=onboarding_body(n=n, **{"customer_email": f"c{n}@example.test", **kw}),
                    headers=headers(gna))
    assert r.status_code == 201, r.text
    oid = r.json()["onboarding_id"]
    upload_required_documents(client, gna, oid)
    confirm_site_location(client, gna, oid)
    complete_survey(client, gna, oid)
    assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
    return gna, oid


def _verified(client, db_session, n=1, **kw):
    gna, oid = _submitted(client, db_session, n=n, **kw)
    assert client.post(f"{DASH}/onboardings/{oid}/verification-call",
                       json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN).status_code == 200
    return gna, oid


def _assessed(client, db_session, n=1, **kw):
    gna, oid = _verified(client, db_session, n=n, **kw)
    inputs_id = db_session.get(CustomerOnboarding, oid).proposal_inputs_id
    r = client.patch(f"/api/v1/proposal-inputs/{inputs_id}/review", json={"status": "approved_for_proposal"},
                     headers=ENG)
    assert r.status_code == 200, r.text
    return gna, oid


def _inputs(db_session, oid) -> ProposalInputs:
    db_session.expire_all()
    return db_session.get(ProposalInputs, db_session.get(CustomerOnboarding, oid).proposal_inputs_id)


def _detail(r):
    return r.json()["detail"]


class TestWhoAndWhen:
    @pytest.mark.parametrize("who", [ENG, ADMIN])
    def test_engineer_and_admin_review(self, client, db_session, onboarding_ready, who):
        _, oid = _submitted(client, db_session)
        r = review_survey(client, oid, who=who)
        assert r.status_code == 200, r.text
        assert r.json()["review"]["outcome"] == "feasible"
        assert r.json()["review"]["reviewed_by"] == who["X-Employee-Id"]

    @pytest.mark.parametrize("who", [SALES, MGMT])
    def test_sales_and_management_only_read(self, client, db_session, onboarding_ready, who):
        _, oid = _submitted(client, db_session)
        assert review_survey(client, oid, who=who).status_code == 403
        assert client.get(f"{DASH}/onboardings/{oid}/survey", headers=who).status_code == 200

    def test_a_draft_is_not_reviewed(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = client.post(BASE, json=onboarding_body(), headers=headers(gna)).json()["onboarding_id"]
        complete_survey(client, gna, oid)
        r = review_survey(client, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_not_submitted"

    def test_an_incomplete_survey_is_not_reviewed(self, client, db_session, onboarding_ready):
        """A grandfathered customer was submitted without a survey — nothing to review."""
        gna = make_associate(db_session)
        oid = client.post(BASE, json=onboarding_body(), headers=headers(gna)).json()["onboarding_id"]
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        db_session.get(CustomerOnboarding, oid).survey_grandfathered = True
        db_session.commit()
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
        r = review_survey(client, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_incomplete"
        assert "bills" in _detail(r)["missing_survey_sections"]
        assert _detail(r)["survey_unsigned"] == ["customer", "associate"]

    def test_a_verified_customer_is_reviewed(self, client, db_session, onboarding_ready):
        _, oid = _verified(client, db_session)
        assert review_survey(client, oid).status_code == 200


class TestValidation:
    @pytest.mark.parametrize("outcome", ["revisit_needed", "not_feasible"])
    def test_notes_required(self, client, db_session, onboarding_ready, outcome):
        _, oid = _submitted(client, db_session)
        extra = {"revisit_items": [{"section": "roof", "note": "x"}]} if outcome == "revisit_needed" else {}
        r = review_survey(client, oid, outcome, notes="   ", **extra)
        assert r.status_code == 422 and _detail(r)["code"] == "notes_required"

    def test_conditions_required_and_only_with_conditions(self, client, db_session, onboarding_ready):
        _, oid = _submitted(client, db_session)
        r = review_survey(client, oid, "feasible_with_conditions", conditions=[" "])
        assert r.status_code == 422 and _detail(r)["code"] == "conditions_required"
        r = review_survey(client, oid, "feasible", conditions=["Reinforce the parapet"])
        assert r.status_code == 422 and _detail(r)["code"] == "not_applicable"

    def test_revisit_items_name_real_sections_and_fields(self, client, db_session, onboarding_ready):
        _, oid = _submitted(client, db_session)
        r = review_survey(client, oid, "revisit_needed", notes="n")
        assert r.status_code == 422 and _detail(r)["code"] == "revisit_items_required"
        r = review_survey(client, oid, "revisit_needed", notes="n", revisit_items=[{"section": "garage", "note": "x"}])
        assert r.status_code == 422 and _detail(r)["code"] == "unknown_section"
        r = review_survey(client, oid, "revisit_needed", notes="n",
                          revisit_items=[{"section": "roof", "field": "discom", "note": "x"}])
        assert r.status_code == 422 and _detail(r)["code"] == "unknown_field"
        # A variant's extra field and a conditional field belong to their sections too.
        r = review_survey(client, oid, "revisit_needed", notes="n",
                          revisit_items=[{"section": "connection", "field": "society_noc_status", "note": "x"},
                                         {"section": "roof", "field": "tenant_noc_available", "note": "y"}])
        assert r.status_code == 200, r.text
        r = review_survey(client, oid, "feasible", revisit_items=[{"section": "roof", "note": "x"}])
        assert r.status_code == 422 and _detail(r)["code"] == "not_applicable"

    def test_bounds(self, client, db_session, onboarding_ready):
        _, oid = _submitted(client, db_session)
        assert review_survey(client, oid, "not_feasible", notes="x" * 2001).status_code == 422
        assert review_survey(client, oid, "feasible_with_conditions", conditions=["x" * 501]).status_code == 422
        assert review_survey(client, oid, "maybe").status_code == 422


class TestRecording:
    def test_stores_audits_and_timelines(self, client, db_session, onboarding_ready, notifications, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", True)
        gna, oid = _submitted(client, db_session)
        r = review_survey(client, oid, "feasible_with_conditions", notes=" Checked ",
                          conditions=[" Reinforce the parapet ", ""])
        assert r.status_code == 200, r.text
        inputs = _inputs(db_session, oid)
        assert inputs.feasibility_outcome.value == "feasible_with_conditions"
        assert inputs.feasibility_notes == "Checked" and inputs.feasibility_conditions == ["Reinforce the parapet"]
        assert inputs.feasibility_reviewed_by_id == "emp-eng-1" and inputs.feasibility_reviewed_at is not None
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "survey_reviewed")).scalar_one()
        assert audit.before_value["outcome"] is None
        assert audit.after_value["outcome"] == "feasible_with_conditions"
        lead_id = db_session.get(CustomerOnboarding, oid).lead_id
        assert db_session.execute(select(TimelineEvent).where(
            TimelineEvent.lead_id == lead_id, TimelineEvent.event_type == "survey_reviewed")).scalar_one()
        mail = [p for p in notifications.of("associate_email") if p["event_type"] == "associate_survey_reviewed"]
        assert len(mail) == 1 and "feasible with conditions" in " ".join(mail[0]["lines"])
        # The associate sees the review, but not who wrote it.
        review = client.get(f"{BASE}/{oid}/survey", headers=headers(gna)).json()["review"]
        assert review["outcome"] == "feasible_with_conditions" and "reviewed_by" not in review

    def test_no_associate_email_when_notifications_are_off(self, client, db_session, onboarding_ready,
                                                           notifications, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", False)
        _, oid = _submitted(client, db_session)
        assert review_survey(client, oid).status_code == 200
        assert notifications.of("associate_email") == []

    def test_re_review_overwrites_and_audit_keeps_before(self, client, db_session, onboarding_ready):
        _, oid = _submitted(client, db_session)
        review_survey(client, oid, "not_feasible", notes="Roof too weak")
        r = review_survey(client, oid, "feasible")
        assert r.status_code == 200
        inputs = _inputs(db_session, oid)
        assert inputs.feasibility_outcome.value == "feasible" and inputs.feasibility_notes is None
        audits = list(db_session.execute(select(AuditLog).where(AuditLog.action == "survey_reviewed")
                                         .order_by(AuditLog.id)).scalars())
        assert audits[1].before_value["outcome"] == "not_feasible"
        assert audits[1].before_value["notes"] == "Roof too weak"


class TestRevisit:
    ITEMS = [{"section": "roof_area", "field": "roof_area_sqft", "note": "Measure the terrace again"}]

    def test_submitted_customer_edits_and_re_enters_the_queue(self, client, db_session, onboarding_ready):
        gna, oid = _submitted(client, db_session)
        assert review_survey(client, oid, "revisit_needed", notes="Area looks off",
                             revisit_items=self.ITEMS).status_code == 200
        home = client.get("/api/v1/associate/home", headers=headers(gna)).json()
        todo = [i for i in home["items"] if i["kind"] == "survey_revisit_needed"]
        assert todo and todo[0]["message_key"] == "todo.survey_revisit_needed"
        assert todo[0]["params"]["via"] == "edit" and todo[0]["params"]["revisit_items"][0]["field"] == "roof_area_sqft"
        # The edit returns the customer to draft and clears the signatures, as any edit does.
        r = client.patch(f"{BASE}/{oid}", json={"roof_area_sqft": 900}, headers=headers(gna))
        assert r.status_code == 200, r.text
        inputs = _inputs(db_session, oid)
        assert inputs.survey_revised_after_review is True
        assert db_session.get(CustomerOnboarding, oid).status == OnboardingStatus.draft
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "updated")
                                   .order_by(AuditLog.id.desc())).scalars().first()
        assert audit.after_value["survey_revised_after_review"] is True
        home = client.get("/api/v1/associate/home", headers=headers(gna)).json()
        assert not [i for i in home["items"] if i["kind"] == "survey_revisit_needed"]
        # Re-signed and re-submitted: reviewable again, and a new review resets the flag.
        complete_survey(client, gna, oid)
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
        assert review_survey(client, oid).status_code == 200
        assert _inputs(db_session, oid).survey_revised_after_review is False

    def test_verified_customer_is_told_to_ask_for_a_correction(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        review_survey(client, oid, "revisit_needed", notes="n", revisit_items=self.ITEMS)
        home = client.get("/api/v1/associate/home", headers=headers(gna)).json()
        todo = next(i for i in home["items"] if i["kind"] == "survey_revisit_needed")
        assert todo["params"]["via"] == "correction"

    def test_a_non_survey_edit_does_not_reopen_the_review(self, client, db_session, onboarding_ready):
        gna, oid = _submitted(client, db_session)
        review_survey(client, oid)
        assert client.patch(f"{BASE}/{oid}", json={"structural_notes": "Old building"},
                            headers=headers(gna)).status_code == 200
        assert _inputs(db_session, oid).survey_revised_after_review is False


class TestFormalProposalGate:
    def _request(self, client, gna, oid):
        return client.post(f"{BASE}/{oid}/request-formal-proposal", headers=headers(gna))

    def test_request_needs_a_passing_review(self, client, db_session, onboarding_ready):
        gna, oid = _assessed(client, db_session)
        r = self._request(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_review_required"
        assert _detail(r)["review_outcome"] is None
        review_survey(client, oid, "revisit_needed", notes="n",
                      revisit_items=[{"section": "roof", "note": "Check the slab"}])
        r = self._request(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["review_outcome"] == "revisit_needed"
        review_survey(client, oid, "feasible_with_conditions", conditions=["Reinforce the parapet"])
        assert self._request(client, gna, oid).status_code == 200

    def test_staff_prepare_needs_a_passing_review(self, client, db_session, onboarding_ready):
        gna, oid = _assessed(client, db_session)
        r = client.post(f"{DASH}/onboardings/{oid}/formal-proposal", headers=SALES)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_review_required"

    def test_generic_draft_route_is_gated_too(self, client, db_session, onboarding_ready):
        gna, oid = _assessed(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, oid)
        r = client.post(f"{DASH}/proposals/draft", json={"lead_id": onboarding.lead_id,
                                                         "customer_id": onboarding.customer_id}, headers=SALES)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_review_required"

    def test_not_feasible_always_blocks(self, client, db_session, onboarding_ready, monkeypatch):
        from app.survey import config as survey_config
        gna, oid = _assessed(client, db_session)
        review_survey(client, oid, "not_feasible", notes="Roof cannot take the load")
        monkeypatch.setitem(survey_config.load(), "review_required_for_formal_proposal", False)
        r = self._request(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["code"] == "survey_not_feasible"

    def test_config_off_and_grandfathered_need_no_review(self, client, db_session, onboarding_ready, monkeypatch):
        from app.survey import config as survey_config
        gna, oid = _assessed(client, db_session)
        db_session.get(CustomerOnboarding, oid).survey_grandfathered = True
        db_session.commit()
        assert self._request(client, gna, oid).status_code == 200
        gna, oid = _assessed(client, db_session, n=2)
        monkeypatch.setitem(survey_config.load(), "review_required_for_formal_proposal", False)
        assert self._request(client, gna, oid).status_code == 200

    def test_a_survey_changed_after_review_needs_a_new_review(self, client, db_session, onboarding_ready):
        gna, oid = _assessed(client, db_session)
        review_survey(client, oid)
        _inputs(db_session, oid).survey_revised_after_review = True
        db_session.commit()
        r = self._request(client, gna, oid)
        assert r.status_code == 409 and _detail(r)["revised_after_review"] is True


def test_config_rejects_an_unknown_reverification_field():
    from app.survey import config as survey_config
    data = dict(survey_config.load())
    data["reverification_fields"] = ["roof_sections", "price"]
    with pytest.raises(survey_config.SurveyConfigError):
        survey_config._validate(data)
