"""Placeholder send-guard: a proposal whose stored sections still carry placeholder
copy, or that has no real price, can be approved internally but never sent to a
customer. adjust-pricing is how staff set a real price, and it clears that part."""
import pytest

from app.memory.models import PipelineStage, ProposalStatus
from tests.conftest import admin_headers, engineer_headers

DASH = "/api/v1/dashboard"


def _lead(memory, db_session, *, email="guard@example.com", **fields):
    customer = memory.get_or_create_customer(email=email, name="G")
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={"name": "G", "email": email, "project_type": "rooftop", "segment": "residential",
                    "location": "Kolkata", "capacity_kw": 6, **fields},
        performed_by="t", stage=PipelineStage.qualified,
    )
    db_session.commit()
    return customer.id, lead.id


def _draft(client, memory, db_session, **fields):
    cid, lid = _lead(memory, db_session, **fields)
    r = client.post(f"{DASH}/proposals/draft", json={"lead_id": lid, "customer_id": cid}, headers=admin_headers())
    assert r.status_code == 200, r.text
    return lid, r.json()["proposal_id"]


def _detail(client, pid):
    return client.get(f"{DASH}/proposals/{pid}", headers=admin_headers()).json()


class TestWithTheRealPlaceholderConfig:
    def test_draft_is_flagged_on_detail_and_summary(self, client, memory, db_session):
        _, pid = _draft(client, memory, db_session)
        detail = _detail(client, pid)
        assert detail["has_placeholder_content"] is True
        assert detail["total_value"] is None
        stored = [s["id"] for s in detail["content"]["sections"] if "[PLACEHOLDER" in s["body"]]
        assert detail["placeholder_sections"] == stored and stored
        # The real rooftop tier matches at a 0 rate: the engine writes "Pricing pending"
        # (no marker) and leaves the price unset — blocked on the price, not the copy.
        assert "pricing" not in stored

        row = next(r for r in client.get(f"{DASH}/proposals", headers=admin_headers()).json()["items"]
                   if r["id"] == pid)
        assert row["has_placeholder_content"] is True
        assert row["placeholder_sections"] == stored

    def test_approval_still_works_but_send_is_blocked(self, client, memory, db_session):
        lid, pid = _draft(client, memory, db_session)
        assert client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers()).status_code == 200

        r = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "placeholder_content"
        assert detail["missing_total_value"] is True
        assert detail["placeholder_sections"] == _detail(client, pid)["placeholder_sections"]
        for section in detail["placeholder_sections"]:
            assert section in detail["message"]
        assert "no price set" in detail["message"]

        db_session.expire_all()
        proposal = memory.get_proposal(pid)
        assert proposal.status == ProposalStatus.approved
        assert proposal.accept_token is None and proposal.date_sent is None
        assert memory.get_lead(lid).pipeline_stage == PipelineStage.proposal_draft

    def test_a_real_price_alone_does_not_unblock_placeholder_copy(self, client, memory, db_session):
        _, pid = _draft(client, memory, db_session)
        client.patch(f"{DASH}/proposals/{pid}/adjust-pricing",
                     json={"new_value": 300_000, "reason": "surveyed"}, headers=admin_headers())
        client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers())
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["missing_total_value"] is False
        assert "pricing" not in detail["placeholder_sections"]
        assert detail["placeholder_sections"]


@pytest.mark.usefixtures("sendable_proposal_content")
class TestWithRealContent:
    def test_real_copy_and_engine_price_can_be_sent(self, client, memory, db_session):
        lid, pid = _draft(client, memory, db_session)
        detail = _detail(client, pid)
        assert detail["total_value"] == 300_000  # 6 kW × the fixture's 50,000/kW
        assert detail["has_placeholder_content"] is False and detail["placeholder_sections"] == []
        client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers())
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).status_code == 200

    def test_adjust_pricing_clears_the_missing_price(self, client, memory, db_session):
        # solar_pump matches no pricing tier → the engine leaves the price unset.
        _, pid = _draft(client, memory, db_session, project_type="solar_pump", segment=None)
        before = _detail(client, pid)
        assert before["total_value"] is None
        assert before["placeholder_sections"] == ["pricing"]

        r = client.patch(f"{DASH}/proposals/{pid}/adjust-pricing",
                         json={"new_value": 180_000, "reason": "vendor quote"}, headers=admin_headers())
        assert r.status_code == 200
        after = _detail(client, pid)
        pricing = next(s for s in after["content"]["sections"] if s["id"] == "pricing")
        assert pricing["body"] == "Proposed project value: Rs 1,80,000.00."
        assert pricing["total_value"] == 180_000 and after["content"]["pricing"]["total_value"] == 180_000
        assert after["has_placeholder_content"] is False

        client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers())
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).status_code == 200

    def test_adjusted_price_is_audited_with_content_unchanged_elsewhere(self, client, memory, db_session):
        _, pid = _draft(client, memory, db_session, project_type="solar_pump", segment=None)
        before = _detail(client, pid)["content"]["sections"]
        client.patch(f"{DASH}/proposals/{pid}/adjust-pricing",
                     json={"new_value": 180_000, "reason": "vendor quote"}, headers=admin_headers())
        after = _detail(client, pid)["content"]["sections"]
        assert [s for s in after if s["id"] != "pricing"] == [s for s in before if s["id"] != "pricing"]
        assert any(l.action == "pricing_adjusted"
                   for l in memory.list_audit_logs(entity_type="proposal", entity_id=str(pid)))


@pytest.mark.usefixtures("known_employees")
class TestEdgeCases:
    def _approved(self, memory, db_session, content, total_value):
        _, lid = _lead(memory, db_session, email=f"edge{total_value}@example.com")
        p = memory.create_proposal_draft(lead_id=lid, content=content, total_value=total_value,
                                         requires_manager_signoff=False, performed_by="emp-1")
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="emp-1", approved_by_employee_id="emp-admin-1")
        db_session.commit()
        return p.id

    def test_zero_price_is_blocked(self, client, memory, db_session):
        pid = self._approved(memory, db_session, {"sections": [{"id": "scope", "category": "scope", "body": "8kW"}]}, 0)
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["missing_total_value"] is True

    def test_content_without_sections_and_a_real_price_can_be_sent(self, client, memory, db_session):
        pid = self._approved(memory, db_session, {}, 120_000)
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).status_code == 200

    def test_engineer_view_is_unchanged(self, client, memory, db_session):
        pid = self._approved(memory, db_session, {}, 120_000)
        body = client.get(f"{DASH}/proposals/{pid}", headers=engineer_headers()).json()
        assert "has_placeholder_content" not in body and "content" not in body
