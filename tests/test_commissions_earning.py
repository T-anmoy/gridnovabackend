"""BE-4 Phase 1 — commission rate schedules, earning on verified payments, rating,
reversal, approval and the associate's view (Commission & Payout Policy 4.1–4.4, 4.7).

Money is asserted with exact Decimals. Rates are TEST values (tests/commission_support.py)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.commissions import config as commission_config
from app.commissions import periods, rates
from app.commissions import service as commissions
from app.memory.models import (
    Associate, AssociateStatus, AuditLog, CommissionEntry, CommissionEntryKind, CommissionRate, CommissionRateSchedule, Payment,
    PaymentReceipt, PaymentStatus, ProjectStage,
)
from tests.commission_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, ADMIN_2, ALIPURDUAR_I, DASH, ENG, MGMT, OPS, SALES, TEST_RATES, accepted, approve_all, by_role, dishonour,
    entries, headers, kyc_ready, make_associate, network, onboarding_ready, pk, publish, record_and_verify,
    record_cheque_and_verify, seat, territories,
)
from tests.payments_support import RECORDER, stub_pdf  # noqa: F401 — fixture

D = Decimal


@pytest.fixture
def scene(client, db_session, onboarding_ready):
    net = network(db_session)
    owner = make_associate(db_session)
    oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
    return {"owner": owner, "oid": oid, **net}


def _cfg(monkeypatch, **values):
    monkeypatch.setitem(commission_config.load(), "commissions", {**commission_config.commissions(), **values})


# ── Earning ──────────────────────────────────────────────────────────────────────

class TestEarning:
    def test_a_recorded_but_unverified_payment_earns_nothing(self, client, db_session, scene):
        r = client.post(f"{DASH}/onboardings/{scene['oid']}/payments", headers=RECORDER,
                        json={"amount": "21780.00", "mode": "neft", "reference": "UTR-NOT-YET"})
        assert r.status_code == 201
        assert entries(db_session) == []

    def test_without_confirmed_rates_entries_are_recorded_with_no_amount(self, client, db_session, scene):
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        rows = by_role(entries(db_session, payment_id=pid))
        assert set(rows) == {"personal_sale", "territory_state", "territory_district", "territory_block"}
        for e in rows.values():
            assert e.basis_amount == D("20000.00")          # 21,780 × 1,00,000 ÷ 1,08,900
            assert e.amount is None and e.rate_percent is None and e.pending_reason == "placeholder_rates"
            assert e.agreement_date == periods.today_ist() and e.status.value == "earned"
        assert rows["personal_sale"].associate_id == pk(db_session, scene["owner"])
        assert rows["territory_block"].associate_id == pk(db_session, scene["block"])

    def test_a_placeholder_schedule_is_no_better_than_none(self, client, db_session, scene):
        publish(db_session, is_placeholder=True)
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        assert {e.pending_reason for e in entries(db_session, payment_id=pid)} == {"placeholder_rates"}

    def test_confirmed_rates_give_exact_amounts(self, client, db_session, scene):
        schedule = publish(db_session)
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        rows = by_role(entries(db_session, payment_id=pid))
        assert {k: e.amount for k, e in rows.items()} == {
            "personal_sale": D("1000.00"), "territory_state": D("100.00"),
            "territory_district": D("200.00"), "territory_block": D("300.00")}
        assert rows["personal_sale"].rate_percent == D("5.0000")
        assert all(e.schedule_id == schedule.id and e.pending_reason is None for e in rows.values())
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payment",
                                                          AuditLog.action == "commission_earned")).scalar_one()
        assert len(audit.after_value["entries"]) == 4

    def test_commission_is_in_proportion_to_the_amount_verified(self, client, db_session, scene):
        publish(db_session)
        pid = record_and_verify(client, scene["oid"], "65340.00", "UTR-60")     # the 60 % milestone
        rows = by_role(entries(db_session, payment_id=pid))
        assert rows["personal_sale"].basis_amount == D("60000.00")
        assert rows["personal_sale"].amount == D("3000.00")

    def test_gross_basis(self, client, db_session, scene, monkeypatch):
        _cfg(monkeypatch, basis="gross")
        publish(db_session)
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        assert by_role(entries(db_session, payment_id=pid))["personal_sale"].amount == D("1089.00")

    def test_a_custom_quote_has_no_basis(self, client, db_session, onboarding_ready):
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, content={"pricing": {"price_status": "custom_quote"}},
                       total=D("150000.00"), stage=ProjectStage.agreement_signed)
        pid = record_and_verify(client, oid, "30000.00", "UTR-CQ")
        (e,) = entries(db_session, payment_id=pid)
        assert e.basis_amount is None and e.amount is None and e.pending_reason == "basis_unavailable"

    def test_a_payment_without_an_associate_customer_earns_nothing(self, db_session, scene):
        payment = Payment(project_id=1, amount=D("10.00"))
        assert commissions.on_payment_verified(db_session, payment, "x") == []

    def test_twice_is_refused(self, client, db_session, scene):
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        assert client.post(f"{DASH}/payments/{pid}/verify", headers=ADMIN).status_code == 409
        assert len(entries(db_session, payment_id=pid)) == 4
        with pytest.raises(Exception):
            commissions.on_payment_verified(db_session, db_session.get(Payment, pid), "again")
        db_session.rollback()
        assert len(entries(db_session, payment_id=pid)) == 4


class TestRateDate:
    def test_the_rate_in_force_on_the_agreement_date_applies(self, client, db_session, scene):
        publish(db_session, effective_from=date(2026, 1, 1))
        later = publish(db_session, effective_from=periods.today_ist() + timedelta(days=20),
                        overrides={"personal_sale.associate.sales": "9"})
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        personal = by_role(entries(db_session, payment_id=pid))["personal_sale"]
        assert personal.rate_percent == D("5.0000") and personal.schedule_id != later.id

    def test_in_force_picks_the_latest_effective_date(self, db_session, onboarding_ready):
        v1 = publish(db_session, effective_from=date(2026, 1, 1))
        v2 = publish(db_session, effective_from=date(2026, 6, 1))
        assert rates.in_force(db_session, date(2025, 12, 31)) is None
        assert rates.in_force(db_session, date(2026, 5, 31)).id == v1.id
        assert rates.in_force(db_session, date(2026, 6, 1)).id == v2.id

    def test_paid_before_the_agreement_is_rated_when_it_is_signed(self, client, db_session, onboarding_ready):
        net = network(db_session)
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner)                       # accepted, not yet signed
        pid = record_and_verify(client, oid, "21780.00", "UTR-ADV")
        (personal,) = entries(db_session, payment_id=pid)                # no territory yet: no seat date
        assert personal.pending_reason == "agreement_not_signed" and personal.agreement_date is None
        advance_to_signed(db_session, oid)
        rows = by_role(entries(db_session, payment_id=pid))
        assert {k: e.amount for k, e in rows.items()} == {
            "personal_sale": D("1000.00"), "territory_state": D("100.00"),
            "territory_district": D("200.00"), "territory_block": D("300.00")}
        assert rows["territory_state"].associate_id == pk(db_session, net["state"])
        assert all(e.agreement_date == periods.today_ist() for e in rows.values())

    def test_seat_holder_at_payment_date(self, client, db_session, onboarding_ready, monkeypatch):
        _cfg(monkeypatch, territory_seat_holder_at="payment_verified")
        network(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner)
        pid = record_and_verify(client, oid, "21780.00", "UTR-ADV")
        rows = by_role(entries(db_session, payment_id=pid))
        assert len(rows) == 4 and {e.pending_reason for e in rows.values()} == {"agreement_not_signed"}


def advance_to_signed(db, oid):
    from tests.commission_support import advance_to
    advance_to(db, oid, ProjectStage.agreement_signed)


class TestTerritory:
    def test_a_released_seat_earns_nothing_and_two_state_heads_share(self, client, db_session, onboarding_ready):
        from app.associates import service as associate_service
        from tests.associate_support import WB
        heads = [make_associate(db_session, level="state_head", territory=WB) for _ in range(2)]
        first, second = (seat(db_session, g) for g in heads)
        associate_service.release_seat(db_session, second, "emp-admin-1", "moved on")
        db_session.commit()
        third = make_associate(db_session, level="state_head", territory=WB)
        seat(db_session, third, back_days=0)
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        pid = record_and_verify(client, oid, "21780.00", "UTR-1")
        state = [e for e in entries(db_session, payment_id=pid) if e.earning_role.value == "territory_state"]
        assert {e.associate_id for e in state} == {pk(db_session, heads[0]), pk(db_session, third)}
        assert [e.amount for e in state] == [D("50.00"), D("50.00")] and {e.holders for e in state} == {2}

    def test_shares_keep_every_paisa(self):
        assert commissions.equal_parts(D("100.01"), 3) == [D("33.34"), D("33.34"), D("33.33")]
        assert sum(commissions.equal_parts(D("0.05"), 2)) == D("0.05")

    def test_each_full_rate(self, client, db_session, onboarding_ready, monkeypatch):
        from tests.associate_support import WB
        _cfg(monkeypatch, multiple_territory_holders="each_full_rate")
        for _ in range(2):
            seat(db_session, make_associate(db_session, level="state_head", territory=WB))
        publish(db_session)
        oid = accepted(client, db_session, make_associate(db_session), stage=ProjectStage.agreement_signed)
        pid = record_and_verify(client, oid, "21780.00", "UTR-1")
        assert [e.amount for e in entries(db_session, payment_id=pid, holders=1)
                if e.earning_role.value == "territory_state"] == [D("100.00"), D("100.00")]

    def test_own_customer_in_own_territory(self, client, db_session, onboarding_ready, monkeypatch):
        block = make_associate(db_session, level="block", territory=ALIPURDUAR_I)
        seat(db_session, block)
        publish(db_session)
        oid = accepted(client, db_session, block, stage=ProjectStage.agreement_signed)
        pid = record_and_verify(client, oid, "21780.00", "UTR-1")
        rows = by_role(entries(db_session, payment_id=pid))
        assert rows["personal_sale"].amount == D("950.00")          # 4.75 % for a Block Associate
        assert rows["territory_block"].associate_id == pk(db_session, block)
        _cfg(monkeypatch, territory_on_own_customers=False)
        oid2 = accepted(client, db_session, block, stage=ProjectStage.agreement_signed)
        pid2 = record_and_verify(client, oid2, "21780.00", "UTR-2")
        assert set(by_role(entries(db_session, payment_id=pid2))) == {"personal_sale"}

    def test_a_deactivated_associate_earns_nothing_new(self, client, db_session, scene):
        a = db_session.get(Associate, pk(db_session, scene["owner"]))
        a.status = AssociateStatus.deactivated
        db_session.commit()
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        assert "personal_sale" not in by_role(entries(db_session, payment_id=pid))


# ── Rate schedules (route) ───────────────────────────────────────────────────────

class TestSchedules:
    URL = f"{DASH}/commission-rate-schedules"

    def test_publish_needs_notice_every_key_and_admin(self, client, db_session, onboarding_ready):
        soon = (periods.today_ist() + timedelta(days=3)).isoformat()
        r = client.post(self.URL, json={"effective_from": soon, "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "notice_period"
        earliest = periods.today_ist() + timedelta(days=commission_config.commissions()["rate_change_notice_days"])
        assert r.json()["detail"]["earliest_effective_from"] == earliest.isoformat()
        r = client.post(self.URL, json={"effective_from": earliest.isoformat(),
                                        "rates": {k: v for k, v in TEST_RATES.items() if k != "territory_block"}},
                        headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["missing"] == ["territory_block"]
        r = client.post(self.URL, json={"effective_from": earliest.isoformat(),
                                        "rates": {**TEST_RATES, "territory_block": "101"}}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_rate"
        for who in (ACCOUNTS, MGMT, SALES):
            assert client.post(self.URL, json={"effective_from": earliest.isoformat(), "rates": TEST_RATES},
                               headers=who).status_code == 403
        r = client.post(self.URL, json={"effective_from": earliest.isoformat(), "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["version"] == 1 and body["rates"]["territory_block"] == "1.5000" and not body["in_effect"]
        listing = client.get(self.URL, headers=MGMT).json()
        assert [s["schedule_id"] for s in listing["items"]] == [body["schedule_id"]]
        assert listing["in_force_schedule_id"] is None and listing["rate_keys"] == list(rates.RATE_KEYS)
        assert client.get(self.URL, headers=OPS).status_code == 403

    def test_a_placeholder_schedule_needs_no_notice_but_not_the_past(self, client, db_session, onboarding_ready):
        today = periods.today_ist()
        r = client.post(self.URL, json={"effective_from": today.isoformat(), "is_placeholder": True,
                                        "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 201 and r.json()["is_placeholder"] and r.json()["in_effect"]
        r = client.post(self.URL, json={"effective_from": (today - timedelta(days=1)).isoformat(),
                                        "is_placeholder": True, "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "effective_in_past"

    def test_a_published_schedule_never_changes(self, db_session, onboarding_ready):
        schedule = publish(db_session)
        rate = db_session.execute(select(CommissionRate).where(CommissionRate.schedule_id == schedule.id)).scalars().first()
        for statement in (f"UPDATE commission_rates SET rate_percent = 9 WHERE id = {rate.id}",
                          f"DELETE FROM commission_rate_schedules WHERE id = {schedule.id}"):
            with pytest.raises((IntegrityError, DBAPIError)):
                db_session.execute(text(statement))
            db_session.rollback()


# ── Re-rating (worked example 2) ─────────────────────────────────────────────────

class TestReRate:
    def test_placeholder_period_then_re_rate(self, client, db_session, scene):
        p1 = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        p2 = record_and_verify(client, scene["oid"], "65340.00", "UTR-2")
        assert {e.pending_reason for e in entries(db_session)} == {"placeholder_rates"}
        placeholder = publish(db_session, is_placeholder=True)
        url = f"{DASH}/commission-rate-schedules/{placeholder.id}/re-rate"
        r = client.post(url, json={"reason": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "schedule_is_placeholder"
        real = publish(db_session, effective_from=periods.today_ist() + timedelta(days=15))
        url = f"{DASH}/commission-rate-schedules/{real.id}/re-rate"
        assert client.post(url, json={"reason": "Rates confirmed by Grid Nova"}, headers=ACCOUNTS).status_code == 403
        r = client.post(url, json={"reason": "Rates confirmed by Grid Nova"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == {"schedule_id": real.id, "rated": 8, "total_amount": "6400.00", "still_pending": 0}
        assert by_role(entries(db_session, payment_id=p1))["personal_sale"].amount == D("1000.00")
        assert by_role(entries(db_session, payment_id=p2))["territory_block"].amount == D("900.00")
        assert {e.schedule_id for e in entries(db_session)} == {real.id}
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "re_rated")).scalar_one()
        assert audit.after_value["reason"] == "Rates confirmed by Grid Nova" and audit.performed_by == "emp-admin-1"
        again = client.post(url, json={"reason": "again"}, headers=ADMIN).json()
        assert again["rated"] == 0


# ── The ledger cannot be rewritten ───────────────────────────────────────────────

class TestAppendOnly:
    def test_an_amount_once_set_never_changes_and_nothing_is_deleted(self, client, db_session, scene):
        publish(db_session)
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        e = entries(db_session, payment_id=pid)[0]
        for statement in (f"UPDATE commission_entries SET amount = 1 WHERE id = {e.id}",
                          f"UPDATE commission_entries SET associate_id = associate_id + 1 WHERE id = {e.id}",
                          f"DELETE FROM commission_entries WHERE id = {e.id}"):
            with pytest.raises((IntegrityError, DBAPIError)):
                db_session.execute(text(statement))
            db_session.rollback()
        db_session.execute(text(f"UPDATE commission_entries SET status = 'approved', approved_by = 'x' "
                                f"WHERE id = {e.id}"))     # status moves
        db_session.rollback()


# ── Reversal ─────────────────────────────────────────────────────────────────────

class TestReversal:
    def test_dishonour_voids_the_receipt_and_reverses_commission(self, client, db_session, scene, stub_pdf):
        publish(db_session)
        pid = record_cheque_and_verify(client, scene["oid"], "65340.00", "000777")
        receipt = db_session.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id == pid)).scalar_one()
        assert receipt.voided_at is None
        dishonour(client, pid)
        db_session.expire_all()
        assert db_session.get(PaymentReceipt, receipt.id).voided_at is not None
        rows = entries(db_session, payment_id=pid)
        earns = [e for e in rows if e.kind.value == "earn"]
        counters = {e.reverses_entry_id: e for e in rows if e.kind.value == "reversal"}
        assert len(earns) == 4 and set(counters) == {e.id for e in earns}
        for e in earns:
            assert e.status.value == "reversed" and counters[e.id].amount == -e.amount
            assert counters[e.id].status.value == "reversed" and counters[e.id].reason == "Cheque returned unpaid"
        assert sum(e.amount for e in rows) == 0

    def test_one_transaction_a_failed_reversal_undoes_the_dishonour_and_the_void(
            self, client, db_session, scene, stub_pdf, monkeypatch):
        """The receipt void, the ledger reversal and the commission reversal commit or
        fail together. PostgreSQL only: under the SQLite test harness pysqlite commits
        a released SAVEPOINT that no explicit BEGIN preceded (the ledger's nested
        insert), so atomicity cannot be observed there."""
        from tests.conftest import TEST_DATABASE_URL
        if not TEST_DATABASE_URL:
            pytest.skip("transaction atomicity is observable on PostgreSQL only (TEST_DATABASE_URL)")
        publish(db_session)
        pid = record_cheque_and_verify(client, scene["oid"], "65340.00", "000778")
        receipt = db_session.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id == pid)).scalar_one()

        def boom(*args, **kwargs):
            raise RuntimeError("commission reversal failed")
        original = commissions.on_payment_reversed
        monkeypatch.setattr(commissions, "on_payment_reversed", boom)
        with pytest.raises(RuntimeError):
            client.post(f"{DASH}/payments/{pid}/dishonour", json={"reason": "Returned"}, headers=ACCOUNTS)
        db_session.expire_all()
        assert db_session.get(Payment, pid).status == PaymentStatus.verified          # nothing happened
        assert db_session.get(PaymentReceipt, receipt.id).voided_at is None
        assert entries(db_session, kind=CommissionEntryKind.reversal) == []
        monkeypatch.setattr(commissions, "on_payment_reversed", original)
        dishonour(client, pid)
        db_session.expire_all()
        assert db_session.get(PaymentReceipt, receipt.id).voided_at is not None
        assert len(entries(db_session, kind=CommissionEntryKind.reversal)) == 4

    def test_an_unrated_entry_is_reversed_and_both_take_the_amount_later(self, client, db_session, scene):
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        dishonour(client, pid, "Funds recalled")
        rows = entries(db_session, payment_id=pid)
        assert all(e.amount is None for e in rows) and len(rows) == 8
        real = publish(db_session, effective_from=periods.today_ist() + timedelta(days=15))
        commissions.re_rate(db_session, real.id, "emp-admin-1", "rates confirmed")
        db_session.commit()
        rows = entries(db_session, payment_id=pid)
        assert all(e.amount is not None for e in rows) and sum(e.amount for e in rows) == 0

    def test_admin_reverses_an_entry_for_a_breach(self, client, db_session, scene):
        publish(db_session)
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        e = by_role(entries(db_session, payment_id=pid))["personal_sale"]
        url = f"{DASH}/commissions/{e.id}/reverse"
        assert client.post(url, json={"reason": "False address"}, headers=ACCOUNTS).status_code == 403
        r = client.post(url, json={"reason": "False address"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["entry"]["kind"] == "reversal" and r.json()["entry"]["amount"] == "-1000.00"
        again = client.post(url, json={"reason": "again"}, headers=ADMIN)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "not_reversible"


# ── Approval ─────────────────────────────────────────────────────────────────────

class TestApproval:
    URL = f"{DASH}/commissions/approve"

    def test_only_rated_earned_entries_all_or_nothing(self, client, db_session, scene):
        pid = record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        ids = [e.id for e in entries(db_session, payment_id=pid)]
        r = client.post(self.URL, json={"entry_ids": ids}, headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "entries_not_approvable"
        assert {x["reason"] for x in r.json()["detail"]["entries"]} == {"not_rated"}
        real = publish(db_session, effective_from=periods.today_ist() + timedelta(days=15))
        commissions.re_rate(db_session, real.id, "emp-admin-1", "confirmed")
        db_session.commit()
        for who in (MGMT, SALES, OPS, ENG):
            assert client.post(self.URL, json={"entry_ids": ids}, headers=who).status_code == 403
        r = client.post(self.URL, json={"entry_ids": ids + [999999]}, headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["entries"] == [{"entry_id": 999999, "reason": "not_found"}]
        assert {e.status.value for e in entries(db_session)} == {"earned"}
        r = client.post(self.URL, json={"entry_ids": ids}, headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["approved"] == 4
        assert {(e.status.value, e.approved_by) for e in entries(db_session)} == {("approved", "emp-acc-1")}
        r = client.post(self.URL, json={"entry_ids": ids[:1]}, headers=ADMIN)
        assert r.json()["detail"]["entries"] == [{"entry_id": ids[0], "reason": "not_earned"}]


# ── Who sees what ────────────────────────────────────────────────────────────────

class TestVisibility:
    def test_staff_ledger(self, client, db_session, scene):
        publish(db_session)
        record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        for who in (ACCOUNTS, MGMT, ADMIN):
            body = client.get(f"{DASH}/commissions", headers=who).json()
            assert body["total"] == 4 and body["totals"]["earned"] == "1600.00"
        for who in (SALES, OPS, ENG):
            assert client.get(f"{DASH}/commissions", headers=who).status_code == 403
        body = client.get(f"{DASH}/commissions", params={"associate_id": scene["block"]}, headers=MGMT).json()
        assert [i["earning_role"] for i in body["items"]] == ["territory_block"]
        item = body["items"][0]
        assert item["amount"] == "300.00" and item["basis_amount"] == "20000.00" and item["rate_percent"] == "1.5000"
        assert item["customer_name"] and item["project_reference_id"].startswith("GNE-")

    def test_associates_see_only_their_own_entries(self, client, db_session, scene):
        publish(db_session)
        record_and_verify(client, scene["oid"], "21780.00", "UTR-1")
        stranger = make_associate(db_session)
        mine = client.get("/api/v1/associate/commissions", headers=headers(scene["owner"])).json()
        assert [i["earning_role"] for i in mine["items"]] == ["personal_sale"]
        assert mine["summary"]["earned"] == "1000.00" and mine["summary"]["pending_count"] == 0
        assert "created_by" not in mine["items"][0] and mine["items"][0]["query"]["open"] is False
        district = client.get("/api/v1/associate/commissions", headers=headers(scene["district"])).json()
        assert [(i["earning_role"], i["amount"]) for i in district["items"]] == [("territory_district", "200.00")]
        assert district["items"][0]["customer_name"] and district["items"][0]["payment_id"]
        assert client.get("/api/v1/associate/commissions", headers=headers(stranger)).json()["total"] == 0
        assert client.get("/api/v1/associate/commissions", headers=ADMIN).status_code == 401
