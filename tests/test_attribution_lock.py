"""BE-11 — the attribution lock follows the approved Associate Terms (A7, 21 Oct 2026):
the first verified onboarding holds the customer until the project is completed
(handover complete) or closed (lead lost / onboarding closed) — no 90-day expiry.

The lock is what an Admin sees on a conflict flag (`existing_was_locked`) and what the
associate sees as `attribution.locked`; a conflicting onboarding raises a flag either
way (as before), and neither can be verified while the flag is open."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.memory.models import (
    AuditLog, CustomerOnboarding, OnboardingAttributionFlag, OnboardingStatus, ProjectStage,
)
from app.onboarding import config as onboarding_config
from app.onboarding.service import attribution
from tests.execution_support import advance_to
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, headers, kyc_ready, make_associate, onboarding_ready, territories,
)
from tests.test_associate_migrations import _insert, _run_alembic, db_at_0025  # noqa: F401 — fixture
from tests.test_associate_onboarding import _create, _submit_and_verify


def _verified(client, db, n=1):
    gna = make_associate(db)
    o = _create(client, gna, n=n)
    assert _submit_and_verify(client, gna, o["onboarding_id"]).status_code == 200
    return gna, o


def _lock(client, gna, oid) -> dict:
    r = client.get(f"{BASE}/{oid}", headers=headers(gna))
    assert r.status_code == 200, r.text
    return r.json()["attribution"]


def _conflict(client, db, mobile="9830000001", n=2):
    other = make_associate(db)
    second = _create(client, other, n=n, customer_mobile=mobile)
    flag = db.execute(select(OnboardingAttributionFlag).where(
        OnboardingAttributionFlag.onboarding_id == second["onboarding_id"])).scalar_one()
    return other, second, flag


def _lost(client, lead_id: int, reason="Customer chose another installer."):
    r = client.patch(f"{DASH}/leads/{lead_id}/stage", json={"stage": "lost", "reason": reason}, headers=ADMIN)
    assert r.status_code == 200, r.text


class TestLockHolds:
    def test_verification_starts_a_lock_with_no_end_date(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        assert _lock(client, gna, o["onboarding_id"]) == {"locked": True, "released_reason": None,
                                                         "under_review": False}
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "customer_onboarding",
                                                          AuditLog.action == "verified")).scalar_one()
        assert audit.after_value == {"status": "verified", "attribution": "locked_until_completed_or_closed"}

    def test_not_verified_is_not_locked(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        o = _create(client, gna)
        assert _lock(client, gna, o["onboarding_id"]) == {"locked": False, "released_reason": None,
                                                         "under_review": False}

    def test_holds_beyond_90_days(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        row = db_session.get(CustomerOnboarding, o["onboarding_id"])
        row.verified_at = datetime.now(timezone.utc) - timedelta(days=400)
        db_session.commit()
        assert _lock(client, gna, o["onboarding_id"])["locked"] is True
        _, _, flag = _conflict(client, db_session)
        assert flag.existing_was_locked is True

    def test_a_conflict_while_locked_raises_the_flag(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        other, second, flag = _conflict(client, db_session)
        assert (flag.existing_onboarding_id, flag.matched_on, flag.existing_was_locked) == \
            (o["onboarding_id"], ["mobile"], True)
        r = _submit_and_verify(client, other, second["onboarding_id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "attribution_conflict"
        assert _lock(client, gna, o["onboarding_id"])["under_review"] is True


class TestLockReleases:
    def test_on_handover_complete(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        advance_to(db_session, o["onboarding_id"], ProjectStage.handover_complete)
        assert _lock(client, gna, o["onboarding_id"]) == {"locked": False, "released_reason": "project_completed",
                                                         "under_review": False}
        _, _, flag = _conflict(client, db_session)
        assert flag.existing_was_locked is False

    def test_on_lead_lost_and_again_on_reopening(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        _lost(client, o["lead_id"])
        assert _lock(client, gna, o["onboarding_id"])["released_reason"] == "lead_lost"
        r = client.patch(f"{DASH}/leads/{o['lead_id']}/stage",
                         json={"stage": "qualified", "reason": "Customer came back."}, headers=ADMIN)
        assert r.status_code == 200
        assert _lock(client, gna, o["onboarding_id"])["locked"] is True

    def test_on_closed_by_an_admin(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        other, second, flag = _conflict(client, db_session)
        r = client.post(f"{DASH}/attribution-flags/{flag.id}/resolve",
                        json={"resolution": "award_new", "note": "Customer asked for the other associate."},
                        headers=ADMIN)
        assert r.status_code == 200
        db_session.expire_all()
        old = db_session.get(CustomerOnboarding, o["onboarding_id"])
        assert attribution(db_session, old) == {"locked": False, "released_reason": "closed"}
        closed = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "customer_onboarding",
                                                           AuditLog.action == "closed")).scalar_one()
        assert closed.before_value == {"status": "verified", "attribution_locked": True}
        assert _submit_and_verify(client, other, second["onboarding_id"]).json()["status"] == "verified"

    def test_after_release_a_new_onboarding_can_be_attributed(self, client, db_session, onboarding_ready):
        """As after the old 90-day expiry: the match is still flagged for an Admin, but
        as an unlocked one; awarding it to the new onboarding lets it be verified."""
        gna, o = _verified(client, db_session)
        _lost(client, o["lead_id"])
        other, second, flag = _conflict(client, db_session)
        assert flag.existing_was_locked is False
        r = client.post(f"{DASH}/attribution-flags/{flag.id}/resolve",
                        json={"resolution": "award_new", "note": "The earlier project was lost."}, headers=ADMIN)
        assert r.status_code == 200
        assert _submit_and_verify(client, other, second["onboarding_id"]).json()["status"] == "verified"
        assert _lock(client, other, second["onboarding_id"])["locked"] is True

    def test_keep_existing_still_works(self, client, db_session, onboarding_ready):
        gna, o = _verified(client, db_session)
        other, second, flag = _conflict(client, db_session)
        r = client.post(f"{DASH}/attribution-flags/{flag.id}/resolve",
                        json={"resolution": "keep_existing", "note": "First associate keeps it."}, headers=ADMIN)
        assert r.status_code == 200
        db_session.expire_all()
        assert db_session.get(CustomerOnboarding, second["onboarding_id"]).status == OnboardingStatus.closed
        assert _lock(client, gna, o["onboarding_id"]) == {"locked": True, "released_reason": None,
                                                         "under_review": False}


class TestContract:
    def test_options_no_longer_carry_a_lock_period(self, client, db_session, onboarding_ready):
        body = client.get(f"{BASE}/options", headers=headers(make_associate(db_session))).json()
        assert "attribution_lock_days" not in body

    def test_config_refuses_a_lock_period(self):
        with pytest.raises(onboarding_config.OnboardingConfigError):
            onboarding_config._validate({**onboarding_config.load(), "attribution_lock_days": 90})

    def test_staff_detail_shows_the_state(self, client, db_session, onboarding_ready):
        _, o = _verified(client, db_session)
        body = client.get(f"{DASH}/onboardings/{o['onboarding_id']}", headers=ADMIN).json()
        assert body["attribution"]["locked"] is True and "locked_until" not in body["attribution"]


class TestMigration:
    def test_existing_locks_survive_and_downgrade_restores_the_column(self, db_at_0025):
        assert _run_alembic("upgrade", "0052_associate_referrals", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            verified = dict(verification_call_completed=1, verification_call_note="x", verified_by_employee_id="e",
                            verified_at="2026-01-01 00:00:00",
                            attribution_locked_until="2026-04-01 00:00:00")      # long expired under 90 days
            for i, status in enumerate(("submitted", "verified"), start=1):
                _insert(conn, "customer_onboardings", id=i, lead_id=i, customer_id=1, project_id=1,
                        proposal_inputs_id=i, owner_associate_id=1, status=status, state_id=1, district_id=1,
                        customer_mobile=f"983000000{i}", **(verified if status == "verified" else {}))
            conn.commit()
        finally:
            conn.close()
        result = _run_alembic("upgrade", "0053_attribution_until_completed", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(customer_onboardings)")}
            assert "attribution_locked_until" not in columns
            # Still verified, so still locked (the lock is derived from the status).
            assert conn.execute("SELECT id, status FROM customer_onboardings ORDER BY id").fetchall() == \
                [(1, "submitted"), (2, "verified")]
            with pytest.raises(sqlite3.IntegrityError):    # verified needs verified_at
                conn.execute("UPDATE customer_onboardings SET verified_at = NULL WHERE id = 2")
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0052_associate_referrals", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            rows = conn.execute("SELECT id, attribution_locked_until FROM customer_onboardings ORDER BY id").fetchall()
            assert rows[0] == (1, None) and rows[1][1].startswith("2026-04-01")
        finally:
            conn.close()
