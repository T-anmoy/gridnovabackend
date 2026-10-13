"""Associates are a separate principal from employees — proven in both directions.

Staff-only routes reject associate tokens (and never provision an Employee row from
one); associate-only routes reject staff tokens. JWT verification itself is stubbed
at the same seam tests/test_role_sync.py uses (_verify_clerk_jwt)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.auth import dependencies as deps
from app.memory.models import (
    Associate, AssociateApplication, AssociateCapability, AssociateStatus, AssociateType, AuditLog, Employee,
)
from tests.associate_support import territories  # noqa: F401 — fixture
from tests.conftest import seed_employees

STAFF_ROUTE = "/api/v1/dashboard/employees/me"
ASSOCIATE_ROUTE = "/api/v1/associate/me"
BEARER = {"Authorization": "Bearer token"}


def _token(monkeypatch, claims: dict) -> None:
    monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: claims)


def associate_claims(sub="user_assoc_1", associate_id="GNA-WB-26-00001"):
    return {"sub": sub, "public_metadata": {"principal": "associate", "associate_id": associate_id}}


STAFF_CLAIMS = {"sub": "user_staff_1", "public_metadata": {"role": "admin"}, "email": "s@gridnova.in"}


@pytest.fixture
def associate(db_session, territories):
    """One active associate, created directly (the approval flow has its own tests)."""
    from app.memory.models import TerritoryDistrict, TerritoryState

    seed_employees(db_session, "emp-admin-1")
    state = db_session.execute(select(TerritoryState).where(TerritoryState.lgd_code == 19)).scalar_one()
    district = db_session.execute(select(TerritoryDistrict).where(TerritoryDistrict.lgd_code == 315)).scalar_one()
    now = datetime.now(timezone.utc)
    app = AssociateApplication(
        full_name="A One", date_of_birth=datetime(1990, 1, 1).date(), address_line="x", state_id=state.id,
        district_id=district.id, pin_code="700001", mobile="9876543210", email="a1@example.test",
        aadhaar_last4="1234", pan_encrypted="enc", pan_last4="234F", bank_account_encrypted="enc", bank_account_last4="9012",
        bank_ifsc="SBIN0001234", kyc_consent_version="v", kyc_consent_accepted_at=now, documents={},
        capability=AssociateCapability.sales, verification_call_completed=False,
    )
    db_session.add(app)
    db_session.flush()
    row = Associate(associate_id="GNA-WB-26-00001", associate_type=AssociateType.associate,
                    status=AssociateStatus.active, associate_application_id=app.id, full_name="A One",
                    email="a1@example.test", mobile="9876543210", state_id=state.id,
                    capability=AssociateCapability.sales, approved_by_employee_id="emp-admin-1")
    db_session.add(row)
    db_session.commit()
    return row


def _employee_count(db_session) -> int:
    return len(db_session.execute(select(Employee)).scalars().all())


class TestStaffRoutesRejectAssociates:
    def test_associate_token_rejected_on_staff_route(self, client, db_session, monkeypatch, associate):
        before = _employee_count(db_session)
        _token(monkeypatch, associate_claims())
        r = client.get(STAFF_ROUTE, headers=BEARER)
        assert r.status_code == 403
        assert _employee_count(db_session) == before  # never provisioned as an employee

    def test_associate_token_with_a_role_claim_is_still_rejected(self, client, db_session, monkeypatch, associate):
        """Even if a role claim were ever added to an associate's metadata, the
        principal decides — no Employee row is created."""
        claims = associate_claims()
        claims["public_metadata"]["role"] = "admin"
        _token(monkeypatch, claims)
        assert client.get(STAFF_ROUTE, headers=BEARER).status_code == 403
        assert db_session.get(Employee, "user_assoc_1") is None

    def test_unknown_principal_rejected(self, client, monkeypatch):
        _token(monkeypatch, {"sub": "u", "public_metadata": {"principal": "partner", "role": "admin"}})
        assert client.get(STAFF_ROUTE, headers=BEARER).status_code == 403

    def test_associate_dev_header_does_not_authenticate_staff_route(self, client, associate):
        assert client.get(STAFF_ROUTE, headers={"X-Associate-Id": associate.associate_id}).status_code == 401

    def test_staff_token_still_works(self, client, monkeypatch):
        _token(monkeypatch, STAFF_CLAIMS)
        assert client.get(STAFF_ROUTE, headers=BEARER).status_code == 200

    def test_explicit_staff_principal_works(self, client, monkeypatch):
        _token(monkeypatch, {**STAFF_CLAIMS, "public_metadata": {"principal": "staff", "role": "admin"}})
        assert client.get(STAFF_ROUTE, headers=BEARER).status_code == 200


class TestAssociateRoutesRejectStaff:
    def test_staff_token_rejected_on_associate_route(self, client, monkeypatch, associate):
        _token(monkeypatch, STAFF_CLAIMS)
        r = client.get(ASSOCIATE_ROUTE, headers=BEARER)
        assert r.status_code == 403
        assert r.json()["detail"] == "This endpoint is for Grid Nova associates only."

    def test_staff_dev_headers_do_not_authenticate_associate_route(self, client, associate):
        r = client.get(ASSOCIATE_ROUTE, headers={"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"})
        assert r.status_code == 401

    def test_no_credentials(self, client):
        assert client.get(ASSOCIATE_ROUTE).status_code == 401


class TestAssociateTokens:
    def test_valid_associate_token(self, client, db_session, monkeypatch, associate):
        _token(monkeypatch, associate_claims())
        r = client.get(ASSOCIATE_ROUTE, headers=BEARER)
        assert r.status_code == 200 and r.json()["associate_id"] == "GNA-WB-26-00001"

    def test_first_sign_in_binds_the_clerk_user_and_is_audited(self, client, db_session, monkeypatch, associate):
        _token(monkeypatch, associate_claims(sub="user_first"))
        assert client.get(ASSOCIATE_ROUTE, headers=BEARER).status_code == 200
        db_session.expire_all()
        assert db_session.get(Associate, associate.id).clerk_user_id == "user_first"
        entries = db_session.execute(select(AuditLog).where(AuditLog.action == "clerk_user_bound")).scalars().all()
        assert [e.entity_id for e in entries] == ["GNA-WB-26-00001"]

    def test_another_clerk_user_cannot_use_a_bound_associate_id(self, client, monkeypatch, associate):
        _token(monkeypatch, associate_claims(sub="user_first"))
        client.get(ASSOCIATE_ROUTE, headers=BEARER)
        _token(monkeypatch, associate_claims(sub="user_intruder"))
        assert client.get(ASSOCIATE_ROUTE, headers=BEARER).status_code == 403

    def test_missing_associate_id_claim(self, client, monkeypatch, associate):
        _token(monkeypatch, {"sub": "u", "public_metadata": {"principal": "associate"}})
        assert client.get(ASSOCIATE_ROUTE, headers=BEARER).status_code == 403

    def test_unknown_associate(self, client, monkeypatch, territories):
        _token(monkeypatch, associate_claims(associate_id="GNA-WB-26-99999"))
        assert client.get(ASSOCIATE_ROUTE, headers=BEARER).status_code == 403

    @pytest.mark.parametrize("status", [AssociateStatus.suspended, AssociateStatus.deactivated])
    def test_inactive_associate_refused(self, client, db_session, monkeypatch, associate, status):
        associate.status = status
        db_session.commit()
        _token(monkeypatch, associate_claims())
        r = client.get(ASSOCIATE_ROUTE, headers=BEARER)
        assert r.status_code == 403 and status.value in r.json()["detail"]

    def test_dev_header_blocked_in_production(self, client, monkeypatch, associate):
        from app.config import settings
        monkeypatch.setattr(settings, "environment", "production")
        assert client.get(ASSOCIATE_ROUTE, headers={"X-Associate-Id": associate.associate_id}).status_code == 403
