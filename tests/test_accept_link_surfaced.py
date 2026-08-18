"""
Accept-link token surfacing fix: previously accept_token was generated at send
time but never appeared in any API response or notification payload — staff had
no way to actually retrieve the customer-facing link. Now surfaced via the send
response, proposal detail/list, and the notifier payload — but never to Engineers,
since the token is a bearer credential onto the public accept endpoint, which
returns pricing to anyone holding it.
"""
from unittest.mock import patch

from app.memory.models import PipelineStage
from tests.conftest import admin_headers, engineer_headers, sales_headers


def _sent_proposal(client, memory, db_session, email="accept-surface@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Accept Surface")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={"name": "Accept Surface", "email": email, "project_type": "rooftop",
                    "location": "Pune", "capacity_kw": 6},
        performed_by="test", stage=PipelineStage.qualified,
    )
    p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                     requires_manager_signoff=False, performed_by="emp-1")
    db_session.commit()
    client.post(f"/api/v1/dashboard/proposals/{p.id}/approve", headers=admin_headers())
    r = client.post(f"/api/v1/dashboard/proposals/{p.id}/send", headers=admin_headers())
    return p.id, r


class TestSendResponseIncludesToken:
    def test_send_response_includes_token_and_path(self, client, memory, db_session):
        pid, r = _sent_proposal(client, memory, db_session)
        assert r.status_code == 200
        body = r.json()
        assert body["accept_token"]
        assert body["accept_path"] == f"/api/v1/proposals/accept/{body['accept_token']}"

    def test_notifier_receives_accept_path(self, client, memory, db_session):
        with patch("app.api.routes.dashboard.notifier.proposal_sent") as mock_notify:
            pid, r = _sent_proposal(client, memory, db_session, email="notify-surface@example.com")
        assert r.status_code == 200
        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs["accept_path"].startswith("/api/v1/proposals/accept/")


class TestProposalDetailAndListIncludeToken:
    def test_detail_includes_token_for_admin(self, client, memory, db_session):
        pid, _ = _sent_proposal(client, memory, db_session, email="detail-surface@example.com")
        r = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["accept_token"]
        assert r.json()["accept_path"]

    def test_list_includes_token_for_admin(self, client, memory, db_session):
        pid, _ = _sent_proposal(client, memory, db_session, email="list-surface@example.com")
        r = client.get("/api/v1/dashboard/proposals", headers=admin_headers())
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["id"] == pid)
        assert item["accept_token"]
        assert item["accept_path"]

    def test_engineer_detail_never_sees_accept_token(self, client, memory, db_session):
        pid, _ = _sent_proposal(client, memory, db_session, email="eng-detail-surface@example.com")
        r = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=engineer_headers())
        assert r.status_code == 200
        assert "accept_token" not in r.json()
        assert "accept_path" not in r.json()

    def test_engineer_list_never_sees_accept_token(self, client, memory, db_session):
        pid, _ = _sent_proposal(client, memory, db_session, email="eng-list-surface@example.com")
        r = client.get("/api/v1/dashboard/proposals", headers=engineer_headers())
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["id"] == pid)
        assert "accept_token" not in item
        assert "accept_path" not in item
