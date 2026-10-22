"""BE-10 — Associate ease: referral links and the estimated commission
(docs/api/associate_ease.md).

Referral: a public estimate request / call booking with ?ref=<active Associate ID> is
recorded on the lead and audited; anything else is ignored with the normal success
response. The referring Associate sees only their own referred enquiries, with the
customer's mobile only when the customer consented. Staff see and filter by the referrer.

Estimated commission: the ledger's own basis, rate and amount rules, nothing written.
Rates are TEST values (tests/commission_support.py)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

import app.api.routes.public_booking as public_booking
import app.api.routes.public_intake as public_intake
from app.commissions import config as commission_config
from app.memory.models import (
    AssociateCapability, AssociateStatus, AuditLog, CommissionEntry, CustomerOnboarding, IndicativeEstimate, Lead,
    PipelineStage, ProjectStage,
)
from app.onboarding import config as onboarding_config
from app.scheduling import google_calendar
from tests.commission_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, BASIC, DASH, FINAL, SALES, accepted, entries, headers, kyc_ready, make_associate,
    new_customer, onboarding_ready, pk, publish, record_and_verify, territories,
)
from tests.payments_support import stub_pdf  # noqa: F401 — fixture

D = Decimal
ESTIMATE = "/api/v1/public/estimate-request"
BOOKING = "/api/v1/public/booking"
ONB = "/api/v1/associate/onboardings"


def _estimate(client, email: str, **extra):
    body = {"name": "Referred Customer", "email": email, "phone": "9830012345", "state": "West Bengal",
            "city": "Siliguri", "pin_code": "734001", **extra}
    r = client.post(ESTIMATE, json=body)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"lead_id", "success"}      # nothing about the referral
    return r.json()["lead_id"]


def _lead(db, lead_id: int) -> Lead:
    db.expire_all()
    return db.get(Lead, lead_id)


def _referral_audits(db) -> list[AuditLog]:
    return list(db.execute(select(AuditLog).where(AuditLog.action == "referral_recorded")).scalars())


@pytest.fixture
def gna(db_session, territories):
    return make_associate(db_session)


# ── B1 — the referral is recorded ────────────────────────────────────────────────

class TestReferralRecorded:
    def test_a_valid_id_is_stored_and_audited(self, client, db_session, gna):
        lead_id = _estimate(client, "ref-ok@example.com", ref=gna)
        lead = _lead(db_session, lead_id)
        assert lead.referred_by_associate_id == pk(db_session, gna)
        assert lead.share_with_referrer is False
        (audit,) = _referral_audits(db_session)
        assert (audit.entity_type, audit.entity_id, audit.performed_by) == ("lead", str(lead_id), "website")
        assert audit.after_value == {"referred_by_associate_id": gna, "share_with_referrer": False,
                                     "source": "website_estimate_form"}

    def test_a_hand_typed_id_is_normalised(self, client, db_session, gna):
        lead_id = _estimate(client, "ref-lower@example.com", ref=f"  {gna.lower()} ")
        assert _lead(db_session, lead_id).referred_by_associate_id == pk(db_session, gna)

    @pytest.mark.parametrize("ref", ["GNA-WB-26-99999", "not-an-id", "", "GNA-WB-26-0001", 12345, None,
                                     ["GNA-WB-26-90001"], "G" * 500])
    def test_unknown_or_malformed_is_ignored_silently(self, client, db_session, gna, ref):
        lead_id = _estimate(client, "ref-bad@example.com", ref=ref, share_with_referrer=True)
        lead = _lead(db_session, lead_id)
        assert lead.referred_by_associate_id is None and lead.share_with_referrer is False
        assert _referral_audits(db_session) == []

    @pytest.mark.parametrize("status", [AssociateStatus.suspended, AssociateStatus.deactivated])
    def test_an_inactive_associate_is_ignored_silently(self, client, db_session, territories, status):
        inactive = make_associate(db_session, status=status)
        lead_id = _estimate(client, "ref-inactive@example.com", ref=inactive)
        assert _lead(db_session, lead_id).referred_by_associate_id is None
        assert _referral_audits(db_session) == []

    def test_the_consent_flag_is_stored(self, client, db_session, gna):
        lead_id = _estimate(client, "ref-consent@example.com", ref=gna, share_with_referrer=True)
        assert _lead(db_session, lead_id).share_with_referrer is True

    def test_a_booked_call_is_recorded_too(self, client, db_session, memory, gna, monkeypatch):
        from tests.booking_support import grid_slot
        from tests.test_public_booking import _connect_employee
        from app.config import settings
        from app.security.encryption import generate_key
        monkeypatch.setattr(settings, "fernet_encryption_key", settings.fernet_encryption_key or generate_key())
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(google_calendar, "insert_event",
                            lambda **k: {"google_event_id": "ev-ref-1", "meet_link": None})
        r = client.post(BOOKING, json={"name": "Caller", "email": "ref-call@example.com", "phone": "9830099999",
                                       "scheduled_at": grid_slot(0).isoformat(), "ref": gna,
                                       "share_with_referrer": True})
        assert r.status_code == 200, r.text
        assert "ref" not in r.json() and "referred_by" not in r.json()
        lead = _lead(db_session, r.json()["lead_id"])
        assert lead.referred_by_associate_id == pk(db_session, gna) and lead.share_with_referrer is True
        items = client.get("/api/v1/associate/referrals", headers=headers(gna)).json()["items"]
        assert [(i["enquiry_kind"], i["customer_mobile"]) for i in items] == [("call_booked", "9830099999")]

    def test_rate_limits_are_unchanged(self, client, gna):
        assert (public_intake._RATE_LIMIT, public_booking._BOOKING_RATE_LIMIT) == (10, 5)
        for n in range(public_intake._RATE_LIMIT):
            assert client.post(ESTIMATE, json={"name": "x", "email": f"rl{n}@example.com", "ref": gna}).status_code == 200
        assert client.post(ESTIMATE, json={"name": "x", "email": "rl-last@example.com", "ref": gna}).status_code == 429

    def test_a_referral_does_not_attribute(self, client, db_session, gna):
        """REF-1: information only — no onboarding is created or claimed, and the config
        refuses automatic attribution (not built)."""
        _estimate(client, "ref-attr@example.com", ref=gna)
        assert db_session.execute(select(func.count()).select_from(CustomerOnboarding)).scalar_one() == 0
        data = {**onboarding_config.load(), "referrals": {"auto_attribute": True}}
        with pytest.raises(onboarding_config.OnboardingConfigError):
            onboarding_config._validate(data)


# ── B1 — the associate's view ────────────────────────────────────────────────────

class TestAssociateReferrals:
    def test_the_referral_link(self, client, gna):
        r = client.get("/api/v1/associate/referral-link", headers=headers(gna))
        assert r.status_code == 200
        assert r.json() == {"associate_id": gna, "share_path": f"/estimate?ref={gna}"}

    def test_own_referrals_newest_first_mobile_only_with_consent(self, client, db_session, gna):
        first = _estimate(client, "ref-a@example.com", ref=gna)
        second = _estimate(client, "ref-b@example.com", ref=gna, share_with_referrer=True, phone="9830011111")
        r = client.get("/api/v1/associate/referrals", headers=headers(gna))
        assert r.status_code == 200
        body = r.json()
        assert (body["total"], body["page"], [i["lead_id"] for i in body["items"]]) == (2, 1, [second, first])
        consented, silent = body["items"]
        assert consented["customer_mobile"] == "9830011111" and consented["share_with_referrer"] is True
        assert "customer_mobile" not in silent and silent["share_with_referrer"] is False
        assert silent == {"lead_id": first, "created_at": silent["created_at"], "customer_name": "Referred Customer",
                          "location": "Siliguri, West Bengal", "pin_code": "734001", "enquiry_kind": "estimate",
                          "status": "in_progress", "share_with_referrer": False}   # the form qualifies it
        assert not any("email" in k for item in body["items"] for k in item)

    def test_a_plain_status(self, client, db_session, gna):
        lead_id = _estimate(client, "ref-won@example.com", ref=gna)
        _lead(db_session, lead_id).pipeline_stage = PipelineStage.won
        db_session.commit()
        (item,) = client.get("/api/v1/associate/referrals", headers=headers(gna)).json()["items"]
        assert item["status"] == "won"

    def test_an_associate_sees_only_their_own(self, client, db_session, gna):
        other = make_associate(db_session)
        preferred = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        _estimate(client, "ref-mine@example.com", ref=gna)
        _estimate(client, "ref-none@example.com")
        for who in (other, preferred):
            body = client.get("/api/v1/associate/referrals", headers=headers(who)).json()
            assert (body["items"], body["total"]) == ([], 0)
        assert client.get("/api/v1/associate/referrals", headers=headers(gna)).json()["total"] == 1

    def test_paged(self, client, gna):
        for n in range(3):
            _estimate(client, f"ref-page{n}@example.com", ref=gna)
        body = client.get("/api/v1/associate/referrals", params={"page": 2, "page_size": 2},
                          headers=headers(gna)).json()
        assert (len(body["items"]), body["total"], body["page_size"]) == (1, 3, 2)

    def test_staff_cannot_call_it(self, client, gna):
        assert client.get("/api/v1/associate/referrals", headers=ADMIN).status_code in (401, 403)


# ── B1 — staff ───────────────────────────────────────────────────────────────────

class TestStaffLeads:
    def test_list_and_detail_name_the_referrer(self, client, db_session, gna):
        referred = _estimate(client, "ref-staff@example.com", ref=gna)
        plain = _estimate(client, "ref-plain@example.com")
        items = {i["id"]: i for i in client.get(f"{DASH}/leads", headers=ADMIN).json()["items"]}
        full_name = client.get(f"{DASH}/associates/{gna}", headers=ADMIN).json()["full_name"]
        assert items[referred]["referred_by"] == {"associate_id": gna, "full_name": full_name}
        assert items[plain]["referred_by"] is None
        detail = client.get(f"{DASH}/leads/{referred}", headers=ADMIN).json()
        assert detail["referred_by"] == {"associate_id": gna, "full_name": full_name}
        assert client.get(f"{DASH}/leads/{plain}", headers=ADMIN).json()["referred_by"] is None

    def test_filter_by_referrer(self, client, db_session, gna):
        other = make_associate(db_session)
        mine = _estimate(client, "ref-f1@example.com", ref=gna)
        _estimate(client, "ref-f2@example.com", ref=other)
        _estimate(client, "ref-f3@example.com")
        r = client.get(f"{DASH}/leads", params={"referred_by_associate_id": gna.lower()}, headers=ADMIN)
        assert r.status_code == 200
        assert ([i["id"] for i in r.json()["items"]], r.json()["total"]) == ([mine], 1)
        r = client.get(f"{DASH}/leads", params={"referred_by_associate_id": "GNA-WB-26-99999"}, headers=ADMIN)
        assert r.json()["total"] == 0
        r = client.get(f"{DASH}/leads", params={"referred_by_associate_id": "nope"}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_associate_id"
        csv = client.get(f"{DASH}/leads/export", params={"referred_by_associate_id": gna}, headers=ADMIN)
        assert csv.status_code == 200 and len(csv.text.strip().splitlines()) == 2   # header + 1

    def test_the_filter_stays_inside_the_role_scope(self, client, db_session, gna):
        lead_id = _estimate(client, "ref-scope@example.com", ref=gna)
        _lead(db_session, lead_id).assigned_employee_id = "emp-admin-1"
        db_session.commit()
        r = client.get(f"{DASH}/leads", params={"referred_by_associate_id": gna}, headers=SALES)
        assert r.status_code == 200 and r.json()["total"] == 0     # Sales: own and unassigned only


# ── B2 — estimated commission ────────────────────────────────────────────────────

def _detail(client, gna: str, oid: int) -> dict:
    r = client.get(f"{ONB}/{oid}", headers=headers(gna))
    assert r.status_code == 200, r.text
    return r.json()["estimated_commission"]


def _listed(client, gna: str, oid: int) -> dict:
    items = client.get(ONB, headers=headers(gna)).json()["items"]
    return next(i for i in items if i["onboarding_id"] == oid)["estimated_commission"]


def _counts(db) -> tuple:
    return (db.execute(select(func.count()).select_from(CommissionEntry)).scalar_one(),
            db.execute(select(func.count()).select_from(AuditLog)).scalar_one())


class TestEstimatedCommission:
    def test_null_without_a_proposal_or_estimate(self, client, db_session, onboarding_ready):
        owner = make_associate(db_session)
        oid = new_customer(client, owner)
        assert _detail(client, owner, oid) is None and _listed(client, owner, oid) is None

    def test_on_the_accepted_proposal_at_the_confirmed_rate(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session)                        # sales: 5 %
        oid = accepted(client, db_session, owner)
        expected = {"amount": "5000.00", "rate": "5.0000", "basis": "accepted_proposal",
                    "value_used": "108900.00", "basis_amount": "100000.00", "pending_reason": None}
        assert _detail(client, owner, oid) == expected
        assert _listed(client, owner, oid) == expected

    def test_the_rate_is_the_associates_own(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session, capability=AssociateCapability.execution)   # 6 %
        oid = accepted(client, db_session, owner)
        assert _detail(client, owner, oid)["amount"] == "6000.00"
        preferred = make_associate(db_session, level="district_head", territory=ALIPURDUAR)  # 4.5 %
        oid = accepted(client, db_session, preferred)
        assert (_detail(client, preferred, oid)["amount"], _detail(client, preferred, oid)["rate"]) == \
            ("4500.00", "4.5000")

    @pytest.mark.parametrize("placeholder", [False, True])
    def test_without_confirmed_rates_there_is_no_amount(self, client, db_session, onboarding_ready, placeholder):
        if placeholder:
            publish(db_session, is_placeholder=True)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner)
        assert _detail(client, owner, oid) == {"amount": None, "rate": None, "basis": "accepted_proposal",
                                               "value_used": "108900.00", "basis_amount": "100000.00",
                                               "pending_reason": "placeholder_rates"}

    def test_a_custom_quote_has_no_basis(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, content={"pricing": {"price_status": "custom_quote"}},
                       total=D("150000.00"))
        got = _detail(client, owner, oid)
        assert (got["amount"], got["rate"], got["basis_amount"], got["pending_reason"]) == \
            (None, "5.0000", None, "basis_unavailable")

    def test_gross_basis_follows_the_config(self, client, db_session, onboarding_ready, monkeypatch):
        monkeypatch.setitem(commission_config.load(), "commissions",
                            {**commission_config.commissions(), "basis": "gross"})
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner)
        assert _detail(client, owner, oid)["amount"] == "5445.00"           # 5 % of 1,08,900

    def test_on_the_indicative_estimate(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session)
        oid = new_customer(client, owner, connection_phase="single_phase")
        r = client.post(f"{ONB}/{oid}/estimates", json={}, headers=headers(owner))
        assert r.status_code == 201, r.text
        packages = r.json()["result"]["packages"]
        assert r.json()["chosen_packages"] == ["NOVA_ASSURED", "NOVA_SIGNATURE"]
        cheaper = min(packages.values(), key=lambda p: D(p["final"]))    # C5-EST: lowest_final
        basis = (D(cheaper["final"]) * D(cheaper["basic"]) / D(cheaper["final"])).quantize(D("0.01"))
        got = _detail(client, owner, oid)
        assert got == {"amount": str((basis * 5 / 100).quantize(D("0.01"))), "rate": "5.0000",
                       "basis": "indicative_estimate", "value_used": cheaper["final"],
                       "basis_amount": str(basis), "pending_reason": None}

    def test_the_package_rule_is_config(self, client, db_session, onboarding_ready, monkeypatch):
        publish(db_session)
        owner = make_associate(db_session)
        oid = new_customer(client, owner, connection_phase="single_phase")
        packages = client.post(f"{ONB}/{oid}/estimates", json={}, headers=headers(owner)).json()["result"]["packages"]
        monkeypatch.setitem(commission_config.load(), "estimate", {"indicative_package_when_several": "NOVA_SIGNATURE"})
        assert _detail(client, owner, oid)["value_used"] == packages["NOVA_SIGNATURE"]["final"]

    def test_an_accepted_proposal_wins_over_an_estimate(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, connection_phase="single_phase")
        assert client.post(f"{ONB}/{oid}/estimates", json={}, headers=headers(owner)).status_code == 201
        assert _detail(client, owner, oid)["basis"] == "accepted_proposal"

    def test_matches_what_the_ledger_records_and_writes_nothing(self, client, db_session, onboarding_ready,
                                                                  stub_pdf):
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        before = _counts(db_session)
        estimate = _detail(client, owner, oid)
        _listed(client, owner, oid)
        assert _counts(db_session) == before                      # no ledger row, no audit row
        assert db_session.execute(select(func.count()).select_from(IndicativeEstimate)).scalar_one() == 0
        pid = record_and_verify(client, oid, str(FINAL), "UTR-EST-FULL")   # the whole value, one payment
        (earned,) = [e for e in entries(db_session, payment_id=pid) if e.earning_role.value == "personal_sale"]
        assert (str(earned.amount), str(earned.basis_amount)) == (estimate["amount"], estimate["basis_amount"])
        assert earned.basis_amount == BASIC

    def test_a_territory_viewer_sees_no_money(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session, home_district=ALIPURDUAR)
        district = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        oid = accepted(client, db_session, owner)
        assert _detail(client, owner, oid)["amount"] == "5000.00"
        assert _detail(client, district, oid) is None
        assert _listed(client, district, oid) is None

    def test_a_closed_customer_has_none(self, client, db_session, onboarding_ready):
        from app.memory.models import OnboardingStatus
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner)
        db_session.get(CustomerOnboarding, oid).status = OnboardingStatus.closed
        db_session.commit()
        assert _detail(client, owner, oid) is None

    def test_the_list_costs_a_fixed_number_of_queries(self, client, db_session, onboarding_ready):
        from sqlalchemy import event
        publish(db_session)
        owner = make_associate(db_session)
        engine = db_session.get_bind()
        counts = []
        for n in (1, 5):
            while len(client.get(ONB, headers=headers(owner)).json()["items"]) < n:
                oid = accepted(client, db_session, owner, connection_phase="single_phase")
                assert client.post(f"{ONB}/{oid}/estimates", json={}, headers=headers(owner)).status_code == 201
            statements = []

            def count(*args, **kwargs):
                statements.append(1)
            event.listen(engine, "before_cursor_execute", count)
            items = client.get(ONB, headers=headers(owner)).json()["items"]
            event.remove(engine, "before_cursor_execute", count)
            assert all(i["estimated_commission"]["amount"] == "5000.00" for i in items)
            counts.append(len(statements))
        assert counts[0] == counts[1], f"queries grew with the page: {counts}"
