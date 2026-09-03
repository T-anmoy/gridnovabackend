"""Employee deactivation — a flag, never a delete.

Every historical FK (leads, proposals, meetings, audit log) must survive a
deactivation untouched, and a deactivated employee must actually be blocked at
sign-in, not merely labelled in the dashboard.
"""
from app.memory.memory_manager import MemoryManager
from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


def _seed_employee(memory, db_session, employee_id, role="sales", name="Test Employee"):
    emp = memory.get_or_create_employee(employee_id, name, f"{employee_id}@example.com", role)
    db_session.commit()
    return emp


class TestDeactivate:
    def test_deactivates_and_audits(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-9")

        response = client.patch(
            "/api/v1/dashboard/employees/emp-sales-9/deactivate", headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        emp = memory.get_employee("emp-sales-9")
        assert emp.is_active is False

        logs = memory.list_audit_logs(entity_type="employee", entity_id="emp-sales-9")
        assert logs[0].action == "deactivated"
        assert logs[0].performed_by == "emp-admin-1"
        assert logs[0].after_value["is_active"] is False

    def test_does_not_delete_the_row(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-10")
        client.patch("/api/v1/dashboard/employees/emp-sales-10/deactivate", headers=admin_headers())

        # Still present, still returned by list — deactivation is purely a flag.
        emp = memory.get_employee("emp-sales-10")
        assert emp is not None
        ids = {e.id for e in memory.list_employees()}
        assert "emp-sales-10" in ids

    def test_requires_admin(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-11")
        for headers in (sales_headers(), engineer_headers(), management_headers()):
            response = client.patch(
                "/api/v1/dashboard/employees/emp-sales-11/deactivate", headers=headers
            )
            assert response.status_code == 403

    def test_404_for_unknown_employee(self, client):
        response = client.patch(
            "/api/v1/dashboard/employees/no-such-employee/deactivate", headers=admin_headers()
        )
        assert response.status_code == 404

    def test_idempotent_on_an_already_inactive_employee(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-12")
        client.patch("/api/v1/dashboard/employees/emp-sales-12/deactivate", headers=admin_headers())
        response = client.patch(
            "/api/v1/dashboard/employees/emp-sales-12/deactivate", headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    def test_cannot_deactivate_the_last_active_admin(self, client, memory, db_session):
        """emp-admin-1 (from admin_headers()) is provisioned by the request itself —
        the only active admin in this test's database — so deactivating it must
        be refused."""
        client.get("/api/v1/dashboard/employees", headers=admin_headers())  # provisions emp-admin-1

        response = client.patch(
            "/api/v1/dashboard/employees/emp-admin-1/deactivate", headers=admin_headers()
        )
        assert response.status_code == 409
        assert memory.get_employee("emp-admin-1").is_active is True

    def test_may_deactivate_an_admin_when_another_remains_active(self, client, memory, db_session):
        client.get("/api/v1/dashboard/employees", headers=admin_headers())  # provisions emp-admin-1
        _seed_employee(memory, db_session, "emp-admin-2", role="admin")

        response = client.patch(
            "/api/v1/dashboard/employees/emp-admin-2/deactivate", headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False


class TestReactivate:
    def test_reactivates_and_audits(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-13")
        client.patch("/api/v1/dashboard/employees/emp-sales-13/deactivate", headers=admin_headers())

        response = client.patch(
            "/api/v1/dashboard/employees/emp-sales-13/reactivate", headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        assert memory.get_employee("emp-sales-13").is_active is True

        logs = memory.list_audit_logs(entity_type="employee", entity_id="emp-sales-13")
        assert logs[0].action == "reactivated"

    def test_requires_admin(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-14")
        response = client.patch(
            "/api/v1/dashboard/employees/emp-sales-14/reactivate", headers=sales_headers()
        )
        assert response.status_code == 403

    def test_404_for_unknown_employee(self, client):
        response = client.patch(
            "/api/v1/dashboard/employees/no-such-employee/reactivate", headers=admin_headers()
        )
        assert response.status_code == 404


class TestDeactivatedEmployeeCannotSignIn:
    def test_a_deactivated_employee_is_refused_at_the_next_request(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-15")
        client.patch("/api/v1/dashboard/employees/emp-sales-15/deactivate", headers=admin_headers())

        response = client.get(
            "/api/v1/dashboard/leads",
            headers={"X-Employee-Id": "emp-sales-15", "X-Employee-Role": "sales"},
        )
        assert response.status_code == 403

    def test_does_not_reactivate_itself_by_presenting_credentials_again(self, client, memory, db_session):
        _seed_employee(memory, db_session, "emp-sales-16")
        client.patch("/api/v1/dashboard/employees/emp-sales-16/deactivate", headers=admin_headers())

        client.get(
            "/api/v1/dashboard/leads",
            headers={"X-Employee-Id": "emp-sales-16", "X-Employee-Role": "sales"},
        )
        assert memory.get_employee("emp-sales-16").is_active is False
