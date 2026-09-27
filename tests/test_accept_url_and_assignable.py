"""Prompt 02c — two additive contract gaps the dashboard build (Prompt 03) surfaced:

1. accept_url: proposal responses only ever exposed accept_path (the backend's own
   API path), never the full website link the customer actually opens. The frontend
   has been reading a field the backend never sent.
2. GET /employees/assignable: PATCH /leads/{id}/assign allows Admin + Management, but
   GET /employees is Admin-only, so Management had no way to see who it could assign to.
"""
from app.config import settings
from app.memory.models import EmployeeRole, PipelineStage
from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers

SUB = {"X-Employee-Id": "emp-sub-1", "X-Employee-Role": "operations"}


def _sent_proposal(client, memory, db_session, email="accept-url@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Accept Url")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={"name": "Accept Url", "email": email, "project_type": "rooftop",
                    "location": "Pune", "capacity_kw": 6},
        performed_by="test", stage=PipelineStage.qualified,
    )
    p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                     requires_manager_signoff=False, performed_by="emp-1")
    db_session.commit()
    client.post(f"/api/v1/dashboard/proposals/{p.id}/approve", headers=admin_headers())
    r = client.post(f"/api/v1/dashboard/proposals/{p.id}/send", headers=admin_headers())
    return p.id, r


class TestAcceptUrl:
    def test_send_response_includes_accept_url(self, client, memory, db_session):
        pid, r = _sent_proposal(client, memory, db_session, email="send-url@example.com")
        assert r.status_code == 200
        body = r.json()
        token = body["accept_token"]
        expected = f"{settings.frontend_base_url.rstrip('/')}/proposal/accept/{token}"
        assert body["accept_url"] == expected
        # Distinct from accept_path, which stays the backend API path.
        assert body["accept_path"] == f"/api/v1/proposals/accept/{token}"

    def test_detail_includes_accept_url_for_admin(self, client, memory, db_session):
        pid, r = _sent_proposal(client, memory, db_session, email="detail-url@example.com")
        token = r.json()["accept_token"]
        detail = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=admin_headers())
        assert detail.status_code == 200
        expected = f"{settings.frontend_base_url.rstrip('/')}/proposal/accept/{token}"
        assert detail.json()["accept_url"] == expected

    def test_list_includes_accept_url_for_admin(self, client, memory, db_session):
        pid, r = _sent_proposal(client, memory, db_session, email="list-url@example.com")
        token = r.json()["accept_token"]
        listing = client.get("/api/v1/dashboard/proposals", headers=admin_headers())
        assert listing.status_code == 200
        item = next(i for i in listing.json()["items"] if i["id"] == pid)
        expected = f"{settings.frontend_base_url.rstrip('/')}/proposal/accept/{token}"
        assert item["accept_url"] == expected

    def test_accept_url_is_null_before_send(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="unsent-url@example.com", name="Unsent")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=50_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        db_session.commit()
        detail = client.get(f"/api/v1/dashboard/proposals/{p.id}", headers=admin_headers())
        assert detail.json()["accept_token"] is None
        assert detail.json()["accept_url"] is None
        listing = client.get("/api/v1/dashboard/proposals", headers=admin_headers())
        item = next(i for i in listing.json()["items"] if i["id"] == p.id)
        assert item["accept_url"] is None

    def test_engineer_never_sees_accept_url(self, client, memory, db_session):
        pid, _ = _sent_proposal(client, memory, db_session, email="eng-url@example.com")
        detail = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=engineer_headers())
        assert detail.status_code == 200
        assert "accept_url" not in detail.json()
        listing = client.get("/api/v1/dashboard/proposals", headers=engineer_headers())
        item = next(i for i in listing.json()["items"] if i["id"] == pid)
        assert "accept_url" not in item

    def test_accept_url_matches_the_link_in_the_customer_email(self, client, memory, db_session, monkeypatch):
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return True, None

        monkeypatch.setattr("app.api.routes.dashboard.notifier.send_proposal_to_customer", _capture)
        pid, r = _sent_proposal(client, memory, db_session, email="email-match@example.com")
        assert r.status_code == 200
        assert captured["accept_url"] == r.json()["accept_url"]

    def test_accept_url_respects_frontend_base_url_setting(self, client, memory, db_session, monkeypatch):
        monkeypatch.setattr(settings, "frontend_base_url", "https://app.gridnova.example/")
        pid, r = _sent_proposal(client, memory, db_session, email="custom-base@example.com")
        token = r.json()["accept_token"]
        assert r.json()["accept_url"] == f"https://app.gridnova.example/proposal/accept/{token}"


class TestAssignableEmployees:
    ASSIGNABLE = "/api/v1/dashboard/employees/assignable"

    def _seed(self, memory, db_session):
        memory.get_or_create_employee("emp-sales-a", "Anaya Sales", "anaya@gridnova.in", "sales")
        memory.get_or_create_employee("emp-sales-z", "Zubin Sales", "zubin@gridnova.in", "sales")
        memory.get_or_create_employee("emp-admin-b", "Bina Admin", "bina@gridnova.in", "admin")
        eng = memory.get_or_create_employee("emp-eng-c", "Chetan Eng", "chetan@gridnova.in", "engineer")
        mgmt = memory.get_or_create_employee("emp-mgmt-d", "Dev Mgmt", "dev@gridnova.in", "management")
        inactive = memory.get_or_create_employee("emp-sales-x", "Xena Inactive", "xena@gridnova.in", "sales")
        inactive.is_active = False
        db_session.commit()

    def test_admin_sees_only_active_admin_and_sales_sorted_by_name(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        names = [e["name"] for e in body]
        # emp-admin-1 (from admin_headers fixture provisioning) is also present.
        assert "Anaya Sales" in names and "Zubin Sales" in names and "Bina Admin" in names
        assert "Chetan Eng" not in names  # engineer excluded
        assert "Dev Mgmt" not in names  # management excluded
        assert "Xena Inactive" not in names  # deactivated excluded
        assert names == sorted(names, key=str.lower)

    def test_management_can_view_assignable(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=management_headers())
        assert r.status_code == 200
        assert len(r.json()) >= 3

    def test_response_shape_is_id_name_role_only(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=admin_headers())
        for item in r.json():
            assert set(item.keys()) == {"id", "name", "role"}
            assert item["role"] in ("admin", "sales")

    def test_sales_forbidden(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=sales_headers())
        assert r.status_code == 403

    def test_engineer_forbidden(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=engineer_headers())
        assert r.status_code == 403

    def test_operations_forbidden(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get(self.ASSIGNABLE, headers=SUB)
        assert r.status_code == 403

    def test_get_employees_stays_admin_only(self, client, memory, db_session):
        self._seed(memory, db_session)
        r = client.get("/api/v1/dashboard/employees", headers=management_headers())
        assert r.status_code == 403

    def test_matches_exactly_what_assign_accepts(self, client, memory, db_session):
        """The set returned by /assignable is exactly the set PATCH .../assign accepts
        (active, role in admin/sales) — cross-checked against a real assign call."""
        self._seed(memory, db_session)
        customer = memory.get_or_create_customer(email="assign-cross@example.com", name="Cross")
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()

        assignable_ids = {e["id"] for e in client.get(self.ASSIGNABLE, headers=admin_headers()).json()}
        assert "emp-sales-a" in assignable_ids

        r = client.patch(f"/api/v1/dashboard/leads/{lead.id}/assign",
                         json={"employee_id": "emp-sales-a"}, headers=admin_headers())
        assert r.status_code == 200

        # A non-assignable target (engineer) must be rejected by assign too.
        r2 = client.patch(f"/api/v1/dashboard/leads/{lead.id}/assign",
                          json={"employee_id": "emp-eng-c"}, headers=admin_headers())
        assert r2.status_code == 422
        assert "emp-eng-c" not in assignable_ids
