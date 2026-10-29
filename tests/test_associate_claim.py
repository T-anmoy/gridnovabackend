"""BE-21 — an approved associate whose Clerk invitation failed (or never arrived) signs up
normally and claims their account: POST /api/v1/associate/me/claim."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth import dependencies as deps
from app.memory.models import Associate, AssociateStatus, AuditLog, ClerkInviteStatus, Employee, EmployeeRole
from tests.associate_support import DASH, territories  # noqa: F401 — fixture
from tests.clerk_support import clerk_mock

CLAIM = "/api/v1/associate/me/claim"
ME = "/api/v1/associate/me"
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}


@pytest.fixture(autouse=True)
def _fresh_rate_limits(monkeypatch):
    from app.api.routes import associate_portal
    from app.security.rate_limit import RateLimitLog
    monkeypatch.setattr(associate_portal, "_claim_ip_log", RateLimitLog(associate_portal.CLAIM_WINDOW_SECONDS))
    monkeypatch.setattr(associate_portal, "_claim_user_log", RateLimitLog(associate_portal.CLAIM_WINDOW_SECONDS))


def associate(db_session, *, invite=ClerkInviteStatus.failed, status=AssociateStatus.active, **fields) -> Associate:
    from tests.onboarding_support import make_associate
    gna = make_associate(db_session, status=status)
    row = db_session.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one()
    row.clerk_invite_status = invite
    for k, v in fields.items():
        setattr(row, k, v)
    db_session.commit()
    return row


def signed_up(monkeypatch, sub: str, metadata: dict | None = None, email: str = "") -> dict:
    """A fresh Clerk session token: no associate principal yet."""
    claims = {"sub": sub, "public_metadata": metadata or {}, "email": email}
    monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda t: claims)
    return {"Authorization": "Bearer fresh"}


def refused(r) -> bool:
    return r.status_code == 403 and r.json()["detail"]["code"] == "claim_not_allowed"


class TestClaim:
    def test_a_verified_matching_address_claims_the_account(self, client, territories, db_session, monkeypatch):
        a = associate(db_session)
        clerk = clerk_mock(monkeypatch, verified={"user_c1": [a.email.upper()]})
        r = client.post(CLAIM, headers=signed_up(monkeypatch, "user_c1"))
        assert r.status_code == 200, r.text
        assert r.json() == {"associate_id": a.associate_id, "refresh_session": True}
        assert clerk.metadata == [("user_c1", {"principal": "associate", "associate_id": a.associate_id})]
        db_session.expire_all()
        assert db_session.get(Associate, a.id).clerk_user_id == "user_c1"
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_id == a.associate_id,
                                                          AuditLog.action == "clerk_user_claimed")).scalar_one()
        assert audit.performed_by == "user_c1" and audit.after_value["clerk_user_id"] == "user_c1"

    def test_after_claiming_the_normal_associate_token_works_end_to_end(self, client, territories, db_session,
                                                                       monkeypatch):
        a = associate(db_session)
        clerk_mock(monkeypatch, verified={"user_c2": [a.email]})
        assert client.post(CLAIM, headers=signed_up(monkeypatch, "user_c2")).status_code == 200
        # The refreshed token carries the claims Clerk now holds.
        h = signed_up(monkeypatch, "user_c2", {"principal": "associate", "associate_id": a.associate_id})
        r = client.get(ME, headers=h)
        assert r.status_code == 200 and r.json()["associate_id"] == a.associate_id
        # The binding holds: another Clerk user naming the same associate is refused.
        assert client.get(ME, headers=signed_up(monkeypatch, "user_other",
                                                {"principal": "associate",
                                                 "associate_id": a.associate_id})).status_code == 403

    def test_an_invitation_still_pending_is_revoked(self, client, territories, db_session, monkeypatch):
        a = associate(db_session, invite=ClerkInviteStatus.sent, clerk_invitation_id="inv_pending")
        clerk = clerk_mock(monkeypatch, verified={"user_c3": [a.email]})
        assert client.post(CLAIM, headers=signed_up(monkeypatch, "user_c3")).status_code == 200
        assert clerk.invitations_revoked == ["inv_pending"]

    def test_an_unverified_address_is_refused(self, client, territories, db_session, monkeypatch):
        a = associate(db_session)
        clerk = clerk_mock(monkeypatch, verified={"user_c4": []})
        # The token claims the address; Clerk has not verified it.
        assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, "user_c4", email=a.email)))
        assert clerk.metadata == []
        db_session.expire_all()
        assert db_session.get(Associate, a.id).clerk_user_id is None

    def test_the_match_is_exact(self, client, territories, db_session, monkeypatch):
        a = associate(db_session)
        local, domain = a.email.split("@")
        for i, near in enumerate((f"{local}x@{domain}", f"{local}@{domain}.in", f"x{local}@{domain}")):
            clerk_mock(monkeypatch, verified={f"user_near{i}": [near]})
            assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, f"user_near{i}"))), near

    @pytest.mark.parametrize("status", [AssociateStatus.suspended, AssociateStatus.deactivated])
    def test_a_non_active_associate_cannot_be_claimed(self, client, territories, db_session, monkeypatch, status):
        a = associate(db_session, status=status)
        clerk_mock(monkeypatch, verified={"user_c5": [a.email]})
        assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, "user_c5")))

    @pytest.mark.parametrize("invite", [None, ClerkInviteStatus.skipped])
    def test_only_a_failed_or_sent_invitation_can_be_claimed(self, client, territories, db_session, monkeypatch,
                                                              invite):
        a = associate(db_session, invite=invite)
        clerk_mock(monkeypatch, verified={"user_c6": [a.email]})
        assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, "user_c6")))

    def test_an_already_bound_associate_cannot_be_claimed(self, client, territories, db_session, monkeypatch):
        a = associate(db_session, clerk_user_id="user_first")
        clerk_mock(monkeypatch, verified={"user_c7": [a.email]})
        assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, "user_c7")))

    def test_staff_can_never_claim(self, client, territories, db_session, monkeypatch):
        a = associate(db_session)
        db_session.add(Employee(id="user_staff", name="S", email="staff@gridnova.in", role=EmployeeRole.sales))
        db_session.commit()
        clerk_mock(monkeypatch, verified={"user_staff": [a.email]})
        assert refused(client.post(CLAIM, headers=signed_up(monkeypatch, "user_staff")))
        # The staff dev headers are not a Clerk token at all.
        assert client.post(CLAIM, headers=ADMIN).status_code == 401

    def test_an_unknown_address_gets_the_same_answer(self, client, territories, db_session, monkeypatch):
        a = associate(db_session)
        clerk_mock(monkeypatch, verified={"user_u": ["nobody@example.test"]})
        unknown = client.post(CLAIM, headers=signed_up(monkeypatch, "user_u"))
        clerk_mock(monkeypatch, verified={"user_s": [a.email]})
        a.status = AssociateStatus.suspended
        db_session.commit()
        known = client.post(CLAIM, headers=signed_up(monkeypatch, "user_s"))
        assert refused(unknown) and refused(known) and unknown.json() == known.json()

    def test_double_claim_one_wins(self, client, territories, db_session, monkeypatch):
        """Two sessions (two Clerk sign-ups with the same verified address) race: the
        conditional UPDATE lets exactly one bind."""
        from app.associates import claim as claim_module
        a = associate(db_session)
        assert claim_module.bind(db_session, a.id, "user_r1") is True
        assert claim_module.bind(db_session, a.id, "user_r2") is False   # the loser's UPDATE matches nothing
        db_session.rollback()
        clerk_mock(monkeypatch, verified={"user_r1": [a.email], "user_r2": [a.email]})
        first = client.post(CLAIM, headers=signed_up(monkeypatch, "user_r1"))
        second = client.post(CLAIM, headers=signed_up(monkeypatch, "user_r2"))
        assert first.status_code == 200 and refused(second)
        db_session.expire_all()
        assert db_session.get(Associate, a.id).clerk_user_id == "user_r1"

    def test_a_clerk_failure_leaves_it_claimable(self, client, territories, db_session, monkeypatch):
        from app.auth.clerk_client import ClerkSyncError
        a = associate(db_session)
        clerk_mock(monkeypatch, verified={"user_c8": [a.email]})

        def _fail(*_):
            raise ClerkSyncError("Clerk metadata update failed (HTTP 500)")
        monkeypatch.setattr("app.auth.clerk_client.update_public_metadata", _fail)
        r = client.post(CLAIM, headers=signed_up(monkeypatch, "user_c8"))
        assert r.status_code == 502 and r.json()["detail"]["code"] == "clerk_sync_failed"
        db_session.expire_all()
        assert db_session.get(Associate, a.id).clerk_user_id is None

    def test_rate_limited(self, client, territories, db_session, monkeypatch):
        clerk_mock(monkeypatch, verified={"user_rl": ["nobody@example.test"]})
        h = signed_up(monkeypatch, "user_rl")
        codes = [client.post(CLAIM, headers=h).status_code for _ in range(11)]
        assert codes[:10] == [403] * 10 and codes[10] == 429

    def test_no_token_is_401(self, client):
        assert client.post(CLAIM).status_code == 401


class TestDashboardHint:
    def test_a_failed_invitation_tells_staff_what_to_say(self, client, territories, db_session, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "frontend_base_url", "https://www.gridnova.in")
        a = associate(db_session)
        body = client.get(f"{DASH}/associates/{a.associate_id}", headers=ADMIN).json()
        assert body["manual_sign_up"] == (f"Ask the associate to sign up at https://www.gridnova.in/associates/sign-up "
                                          f"with {a.email}.")

    def test_no_hint_once_claimed_or_sent(self, client, territories, db_session, monkeypatch):
        a = associate(db_session, invite=ClerkInviteStatus.sent)
        assert client.get(f"{DASH}/associates/{a.associate_id}", headers=ADMIN).json()["manual_sign_up"] is None
        b = associate(db_session, clerk_user_id="user_bound")
        assert client.get(f"{DASH}/associates/{b.associate_id}", headers=ADMIN).json()["manual_sign_up"] is None
