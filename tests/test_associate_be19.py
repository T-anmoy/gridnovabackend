"""BE-19 — associate journeys: the defects the audit found, one test (or class) each.

Applicant emails (M1), translatable field-error codes (m2), dead staff links (M3, M13),
region scope on every per-record review route (M4), Clerk access on suspension (M9),
moving a suspended associate's customers (B10), frozen commission (M11), no customer
emails to an inactive associate (m12), and a second associate's ids on the commission
routes (data protection)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

import app.notifications.notifier as notifier_module
from app.memory.models import (
    Associate, AssociateStatus, AuditLog, ClerkInviteStatus, CustomerOnboarding, Employee, ExecutionMode,
    ProjectExecution, ProjectStage, TerritoryState,
)
from tests.associate_support import (  # noqa: F401 — fixtures
    ASSOCIATE_APPLY, BR, DASH, associate_form, kyc_ready, submit, territories,
)
from tests.conftest import seed_employees
from tests.execution_support import Recorder

ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}


@pytest.fixture
def emails(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(notifier_module, "notifier", rec)
    return rec


@pytest.fixture
def ready(kyc_ready, db_session, emails):
    seed_employees(db_session, "emp-admin-1")
    with patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub"):
        yield emails


def _apply(client, n=1):
    r = submit(client, ASSOCIATE_APPLY, associate_form(n))
    assert r.status_code == 201, r.text
    return r.json()["application_id"]


def _app_url(app_id):
    return f"{DASH}/associate-applications/associate/{app_id}"


def _mails(rec, event):
    return [p for e, p in rec.events if e == "associate_email" and p.get("event_type") == event]


# ── M1: the applicant hears back ────────────────────────────────────────────────

class TestApplicantEmails:
    def test_confirmation_on_submit(self, client, ready):
        app_id = _apply(client)
        [mail] = _mails(ready, "associate_application_received")
        assert mail["to"] == "applicant1@example.test"
        assert f"application #{app_id}" in " ".join(mail["lines"]) and "as an Associate" in " ".join(mail["lines"])

    def test_needs_info_tells_the_applicant_what_is_needed(self, client, ready):
        app_id = _apply(client)
        client.post(f"{_app_url(app_id)}/status", json={"status": "under_review"}, headers=ADMIN)
        r = client.post(f"{_app_url(app_id)}/status", json={"status": "needs_info", "note": "Cheque unreadable"},
                        headers=ADMIN)
        assert r.status_code == 200
        [mail] = _mails(ready, "associate_application_needs_info")
        assert "Cheque unreadable" in mail["lines"] and mail["to"] == "applicant1@example.test"

    def test_rejection_gives_the_reason(self, client, ready):
        app_id = _apply(client)
        client.post(f"{_app_url(app_id)}/status", json={"status": "under_review"}, headers=ADMIN)
        client.post(f"{_app_url(app_id)}/status", json={"status": "rejected", "note": "Outside our area"},
                    headers=ADMIN)
        [mail] = _mails(ready, "associate_application_rejected")
        assert "Reason: Outside our area" in mail["lines"]

    def test_moving_to_under_review_sends_nothing(self, client, ready):
        app_id = _apply(client)
        client.post(f"{_app_url(app_id)}/status", json={"status": "under_review"}, headers=ADMIN)
        assert not _mails(ready, "associate_application_needs_info") and not _mails(ready,
                                                                                      "associate_application_rejected")

    def test_switched_off_with_notify_associates(self, client, ready, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", False)
        _apply(client)
        assert not _mails(ready, "associate_application_received")


# ── m2: every field error has a stable code ─────────────────────────────────────

def test_field_errors_carry_codes_the_website_can_translate(client, kyc_ready):
    form = {**associate_form(1), "pan": "ABCCE1234F", "pin_code": "012345", "date_of_birth": "2020-01-01",
            "kyc_consent_accepted": "false"}
    form.pop("mobile")
    r = submit(client, ASSOCIATE_APPLY, form)
    assert r.status_code == 422
    codes = {e["loc"][1]: e["code"] for e in r.json()["detail"]}
    assert codes["pan"] == "pan_not_individual" and codes["pin_code"] == "pin_format"
    assert codes["date_of_birth"] == "underage" and codes["mobile"] == "required"
    assert codes["kyc_consent_accepted"] == "consent_required"
    assert all(e["msg"] for e in r.json()["detail"])   # the English fallback stays


# ── M3 / M13: links the staff emails carry exist on the website ─────────────────

def test_staff_email_links_point_at_real_dashboard_pages():
    from app.notifications.notifier import _render_commission_queries_overdue, _staff_message
    _, _, _, url = _staff_message("associate_application_submitted",
                                  {"application_id": 7, "application_type": "preferred_associate", "name": "A",
                                   "state": "WB"}, "https://www.gridnova.in")
    assert url == "https://www.gridnova.in/dashboard/associate-network/preferred_associate/7"
    _, _, text = _render_commission_queries_overdue({"queries": [{"query_id": 1}]}, "https://www.gridnova.in")
    assert "https://www.gridnova.in/dashboard/commissions/queries" in text


# ── M4: a regional Network Manager stays in their regions on every route ────────

class TestRegions:
    @pytest.fixture
    def bihar_nm(self, db_session):
        from app.memory.models import EmployeeRole, EmployeeRoleGrant
        bihar = db_session.execute(select(TerritoryState).where(TerritoryState.lgd_code == BR)).scalar_one()
        db_session.add(Employee(id="emp-nm-br", name="NM Bihar", email="nm.br@gridnova.test",
                                role=EmployeeRole.network_manager, is_active=True, regions=[bihar.name]))
        db_session.flush()
        db_session.add(EmployeeRoleGrant(employee_id="emp-nm-br", role="network_manager", granted_by="test"))
        db_session.commit()
        return {"X-Employee-Id": "emp-nm-br", "X-Employee-Role": "network_manager"}

    def test_west_bengal_application_refused_on_every_route(self, client, ready, bihar_nm):
        app_id = _apply(client)   # a West Bengal applicant
        base = _app_url(app_id)
        calls = [("GET", base, None), ("GET", f"{base}/documents/photo", None),
                 ("POST", f"{base}/status", {"status": "under_review"}),
                 ("POST", f"{base}/approve", {"verification_call_completed": True, "verification_call_note": "x"})]
        for method, url, body in calls:
            r = client.request(method, url, json=body, headers=bihar_nm)
            assert r.status_code == 403 and "outside the regions" in r.text, (url, r.status_code)

    def test_west_bengal_associate_refused_on_every_route(self, client, ready, bihar_nm):
        from tests.associate_support import approve_flow
        gna = approve_flow(client, "associate", _apply(client)).json()["associate"]["associate_id"]
        for method, path, body in (("POST", "status", {"status": "suspended", "reason": "x"}),
                                   ("POST", "clerk-invite", None), ("POST", "documents/regenerate", None),
                                   ("GET", "documents/id_card", None),
                                   ("POST", "reassign-customers", {"to_associate_id": gna, "reason": "x"})):
            r = client.request(method, f"{DASH}/associates/{gna}/{path}", json=body, headers=bihar_nm)
            assert r.status_code == 403, (path, r.status_code, r.text)


# ── M9: Clerk access follows the status ─────────────────────────────────────────

class TestClerkAccess:
    def _associate(self, db_session, **fields):
        from tests.onboarding_support import make_associate
        gna = make_associate(db_session)
        row = db_session.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one()
        for k, v in fields.items():
            setattr(row, k, v)
        db_session.commit()
        return gna

    def test_suspending_a_signed_in_associate_bans_them_and_reinstating_unbans(self, client, territories,
                                                                               db_session, monkeypatch):
        calls = []
        monkeypatch.setattr("app.auth.clerk_client.ban_user", lambda u: calls.append(("ban", u)) or True)
        monkeypatch.setattr("app.auth.clerk_client.unban_user", lambda u: calls.append(("unban", u)) or True)
        gna = self._associate(db_session, clerk_user_id="user_a")
        r = client.post(f"{DASH}/associates/{gna}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["access"]["clerk_banned"] is True
        assert calls == [("ban", "user_a")]
        r = client.post(f"{DASH}/associates/{gna}/status", json={"status": "active"}, headers=ADMIN)
        assert r.json()["access"] == {"clerk_unbanned": True, "clerk_error": None}
        assert calls[-1] == ("unban", "user_a")
        actions = set(db_session.execute(select(AuditLog.action).where(AuditLog.entity_id == gna)).scalars())
        assert {"clerk_access_revoked", "clerk_access_restored"} <= actions

    def test_an_unaccepted_invitation_is_revoked(self, client, territories, db_session, monkeypatch):
        revoked = []
        monkeypatch.setattr("app.auth.clerk_client.revoke_invitation", lambda i: revoked.append(i) or True)
        gna = self._associate(db_session, clerk_invite_status=ClerkInviteStatus.sent, clerk_invitation_id="inv_1")
        r = client.post(f"{DASH}/associates/{gna}/status", json={"status": "deactivated", "reason": "x"},
                        headers=ADMIN)
        assert r.json()["access"]["invitation_revoked"] is True and revoked == ["inv_1"]
        assert r.json()["clerk_invite_status"] is None

    def test_a_clerk_failure_leaves_the_suspension_standing(self, client, territories, db_session, monkeypatch):
        from app.auth.clerk_client import ClerkSyncError

        def boom(_):
            raise ClerkSyncError("Clerk ban failed (HTTP 500)")
        monkeypatch.setattr("app.auth.clerk_client.ban_user", boom)
        gna = self._associate(db_session, clerk_user_id="user_b")
        r = client.post(f"{DASH}/associates/{gna}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "suspended"
        assert r.json()["access"]["clerk_error"] == "Clerk ban failed (HTTP 500)"
        assert client.get("/api/v1/associate/me", headers={"X-Associate-Id": gna}).status_code == 403


# ── B10: a suspended associate's customers can be moved ─────────────────────────

class TestReassignCustomers:
    @pytest.fixture
    def two(self, client, db_session, onboarding_ready):
        from tests.execution_support import make_associate, new_customer
        old = make_associate(db_session)
        new = make_associate(db_session)
        oid = new_customer(client, old)
        return old, new, oid

    def _move(self, client, old, new, **body):
        return client.post(f"{DASH}/associates/{old}/reassign-customers",
                           json={"to_associate_id": new, "reason": "Suspended", **body}, headers=ADMIN)

    def _suspend(self, client, gna):
        r = client.post(f"{DASH}/associates/{gna}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        assert r.status_code == 200
        return r.json()

    def test_an_active_associates_customers_never_move(self, client, two):
        old, new, _ = two
        r = self._move(client, old, new)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "associate_active"

    def test_moved_to_the_new_owner_who_sees_and_works_them(self, client, db_session, two, emails):
        old, new, oid = two
        assert self._suspend(client, old)["open_customers"] == 1
        r = self._move(client, old, new)
        assert r.status_code == 200 and r.json() == {"from_associate_id": old, "to_associate_id": new,
                                                     "onboarding_ids": [oid], "moved": 1}
        db_session.expire_all()
        owner = db_session.get(Associate, db_session.get(CustomerOnboarding, oid).owner_associate_id)
        assert owner.associate_id == new
        mine = client.get("/api/v1/associate/onboardings", headers={"X-Associate-Id": new}).json()
        assert [i["onboarding_id"] for i in mine["items"]] == [oid]
        assert client.patch(f"/api/v1/associate/onboardings/{oid}", json={"floors": 2},
                            headers={"X-Associate-Id": new}).status_code == 200
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "owner_reassigned")).scalar_one()
        assert audit.before_value == {"owner_associate_id": old} and audit.after_value["owner_associate_id"] == new
        [mail] = _mails(emails, "associate_customers_assigned")
        assert mail["subject"] == "1 customer assigned to you"
        assert self._move(client, old, new).json()["detail"]["code"] == "nothing_to_move"

    def test_target_must_be_active_and_different(self, client, db_session, two):
        from tests.execution_support import make_associate
        old, _, _ = two
        self._suspend(client, old)
        gone = make_associate(db_session, status=AssociateStatus.deactivated)
        assert self._move(client, old, gone).json()["detail"]["code"] == "target_not_active"
        assert self._move(client, old, old).json()["detail"]["code"] == "same_associate"
        assert self._move(client, old, "GNA-WB-26-99999").status_code == 404
        assert self._move(client, old, gone, reason=" ").json()["detail"]["code"] == "reason_required"

    def test_unknown_customer_ids_refused(self, client, two):
        old, new, oid = two
        self._suspend(client, old)
        r = self._move(client, old, new, onboarding_ids=[oid, 999999])
        assert r.status_code == 422 and r.json()["detail"]["onboarding_ids"] == [999999]

    def test_an_associate_run_project_needs_an_associate_who_can_run_execution(self, client, db_session, two):
        old, new, oid = two
        onboarding = db_session.get(CustomerOnboarding, oid)
        db_session.add(ProjectExecution(project_id=onboarding.project_id, onboarding_id=oid,
                                        execution_mode=ExecutionMode.associate, started_at=datetime.now(timezone.utc),
                                        files={}, created_at=datetime.now(timezone.utc)))
        db_session.commit()
        self._suspend(client, old)
        r = self._move(client, old, new)   # `new` is a sales-only Associate
        assert r.status_code == 409 and r.json()["detail"]["code"] == "target_cannot_execute"
        assert r.json()["detail"]["onboarding_ids"] == [oid]


# ── m12: no customer emails to an associate who is not active ───────────────────

def test_customer_emails_skip_a_suspended_owner(client, db_session, onboarding_ready, emails):
    from app.notifications import associate_emails
    from tests.execution_support import make_associate, new_customer
    gna = make_associate(db_session)
    oid = new_customer(client, gna)
    onboarding = db_session.get(CustomerOnboarding, oid)
    assert associate_emails._target(db_session, onboarding) is not None
    db_session.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one().status = \
        AssociateStatus.suspended
    db_session.commit()
    assert associate_emails._target(db_session, onboarding) is None


# ── M11: a suspended associate's commission is frozen, not paid ─────────────────

from tests.commission_support import (  # noqa: E402,F401 — fixtures
    ACCOUNTS, accepted, after_month_end, approve_all, make_associate as commission_associate, onboarding_ready,
    publish, record_and_verify, this_period,
)
from tests.payments_support import stub_pdf  # noqa: E402,F401 — fixture


class TestFrozenCommission:
    def test_held_out_of_the_batch_while_suspended(self, client, db_session,
                                                                               onboarding_ready, monkeypatch):
        from app.commissions import periods
        frozen = commission_associate(db_session)
        paid = commission_associate(db_session)
        publish(db_session, overrides={"personal_sale.associate.sales": "10"})
        for gna, ref in ((frozen, "UTR-F"), (paid, "UTR-P")):
            oid = accepted(client, db_session, gna, stage=ProjectStage.agreement_signed)
            record_and_verify(client, oid, "11000.00", ref)
        approve_all(client, db_session)
        assert client.post(f"{DASH}/associates/{frozen}/status", json={"status": "suspended", "reason": "x"},
                           headers=ADMIN).status_code == 200
        period, day = this_period(), after_month_end()
        monkeypatch.setattr(periods, "today_ist", lambda: day)
        r = client.post(f"{DASH}/payout-batches", json={"period": period}, headers=ACCOUNTS)
        assert r.status_code == 201, r.text
        assert [ln["associate"]["associate_id"] for ln in r.json()["lines"]] == [paid]
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payout_batch",
                                                          AuditLog.action == "prepared")).scalar_one()
        assert len(audit.after_value["frozen_entries"]) == 1

    def test_a_month_with_only_frozen_commission_says_so(self, client, db_session, onboarding_ready, monkeypatch):
        from app.commissions import periods
        gna = commission_associate(db_session)
        publish(db_session, overrides={"personal_sale.associate.sales": "10"})
        record_and_verify(client, accepted(client, db_session, gna, stage=ProjectStage.agreement_signed),
                          "11000.00", "UTR-ONLY")
        approve_all(client, db_session)
        client.post(f"{DASH}/associates/{gna}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        period, day = this_period(), after_month_end()
        monkeypatch.setattr(periods, "today_ist", lambda: day)
        r = client.post(f"{DASH}/payout-batches", json={"period": period}, headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"] == {
            "code": "no_approved_entries", "message": "There is no approved commission to pay for this month.",
            "frozen_entries": 1}


# ── Data protection: a second associate's commission ids ────────────────────────

def test_commission_queries_refuse_another_associates_entry(client, db_session, onboarding_ready):
    from app.memory.models import CommissionEntry
    owner = commission_associate(db_session)
    other = commission_associate(db_session)
    publish(db_session, overrides={"personal_sale.associate.sales": "10"})
    record_and_verify(client, accepted(client, db_session, owner, stage=ProjectStage.agreement_signed),
                      "11000.00", "UTR-DP")
    entry = db_session.execute(select(CommissionEntry)).scalars().first()
    them = {"X-Associate-Id": other}
    assert client.get(f"/api/v1/associate/commissions/{entry.id}/queries", headers=them).status_code == 404
    r = client.post(f"/api/v1/associate/commissions/{entry.id}/queries", json={"message": "mine?"}, headers=them)
    assert r.status_code == 404
    assert client.get("/api/v1/associate/commissions", headers=them).json()["total"] == 0
    assert client.get("/api/v1/associate/payouts", headers=them).json()["items"] == []
    assert client.post(f"/api/v1/associate/commissions/{entry.id}/queries", json={"message": "query"},
                       headers={"X-Associate-Id": owner}).status_code == 201


# ── Found by the scenario test: commission basis on a BE-13 proposal ────────────

def test_commission_basis_reads_the_accepted_document_price():
    from app.commissions.service import accepted_price
    from app.memory.models import Proposal
    from app.proposals import document as d
    from tests.studio_support import residential_3kw_pmsg
    doc = residential_3kw_pmsg()
    figs = d.figures(doc)
    assert figs["basic"] and figs["total"]
    priced = Proposal(content={"document": d.dump(doc), "pricing": {"price_status": "custom_quote"}})
    assert accepted_price(priced) == {"basic": figs["basic"], "final": figs["total"]}
    legacy = Proposal(content={"pricing": {"basic": "100000.00", "final": "108900.00"}})
    assert accepted_price(legacy) == {"basic": "100000.00", "final": "108900.00"}
    assert accepted_price(Proposal(content={})) == {}
