"""BE-19 — the FE-17 contract gaps: GET /access/admins, sign-in fields on GET /employees,
alert addresses for staff managers, and the /approvals list (wrapper, requester name, only
requests the viewer may decide)."""
import pytest

from app.auth import clerk_client
from app.memory.models import ApprovalRequest, ApprovalRequestStatus
from tests.test_access_control import D, multi, seed

NM = multi("emp-nm-1", "network_manager")


@pytest.fixture
def staff(db_session):
    seed(db_session, "emp-admin-1", "admin", email="ada@gridnova.in").name = "Ada Admin"
    seed(db_session, "emp-admin-2", "admin", email="bose@gridnova.in").name = "Bose Admin"
    seed(db_session, "emp-admin-3", "admin", email="gone@gridnova.in", active=False)
    seed(db_session, "emp-nm-1", "network_manager", email="nm@gridnova.in").name = "Abhijit Bhattacharjee"
    seed(db_session, "emp-sales-1", "sales", email="sales@gridnova.in")
    db_session.commit()


class TestAdmins:
    def test_any_staff_sees_the_active_admins_by_name_without_emails(self, client, staff):
        r = client.get(f"{D}/access/admins", headers=multi("emp-sales-1", "sales"))
        assert r.status_code == 200
        assert r.json()["items"] == [{"id": "emp-admin-1", "name": "Ada Admin", "email": None},
                                     {"id": "emp-admin-2", "name": "Bose Admin", "email": None}]

    def test_an_admin_also_sees_emails(self, client, staff):
        items = client.get(f"{D}/access/admins", headers=multi("emp-admin-1", "admin")).json()["items"]
        assert [i["email"] for i in items] == ["ada@gridnova.in", "bose@gridnova.in"]

    def test_associates_are_refused(self, client, staff):
        assert client.get(f"{D}/access/admins", headers={"X-Associate-Id": "GNA-WB-26-00001"}).status_code == 401


class TestEmployeeSignIn:
    def test_null_when_clerk_is_not_configured(self, client, staff, monkeypatch):
        monkeypatch.setattr(clerk_client.settings, "clerk_secret_key", "")
        rows = client.get(f"{D}/employees", headers=multi("emp-admin-1", "admin")).json()
        assert all(r["last_sign_in_at"] is None and r["two_factor_enabled"] is None for r in rows)

    def test_from_clerk_and_cached(self, client, staff, monkeypatch):
        monkeypatch.setattr(clerk_client.settings, "clerk_secret_key", "sk_test")
        monkeypatch.setattr(clerk_client, "_activity_cache", {})
        calls = []

        class Response:
            def json(self):
                return [{"id": "emp-admin-1", "last_sign_in_at": 1793000000000, "two_factor_enabled": True},
                        {"id": "emp-sales-1", "last_sign_in_at": None, "two_factor_enabled": False}]

        monkeypatch.setattr(clerk_client, "_call", lambda *a, **k: calls.append(k["params"]) or Response())
        rows = {r["id"]: r for r in client.get(f"{D}/employees", headers=multi("emp-admin-1", "admin")).json()}
        assert rows["emp-admin-1"]["last_sign_in_at"] == "2026-10-26T07:33:20+00:00"
        assert rows["emp-admin-1"]["two_factor_enabled"] is True
        assert rows["emp-sales-1"] == {**rows["emp-sales-1"], "last_sign_in_at": None, "two_factor_enabled": False}
        assert rows["emp-nm-1"]["last_sign_in_at"] is None   # Clerk does not know it: unknown
        client.get(f"{D}/employees", headers=multi("emp-admin-1", "admin"))
        assert len(calls) == 2   # the second listing asked only for the user Clerk did not return

    def test_clerk_down_means_unknown_not_an_error(self, client, staff, monkeypatch):
        monkeypatch.setattr(clerk_client.settings, "clerk_secret_key", "sk_test")
        monkeypatch.setattr(clerk_client, "_activity_cache", {})

        def down(*a, **k):
            raise clerk_client.ClerkSyncError("Clerk user activity failed (HTTP 503)")
        monkeypatch.setattr(clerk_client, "_call", down)
        r = client.get(f"{D}/employees", headers=multi("emp-admin-1", "admin"))
        assert r.status_code == 200 and all(x["last_sign_in_at"] is None for x in r.json())


class TestAlertAddresses:
    def test_staff_managers_see_addresses(self, client, staff):
        events = {e["event"]: e for e in client.get(f"{D}/access/alerts", headers=NM).json()["events"]}
        submitted = events["associate_application_submitted"]["addresses"]
        assert submitted["by_role"] == {"admin": ["ada@gridnova.in", "bose@gridnova.in"],
                                        "network_manager": ["nm@gridnova.in"]}
        assert "gone@gridnova.in" not in str(submitted)

    def test_others_see_counts_only(self, client, staff):
        events = client.get(f"{D}/access/alerts", headers=multi("emp-sales-1", "sales")).json()["events"]
        assert all("addresses" not in e for e in events)
        assert next(e for e in events if e["event"] == "associate_application_submitted")["active_holders"] == {
            "admin": 2, "network_manager": 1}


class TestApprovals:
    def _request(self, db_session, rid, action, requested_by, payload):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        db_session.add(ApprovalRequest(id=rid, action=action, summary=rid, payload=payload,
                                       status=ApprovalRequestStatus.pending, requested_by=requested_by,
                                       created_at=now, expires_at=now + timedelta(hours=72)))
        db_session.commit()

    def test_network_manager_sees_only_what_it_may_decide(self, client, db_session, staff):
        self._request(db_session, "grant-admin", "staff.set_roles", "emp-admin-1",
                      {"employee_id": "emp-sales-1", "roles": ["admin", "sales"], "before": ["sales"]})
        self._request(db_session, "grant-ops", "staff.invite", "emp-admin-1",
                      {"name": "O", "email": "o@x.in", "roles": ["operations"]})
        r = client.get(f"{D}/approvals", headers=NM)
        assert [i["approval_request_id"] for i in r.json()["items"]] == ["grant-ops"]
        admin = client.get(f"{D}/approvals", headers=multi("emp-admin-2", "admin")).json()["items"]
        assert {i["approval_request_id"] for i in admin} == {"grant-admin", "grant-ops"}

    def test_the_requester_always_sees_their_own_and_its_name(self, client, db_session, staff):
        self._request(db_session, "mine", "staff.set_roles", "emp-nm-1",
                      {"employee_id": "emp-sales-1", "roles": ["admin"], "before": ["sales"]})
        [item] = client.get(f"{D}/approvals", headers=NM).json()["items"]
        assert item["approval_request_id"] == "mine" and item["requested_by_name"] == "Abhijit Bhattacharjee"
