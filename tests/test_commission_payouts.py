"""BE-4 Phase 2 — TDS, monthly payout batches and the Accounts exports (policy 4.5–4.7).

Time is controlled by freezing the commission clock (app.commissions.service._now, which
stamps every entry) and the business 'today' (app.commissions.periods.today_ist), so a
test can earn in one month and pay out the next, and cross 31 March → 1 April IST."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.commissions import payouts, periods
from app.commissions import service as commissions
from app.memory.models import (
    Associate, AuditLog, CommissionEntry, CommissionEntryKind, PayoutBatch, PayoutBatchStatus, PayoutLine, ProjectStage,
)
from tests.commission_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ACCOUNTS_2, ADMIN, ADMIN_2, DASH, MGMT, OPS, SALES, accepted, approve_all, by_role, dishonour,
    entries, headers, kyc_ready, make_associate, network, onboarding_ready, pk, publish, record_and_verify,
    record_cheque_and_verify, territories,
)
from tests.payments_support import stub_pdf  # noqa: F401 — fixture

D = Decimal
BATCHES = f"{DASH}/payout-batches"
UTC = timezone.utc


def at(monkeypatch, instant: datetime, today: date | None = None) -> None:
    """Entries written from now on are stamped `instant`; 'today' (IST) is `today`."""
    monkeypatch.setattr(commissions, "_now", lambda: instant)
    if today is not None:
        monkeypatch.setattr(periods, "today_ist", lambda: today)


def batch(client, period: str, who=ACCOUNTS):
    return client.post(BATCHES, json={"period": period}, headers=who)


def approve(client, batch_id: int, who=ACCOUNTS_2, **body):
    return client.post(f"{BATCHES}/{batch_id}/approve", json=body, headers=who)


def pay(client, batch_id: int, refs: dict, who=ACCOUNTS):
    return client.post(f"{BATCHES}/{batch_id}/mark-paid",
                       json={"lines": [{"associate_id": k, "bank_reference": v} for k, v in refs.items()]},
                       headers=who)


def line_of(body: dict, gna: str) -> dict:
    return next(ln for ln in body["lines"] if ln["associate"]["associate_id"] == gna)


# ── TDS: one function, the draft rule ────────────────────────────────────────────

class TestTds:
    CFG = {"rate_percent": "2", "fy_threshold_inr": "20000", "no_pan_rate_percent": "20", "rounding": "0.01"}

    def due(self, before, tds_before, now, pan=True, **cfg):
        return payouts.tds_due(fy_taxable_before=D(before), fy_tds_before=D(tds_before), taxable_now=D(now),
                               pan_on_file=pan, cfg={**self.CFG, **cfg})

    def test_nothing_up_to_the_threshold(self):
        assert self.due("0", "0", "15000.00") == (D("0.00"), D("2"))
        assert self.due("15000", "0", "5000.00")[0] == D("0.00")          # exactly 20,000: not crossed

    def test_crossing_deducts_on_the_whole_year_to_date(self):
        assert self.due("15000", "0", "10000.00")[0] == D("500.00")       # 2 % of 25,000
        assert self.due("25000", "500", "5000.00")[0] == D("100.00")      # 2 % of 30,000 − 500 already

    def test_without_a_pan_the_higher_rate(self):
        assert self.due("0", "0", "25000.00", pan=False) == (D("5000.00"), D("20"))

    def test_never_more_than_the_payout_and_rounding(self):
        assert self.due("0", "0", "0.00")[0] == D("0.00")
        assert self.due("20000", "0", "1.30", rounding="1")[0] == D("1.30")       # capped at the payout
        assert self.due("20000", "0", "100.65", rounding="1")[0] == D("100.65")   # due 402 ≥ payout
        assert self.due("19000", "0", "1100.65", rounding="1")[0] == D("402.00")  # 2 % of 20,100.65 → ₹402

    def test_financial_year_boundary_is_ist_midnight(self):
        last = datetime(2027, 3, 31, 18, 29, 59, tzinfo=UTC)   # 23:59:59 IST, 31 March
        first = datetime(2027, 3, 31, 18, 30, 0, tzinfo=UTC)   # 00:00:00 IST, 1 April
        assert periods.financial_year(periods.ist_date(last)) == "2026-27"
        assert periods.financial_year(periods.ist_date(first)) == "2027-28"
        assert periods.period_end_utc("2027-03") == first
        assert periods.fy_of_period("2027-03") == "2026-27" and periods.fy_of_period("2027-04") == "2027-28"


# ── Batches ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def owner_oid(client, db_session, onboarding_ready):
    owner = make_associate(db_session)
    return owner, accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)


class TestFinancialYear:
    def test_31_march_and_1_april_fall_in_different_years(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        publish(db_session, overrides={"personal_sale.associate.sales": "50"})
        at(monkeypatch, datetime(2027, 3, 31, 18, 29, 59, tzinfo=UTC), date(2027, 4, 5))
        record_and_verify(client, oid, "65340.00", "UTR-MAR")            # 50 % of 60,000 = 30,000
        at(monkeypatch, datetime(2027, 3, 31, 18, 30, 0, tzinfo=UTC), date(2027, 4, 5))
        record_and_verify(client, oid, "21780.00", "UTR-APR")            # 50 % of 20,000 = 10,000
        approve_all(client, db_session)
        march = batch(client, "2027-03")
        assert march.status_code == 201, march.text
        body = march.json()
        assert body["financial_year"] == "2026-27"
        ln = line_of(body, owner)
        assert (ln["gross"], ln["taxable"], ln["tds"], ln["net"]) == ("30000.00", "30000.00", "600.00", "29400.00")
        assert approve(client, body["batch_id"]).status_code == 200
        assert pay(client, body["batch_id"], {owner: "NEFT-1"}).status_code == 200
        at(monkeypatch, datetime(2027, 4, 2, tzinfo=UTC), date(2027, 5, 5))
        april = batch(client, "2027-04").json()
        assert april["financial_year"] == "2027-28"
        ln = line_of(april, owner)
        assert (ln["gross"], ln["tds"], ln["fy_taxable_before"]) == ("10000.00", "0.00", "0.00")   # threshold resets


class TestWorkedExample1:
    """A 20/60/20 project (1,08,900.00 final; basic 1,00,000.00); the 60 % cheque is
    dishonoured after its commission was paid out."""

    def test_recovery_from_the_next_payouts_and_carry_forward(self, client, db_session, onboarding_ready,
                                                               monkeypatch, stub_pdf):
        net = network(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, 6, 0, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-20A")
        p60 = record_cheque_and_verify(client, oid, "65340.00", "100060")
        record_and_verify(client, oid, "21780.00", "UTR-20B")
        approve_all(client, db_session)
        november = batch(client, "2026-11").json()
        assert line_of(november, owner)["gross"] == "5000.00"              # 1,000 + 3,000 + 1,000
        assert line_of(november, net["block"])["gross"] == "1500.00"       # 300 + 900 + 300
        assert approve(client, november["batch_id"]).status_code == 200
        paid = pay(client, november["batch_id"], {owner: "NEFT-OWN", net["state"]: "NEFT-S", net["district"]: "NEFT-D",
                                                   net["block"]: "NEFT-B"})
        assert paid.status_code == 200, paid.text
        assert {e.status.value for e in entries(db_session)} == {"paid"}

        at(monkeypatch, datetime(2026, 12, 3, 6, 0, tzinfo=UTC), date(2027, 1, 5))
        dishonour(client, p60, "Cheque returned: insufficient funds")
        recoveries = [e for e in entries(db_session) if e.kind.value == "recovery"]
        assert sorted(e.amount for e in recoveries) == [D("-3000.00"), D("-900.00"), D("-600.00"), D("-300.00")]
        assert {e.status.value for e in recoveries} == {"approved"}
        assert {e.status.value for e in entries(db_session, payment_id=p60, kind=CommissionEntryKind.earn)} \
            == {"paid"}                                                     # what was paid stays paid
        december = batch(client, "2026-12").json()
        ln = line_of(december, owner)
        assert (ln["gross"], ln["recoveries"], ln["taxable"], ln["net"], ln["carry_forward"]) == \
            ("0.00", "3000.00", "0.00", "0.00", "3000.00")
        assert approve(client, december["batch_id"]).status_code == 200
        assert pay(client, december["batch_id"], {}).status_code == 200    # nothing to transfer

        oid2 = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        at(monkeypatch, datetime(2027, 1, 12, 6, 0, tzinfo=UTC), date(2027, 2, 5))
        record_and_verify(client, oid2, "21780.00", "UTR-NEW")             # owner earns 1,000 more
        approve_all(client, db_session)
        january = batch(client, "2027-01").json()
        ln = line_of(january, owner)
        assert (ln["gross"], ln["carried_in"], ln["taxable"], ln["net"], ln["carry_forward"]) == \
            ("1000.00", "3000.00", "0.00", "0.00", "2000.00")
        mine = client.get("/api/v1/associate/commissions", headers=headers(owner)).json()["summary"]
        # Paid, net of the recovery paid in December; January is still a draft.
        assert mine["paid"] == "2000.00" and mine["to_recover"] == "3000.00"

    def test_a_reversal_cancels_the_draft_holding_it(self, client, db_session, owner_oid, monkeypatch, stub_pdf):
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, 6, 0, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-20A")
        p60 = record_cheque_and_verify(client, oid, "65340.00", "100061")
        approve_all(client, db_session)
        draft = batch(client, "2026-11").json()
        dishonour(client, p60)
        db_session.expire_all()
        cancelled = db_session.get(PayoutBatch, draft["batch_id"])
        assert cancelled.status == PayoutBatchStatus.cancelled and "reversed" in cancelled.cancel_reason
        rows = entries(db_session, payment_id=p60)
        assert {e.kind.value for e in rows} == {"earn", "reversal"} and {e.status.value for e in rows} == {"reversed"}
        again = batch(client, "2026-11").json()
        assert line_of(again, owner)["gross"] == "1000.00"                  # only the 20 % payment


class TestBatchRules:
    def test_no_money_under_placeholder_rates(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        r = batch(client, "2026-11")
        assert r.status_code == 409 and r.json()["detail"] == {
            "code": "placeholder_rates", "pending_entries": 1,
            "message": "Commission rates are not confirmed yet: entries of this month have no amount."}
        real = publish(db_session, effective_from=date(2026, 12, 20))
        assert client.post(f"{DASH}/commission-rate-schedules/{real.id}/re-rate", json={"reason": "confirmed"},
                           headers=ADMIN).json()["rated"] == 1
        approve_all(client, db_session)
        assert batch(client, "2026-11").status_code == 201

    def test_order_and_uniqueness(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 11, 20))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        approve_all(client, db_session)
        assert batch(client, "2026-11").json()["detail"]["code"] == "period_not_ended"
        assert batch(client, "2026-13").json()["detail"]["code"] == "invalid_period"
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2027, 1, 5))
        assert batch(client, "2026-10").json()["detail"]["code"] == "no_approved_entries"
        first = batch(client, "2026-11").json()
        assert batch(client, "2026-12").json()["detail"] == {
            "code": "draft_batch_exists", "batch_id": first["batch_id"],
            "message": "Another payout batch is still a draft: approve or cancel it first."}
        assert approve(client, first["batch_id"]).status_code == 200
        assert batch(client, "2026-11").json()["detail"]["code"] == "batch_exists"
        assert batch(client, "2026-10").json()["detail"]["code"] == "period_out_of_order"
        for who in (MGMT, SALES, OPS):
            assert batch(client, "2026-12", who=who).status_code == 403
        db_session.add(PayoutBatch(period="2026-11", financial_year="2026-27", status=PayoutBatchStatus.draft,
                                   prepared_by="x"))
        with pytest.raises(IntegrityError):
            db_session.flush()                  # the database refuses a second live batch for a month
        db_session.rollback()

    def test_segregation_of_duties_and_paying(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        approve_all(client, db_session)
        b = batch(client, "2026-11", who=ADMIN).json()
        url = f"{BATCHES}/{b['batch_id']}"
        assert pay(client, b["batch_id"], {owner: "X"}).json()["detail"]["code"] == "invalid_transition"
        r = approve(client, b["batch_id"], who=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "reason_required"
        r = approve(client, b["batch_id"], who=ADMIN, reason="Only one Accounts person this week")
        assert r.status_code == 200 and r.json()["approval_override_reason"].startswith("Only one")
        assert db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payout_batch",
                                                         AuditLog.action == "segregation_override")).scalar_one()
        assert client.post(f"{url}/cancel", json={"reason": "x"}, headers=ACCOUNTS).json()["detail"]["code"] \
            == "invalid_transition"
        r = pay(client, b["batch_id"], {})
        assert r.status_code == 422 and r.json()["detail"]["associate_ids"] == [owner]
        r = pay(client, b["batch_id"], {owner: "UTR-PAY-1", "GNA-XX-00-00000": "Y"})
        assert r.json()["detail"]["code"] == "unknown_associate"
        r = pay(client, b["batch_id"], {owner: "UTR-PAY-1"})
        assert r.status_code == 200 and r.json()["status"] == "paid"
        assert r.json()["lines"][0]["bank_reference"] == "UTR-PAY-1"
        assert {e.status.value for e in entries(db_session)} == {"paid"}
        assert all(e.paid_at is not None for e in entries(db_session))

    def test_accounts_cannot_approve_their_own_batch(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        approve_all(client, db_session)
        b = batch(client, "2026-11").json()
        r = approve(client, b["batch_id"], who=ACCOUNTS, reason="I insist")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_approval"
        r = client.post(f"{BATCHES}/{b['batch_id']}/cancel", json={"reason": "Wrong month"}, headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert {e.batch_id for e in entries(db_session)} == {None}
        assert batch(client, "2026-11").status_code == 201                  # prepared again

    def test_associate_payouts_show_approved_and_paid_only(self, client, db_session, owner_oid, monkeypatch):
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        approve_all(client, db_session)
        b = batch(client, "2026-11").json()
        mine = lambda: client.get("/api/v1/associate/payouts", headers=headers(owner)).json()["items"]  # noqa: E731
        assert mine() == []
        approve(client, b["batch_id"])
        pay(client, b["batch_id"], {owner: "UTR-P"})
        (item,) = mine()
        assert (item["period"], item["status"], item["net"], item["bank_reference"]) == \
            ("2026-11", "paid", "1000.00", "UTR-P")
        assert item["bank_account_last4"] and "account_holder_name" not in item


# ── Exports ──────────────────────────────────────────────────────────────────────

class TestExports:
    def _paid_batch(self, client, db_session, owner_oid, monkeypatch) -> tuple[str, dict]:
        owner, oid = owner_oid
        publish(db_session)
        at(monkeypatch, datetime(2026, 11, 10, tzinfo=UTC), date(2026, 12, 5))
        record_and_verify(client, oid, "21780.00", "UTR-1")
        approve_all(client, db_session)
        return owner, batch(client, "2026-11").json()

    def test_bank_file_accounts_only_approved_only_audited(self, client, db_session, owner_oid, monkeypatch):
        from app.security.encryption import decrypt
        owner, b = self._paid_batch(client, db_session, owner_oid, monkeypatch)
        url = f"{DASH}/exports/payout-bank-{b['batch_id']}.csv"
        for who in (ADMIN, MGMT, SALES):
            assert client.get(url, headers=who).status_code == 403
        r = client.get(url, headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "batch_not_approved"
        approve(client, b["batch_id"])
        r = client.get(url, headers=ACCOUNTS)
        assert r.status_code == 200 and r.text.startswith("﻿associate_id,account_holder_name,account_number")
        a = db_session.execute(select(Associate).where(Associate.associate_id == owner)).scalar_one()
        app_row = payouts.application_of(db_session, a)
        account = decrypt(app_row.bank_account_encrypted)
        assert account in r.text and "1000.00" in r.text
        # Nowhere else: the batch JSON carries the last four digits only.
        assert account not in client.get(f"{BATCHES}/{b['batch_id']}", headers=ACCOUNTS).text
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "pii_decrypted")).scalars().all()
        assert [(x.after_value["field"], x.after_value["purpose"], x.performed_by) for x in audit] == \
            [("bank_account", "payout_bank_export", "emp-acc-1")]
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "bank_file_exported")).scalar_one() \
            .after_value["rows"] == 1

    def test_tds_and_commission_exports(self, client, db_session, owner_oid, monkeypatch):
        owner, b = self._paid_batch(client, db_session, owner_oid, monkeypatch)
        approve(client, b["batch_id"])
        r = client.get(f"{DASH}/exports/tds.csv", params={"financial_year": "2026-27"}, headers=ACCOUNTS)
        assert r.status_code == 200 and r.text.startswith("﻿financial_year,period")
        a = db_session.execute(select(Associate).where(Associate.associate_id == owner)).scalar_one()
        from app.security.encryption import decrypt
        pan = decrypt(payouts.application_of(db_session, a).pan_encrypted)
        assert pan in r.text
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "pii_decrypted")).scalar_one() \
            .after_value == {"field": "pan", "purpose": "tds_export"}
        bad = client.get(f"{DASH}/exports/tds.csv", params={"financial_year": "2026-28"}, headers=ACCOUNTS)
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_financial_year"
        assert client.get(f"{DASH}/exports/tds.csv", headers=MGMT).status_code == 403

        e = entries(db_session)[0]
        reverse = client.post(f"{DASH}/commissions/{e.id}/reverse", json={"reason": "=HYPERLINK(\"x\")"},
                              headers=ADMIN)
        assert reverse.status_code == 200
        csv_text = client.get(f"{DASH}/exports/commissions.csv", headers=ACCOUNTS).text
        assert csv_text.startswith("﻿entry_id,associate_id")
        assert "'=HYPERLINK" in csv_text and "-1000.00" in csv_text      # text guarded, numbers left as numbers
        assert "IST" in csv_text
