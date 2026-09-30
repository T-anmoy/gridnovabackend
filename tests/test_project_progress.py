"""Phase 3A Step 3 — per-project sales progress stages."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.associates.service import AssociateError
from app.memory.models import CustomerOnboarding, ProjectStage, ProjectStageEvent, ProposalStatus
from app.onboarding import progress
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, headers, kyc_ready, make_associate, onboarding_body, onboarding_ready,
    territories, upload_required_documents,
)


def _stages(timeline) -> dict:
    return {s["stage"]: s for s in timeline["stages"]}


def _new(client, db_session, **kw):
    gna = make_associate(db_session)
    r = client.post(BASE, json=onboarding_body(**kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return gna, r.json()


def _verify(client, gna, oid):
    upload_required_documents(client, gna, oid)
    client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
    r = client.post(f"{DASH}/onboardings/{oid}/verification-call",
                    json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
    assert r.status_code == 200


def _assess(client, o, reviewer=SALES):
    r = client.patch(f"/api/v1/proposal-inputs/{o['site_assessment']['proposal_inputs_id']}/review",
                     json={"status": "approved_for_proposal"}, headers=reviewer)
    assert r.status_code == 200, r.text


class TestAutomaticStages:
    def test_registered_on_create(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        t = client.get(f"{BASE}/{o['onboarding_id']}/progress", headers=headers(gna)).json()
        assert t["current_stage"] == "registered" and t["next_stage"] == "verified"
        reg = _stages(t)["registered"]
        assert reg["recorded_by"] == f"associate:{gna}" and reg["evidence"]["onboarding_id"] == o["onboarding_id"]
        assert "portal_agreement_approved" not in _stages(t)  # not a PM Surya Ghar project

    def test_portal_stage_only_for_pm_surya_ghar(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session, scheme_linked=True, scheme_name="pm_surya_ghar")
        t = client.get(f"{BASE}/{o['onboarding_id']}/progress", headers=headers(gna)).json()
        stages = list(_stages(t))
        # Straight after the agreement, before execution begins.
        assert stages[stages.index("agreement_signed") + 1] == "portal_agreement_approved"
        assert stages[stages.index("portal_agreement_approved") + 1] == "feasibility_submitted"

    def test_verified_and_site_assessed(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        _verify(client, gna, o["onboarding_id"])
        _assess(client, o, reviewer=ENG)
        t = client.get(f"{DASH}/onboardings/{o['onboarding_id']}/progress", headers=ADMIN).json()
        assert t["current_stage"] == "site_assessed" and t["next_stage"] == "proposal_requested"
        assert _stages(t)["verified"]["evidence"] == {"verification_call": True, "verified_by": "emp-admin-1"}
        assert _stages(t)["site_assessed"]["recorded_by"] == "emp-eng-1"

    def test_site_assessed_before_verification_is_not_recorded(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        _assess(client, o)  # the review itself still succeeds
        t = client.get(f"{BASE}/{o['onboarding_id']}/progress", headers=headers(gna)).json()
        assert _stages(t)["site_assessed"]["recorded"] is False

    def test_prepared_issued_and_accepted_follow_the_formal_proposal(self, client, db_session, memory,
                                                                     onboarding_ready):
        gna, o = _new(client, db_session)
        _verify(client, gna, o["onboarding_id"])
        _assess(client, o)
        onboarding = db_session.get(CustomerOnboarding, o["onboarding_id"])
        proposal = memory.create_proposal_draft(lead_id=o["lead_id"], content={}, total_value=100000.0,
                                                requires_manager_signoff=False, performed_by="emp-sales-1")
        memory.transition_proposal_status(proposal.id, ProposalStatus.sent, performed_by="emp-sales-1")
        memory.mark_customer_accepted(proposal.id)
        db_session.commit()
        t = progress.timeline(db_session, onboarding)
        assert _stages(t)["proposal_requested"]["evidence"]["requested_by"] == "emp-sales-1"
        assert _stages(t)["proposal_prepared"]["evidence"] == {"proposal_id": proposal.id, "version": 1,
                                                              "drafted_by": "emp-sales-1"}
        assert _stages(t)["proposal_issued"]["evidence"] == {"proposal_id": proposal.id, "version": 1,
                                                            "via": "emailed with the online accept link"}
        assert _stages(t)["accepted"]["recorded_by"] == "customer"
        assert _stages(t)["accepted"]["evidence"]["via"] == "online accept link"
        assert t["next_stage"] == "agreement_signed"

    def test_a_revision_does_not_re_record_prepared(self, client, db_session, memory, onboarding_ready):
        gna, o = _new(client, db_session)
        _verify(client, gna, o["onboarding_id"])
        _assess(client, o)
        for _ in range(2):
            memory.create_proposal_draft(lead_id=o["lead_id"], content={}, total_value=1.0,
                                         requires_manager_signoff=False, performed_by="emp-sales-1")
        db_session.commit()
        rows = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.proposal_prepared)).scalars().all()
        assert len(rows) == 1 and rows[0].evidence["version"] == 1

    def test_non_associate_leads_are_untouched(self, db_session, memory):
        customer = memory.get_or_create_customer("chat@x.test", "Chat")
        lead = memory.create_lead(customer.id)
        proposal = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=1.0,
                                                requires_manager_signoff=False, performed_by="system")
        memory.transition_proposal_status(proposal.id, ProposalStatus.sent, performed_by="system")
        memory.mark_customer_accepted(proposal.id)
        db_session.commit()
        assert db_session.execute(select(ProjectStageEvent)).first() is None


class TestRules:
    def test_order_and_once_only(self, client, db_session, onboarding_ready):
        gna, o = _new(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, o["onboarding_id"])
        with pytest.raises(AssociateError, match="before 'verified'"):
            progress.record(db_session, onboarding, ProjectStage.site_assessed, "x")
        with pytest.raises(AssociateError, match="already recorded"):
            progress.record(db_session, onboarding, ProjectStage.registered, "x")
        with pytest.raises(AssociateError, match="does not apply"):
            progress.record(db_session, onboarding, ProjectStage.portal_agreement_approved, "x")

    def test_database_refuses_a_second_row_for_a_stage(self, client, db_session, onboarding_ready):
        _, o = _new(client, db_session)
        project_id = db_session.get(CustomerOnboarding, o["onboarding_id"]).project_id
        db_session.add(ProjectStageEvent(project_id=project_id, stage=ProjectStage.registered, recorded_by="x",
                                         evidence={}))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestManualStages:
    def _to_accepted_ready(self, client, db_session, memory, **kw):
        gna, o = _new(client, db_session, **kw)
        _verify(client, gna, o["onboarding_id"])
        _assess(client, o)
        proposal = memory.create_proposal_draft(lead_id=o["lead_id"], content={}, total_value=1.0,
                                                requires_manager_signoff=False, performed_by="emp-sales-1")
        memory.transition_proposal_status(proposal.id, ProposalStatus.sent, performed_by="emp-sales-1")
        db_session.commit()
        return o

    def test_only_manual_stages_can_be_recorded_by_hand(self, client, db_session, onboarding_ready):
        _, o = _new(client, db_session)
        base = f"{DASH}/onboardings/{o['onboarding_id']}/progress"
        r = client.post(f"{base}/verified", json={"note": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_recorded_by_its_action"
        assert client.post(f"{base}/nonsense", json={"note": "x"}, headers=ADMIN).status_code == 404

    def test_portal_approval_needs_the_agreement_first(self, client, db_session, memory, onboarding_ready):
        o = self._to_accepted_ready(client, db_session, memory, scheme_linked=True, scheme_name="pm_surya_ghar")
        r = client.post(f"{DASH}/onboardings/{o['onboarding_id']}/progress/portal_agreement_approved",
                        json={"note": "Seen on portal"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_out_of_order"

    def test_associate_progress_is_scoped(self, client, db_session, onboarding_ready):
        _, o = _new(client, db_session)
        other = make_associate(db_session)
        assert client.get(f"{BASE}/{o['onboarding_id']}/progress", headers=headers(other)).status_code == 403
