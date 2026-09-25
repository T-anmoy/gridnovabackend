"""End to end: a website estimate-form lead reaches a proposal draft once a site
assessment supplies what the form never asks for — and does not when the assessment
has no honest mapping for the project type."""
from app.memory.models import PipelineStage, Proposal, TimelineEventType
from tests.conftest import sales_headers

INPUTS = "/api/v1/proposal-inputs/"


def _estimate(client, email):
    r = client.post("/api/v1/public/estimate-request", json={
        "name": "Meera Sen", "email": email, "phone": "9830000000",
        "state": "West Bengal", "city": "Kolkata", "average_bill": 5200,
    })
    assert r.status_code == 200
    return r.json()["lead_id"]


def _assessment(lead_id, **overrides):
    return {"lead_id": lead_id, "customer_type": "individual", "project_family": "rooftop_solar",
            "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
            "site_type": "rooftop_residential", "connection_type": "on_grid", "desired_output_kw": 5,
            **overrides}


def _lead(memory, db_session, lead_id):
    db_session.expire_all()
    return memory.get_lead(lead_id)


def test_estimate_lead_to_proposal_draft(client, memory, db_session):
    lead_id = _estimate(client, "meera@example.com")
    lead = _lead(memory, db_session, lead_id)
    # Intake is evaluated like a manual edit: some details, not yet qualified.
    assert lead.pipeline_stage == PipelineStage.qualifying
    assert lead.lead_score is not None

    created = client.post(INPUTS, json=_assessment(lead_id), headers=sales_headers()).json()
    assert created["lead_fields_filled"] == {"project_type": "rooftop", "capacity_kw": 5}
    assert _lead(memory, db_session, lead_id).pipeline_stage == PipelineStage.qualified

    review = client.patch(f"{INPUTS}{created['id']}/review", json={"status": "approved_for_proposal"},
                          headers=sales_headers()).json()
    draft = review["proposal_draft"]
    assert draft["triggered"] is True

    proposal = db_session.get(Proposal, draft["proposal_id"])
    assert proposal is not None and proposal.lead_id == lead_id
    lead = _lead(memory, db_session, lead_id)
    assert lead.pipeline_stage == PipelineStage.proposal_draft
    events = [e.event_type for e in memory.get_lead_context(lead_id)["timeline"]]
    for expected in (TimelineEventType.fields_filled_from_inputs, TimelineEventType.qualification_completed,
                     TimelineEventType.proposal_auto_drafted_from_inputs):
        assert expected in events


def test_unmapped_project_family_creates_no_draft(client, memory, db_session):
    lead_id = _estimate(client, "lamps@example.com")
    created = client.post(INPUTS, json=_assessment(lead_id, project_family="street_lighting"),
                          headers=sales_headers()).json()
    assert created["lead_fields_filled"] == {"capacity_kw": 5}

    review = client.patch(f"{INPUTS}{created['id']}/review", json={"status": "approved_for_proposal"},
                          headers=sales_headers()).json()
    assert review["proposal_draft"]["triggered"] is False
    assert review["proposal_draft"]["missing_fields"] == ["project_type"]
    assert db_session.query(Proposal).filter_by(lead_id=lead_id).count() == 0
    assert _lead(memory, db_session, lead_id).pipeline_stage == PipelineStage.qualifying
