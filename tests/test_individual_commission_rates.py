"""BE-12 — individual commission rates set by an Admin, the one rate resolver, and the
first real schedule taking effect without notice.

Rates are TEST values (tests/commission_support.py): a Sales Associate's standard
personal-sale rate is 5 %; the scenario customer's 20 % milestone (21,780.00) has a
commission basis of 20,000.00, so 5 % = 1,000.00 and 7 % = 1,400.00."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.commissions import config as commission_config
from app.commissions import periods, rates
from app.memory.models import (
    Associate, AssociateCommissionRate, AuditLog, CommissionRole, CustomerOnboarding, ProjectStage,
)
from tests.commission_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, ADMIN_2, DASH, MGMT, OPS, SALES, TEST_RATES, accepted, entries, headers, kyc_ready,
    make_associate, onboarding_ready, pk, publish, record_and_verify, territories,
)
from tests.execution_support import advance_to, notifications  # noqa: F401 — fixture
from tests.payments_support import stub_pdf  # noqa: F401 — fixture
from tests.test_bootstrap_db import REPO_ROOT, _run_bootstrap, empty_database_url  # noqa: F401 — the fixture

D = Decimal
REASON = "Twelve installs this quarter, all on time"
SCHEDULES = f"{DASH}/commission-rate-schedules"
ONB = "/api/v1/associate/onboardings"
HOME = "/api/v1/associate/home"
MINE = "/api/v1/associate/commission-rate"


def url(gna: str) -> str:
    return f"{DASH}/associates/{gna}/commission-rate"


def today() -> date:
    return periods.today_ist()


def notice_days() -> int:
    return commission_config.commissions()["rate_change_notice_days"]


def set_rate(client, gna: str, rate, effective: date, reason: str = REASON, who=ADMIN):
    return client.post(url(gna), json={"rate_percent": rate, "effective_from": effective.isoformat(),
                                       "reason": reason}, headers=who)


def ok(r):
    assert r.status_code == 201, r.text
    return r.json()


def associate(db, gna: str) -> Associate:
    db.expire_all()
    return db.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one()


def resolved(db, gna: str, day: date):
    return rates.resolve(db, associate(db, gna), CommissionRole.personal_sale, day)


def _cfg(monkeypatch, **values):
    monkeypatch.setitem(commission_config.load(), "commissions", {**commission_config.commissions(), **values})


@pytest.fixture
def standard(db_session, onboarding_ready):
    return publish(db_session)            # in force since 2026-01-01, never placeholder


# ── The resolver ─────────────────────────────────────────────────────────────────

class TestResolution:
    def test_standard_only(self, db_session, standard):
        gna = make_associate(db_session)
        r = resolved(db_session, gna, today())
        assert (r.percent, r.source, r.schedule.id, r.individual) == (D("5.0000"), "standard", standard.id, None)

    def test_nothing_confirmed_is_none(self, db_session, onboarding_ready):
        gna = make_associate(db_session)
        assert resolved(db_session, gna, today()) is None
        publish(db_session, is_placeholder=True)
        assert resolved(db_session, gna, today()) is None

    def test_individual_over_standard_and_null_back_to_standard(self, client, db_session, standard):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "7", today()))
        r = resolved(db_session, gna, today())
        assert (r.percent, r.source, r.schedule) == (D("7.0000"), "individual", None)
        later = today() + timedelta(days=notice_days())
        ok(set_rate(client, gna, None, later))                         # back to 5 %: a reduction, with notice
        assert resolved(db_session, gna, later - timedelta(days=1)).percent == D("7.0000")
        back = resolved(db_session, gna, later)
        assert (back.percent, back.source, back.schedule.id) == (D("5.0000"), "standard", standard.id)

    def test_a_future_change_is_not_applied_before_its_date(self, client, db_session, standard):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "9", today() + timedelta(days=5)))   # an increase may be dated any day from today
        assert resolved(db_session, gna, today()).percent == D("5.0000")
        assert resolved(db_session, gna, today() + timedelta(days=4)).percent == D("5.0000")
        assert resolved(db_session, gna, today() + timedelta(days=5)).percent == D("9.0000")

    def test_the_later_row_wins_on_the_same_date(self, client, db_session, standard):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "8", today()))
        ok(set_rate(client, gna, "9", today()))   # a correction the same day: the later row wins
        assert resolved(db_session, gna, today()).percent == D("9.0000")
        # 8.5 is lower than the 9 % in force that day: a reduction, so not today.
        assert set_rate(client, gna, "8.5", today()).json()["detail"]["code"] == "notice_period"

    def test_only_the_associates_own_rate_and_never_territory_roles(self, client, db_session, standard):
        gna, other = make_associate(db_session), make_associate(db_session)
        ok(set_rate(client, gna, "7", today()))
        assert resolved(db_session, other, today()).percent == D("5.0000")
        territory = rates.resolve(db_session, associate(db_session, gna), CommissionRole.territory_block, today())
        assert (territory.percent, territory.source) == (D("1.5000"), "standard")


# ── The ledger: the agreement date decides; recorded entries never change ────────

class TestLedger:
    def test_the_agreement_date_decides(self, client, db_session, standard):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "7", today() + timedelta(days=1)))   # from tomorrow
        oid = accepted(client, db_session, gna, stage=ProjectStage.agreement_signed)   # signed today
        pid = record_and_verify(client, oid, "21780.00", "UTR-1")
        e = entries(db_session, payment_id=pid)[0]
        assert (e.rate_percent, e.amount, e.schedule_id, e.individual_rate_id) == \
            (D("5.0000"), D("1000.00"), standard.id, None)

    def test_rated_at_the_individual_rate_and_earlier_entries_unchanged(self, client, db_session, standard):
        gna = make_associate(db_session)
        oid = accepted(client, db_session, gna, stage=ProjectStage.agreement_signed)
        first = record_and_verify(client, oid, "21780.00", "UTR-1")
        change = ok(set_rate(client, gna, "7", today()))["change"]
        second = record_and_verify(client, oid, "21780.00", "UTR-2")
        a, b = entries(db_session, payment_id=first)[0], entries(db_session, payment_id=second)[0]
        assert (a.rate_percent, a.amount, a.schedule_id, a.individual_rate_id) == \
            (D("5.0000"), D("1000.00"), standard.id, None)
        assert (b.rate_percent, b.amount, b.schedule_id, b.individual_rate_id) == \
            (D("7.0000"), D("1400.00"), None, change["individual_rate_id"])
        listed = {i["entry_id"]: i for i in client.get(f"{DASH}/commissions", headers=ACCOUNTS).json()["items"]}
        assert (listed[a.id]["rate_source"], listed[b.id]["rate_source"]) == ("standard", "individual")
        assert listed[b.id]["individual_rate_id"] == change["individual_rate_id"]
        own = client.get("/api/v1/associate/commissions", headers=headers(gna)).json()["items"]
        assert {i["rate_source"] for i in own} == {"standard", "individual"} and "individual_rate_id" not in own[0]
        # The database refuses to move a recorded entry onto another rate.
        with pytest.raises((IntegrityError, DBAPIError)):
            db_session.execute(text(f"UPDATE commission_entries SET individual_rate_id = NULL WHERE id = {b.id}"))
        db_session.rollback()

    def test_re_rate_respects_an_individual_rate(self, client, db_session, onboarding_ready, monkeypatch):
        gna = make_associate(db_session)
        oid = accepted(client, db_session, gna, stage=ProjectStage.agreement_signed)
        pid = record_and_verify(client, oid, "21780.00", "UTR-1")
        assert entries(db_session, payment_id=pid)[0].pending_reason == "placeholder_rates"
        ok(set_rate(client, gna, "7", today()))   # no confirmed rate yet: nothing to give notice of
        real = publish(db_session, effective_from=today() + timedelta(days=30))
        r = client.post(f"{SCHEDULES}/{real.id}/re-rate", json={"reason": "Rates confirmed"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        e = entries(db_session, payment_id=pid)[0]
        assert (e.rate_percent, e.amount, e.schedule_id) == (D("7.0000"), D("1400.00"), None)


# ── Setting a rate: notice, validation, who ──────────────────────────────────────

class TestSetting:
    def test_an_increase_may_take_effect_today(self, client, db_session, standard):
        gna = make_associate(db_session)
        body = ok(set_rate(client, gna, "7.25", today()))
        assert body["change"]["rate_percent"] == "7.2500" and body["change"]["status"] == "in_force"
        assert body["rate"]["effective"] == {"rate_percent": "7.2500", "source": "individual",
                                             "effective_from": today().isoformat(), "pending_reason": None}

    def test_a_reduction_needs_notice(self, client, db_session, standard):
        gna = make_associate(db_session)
        earliest = today() + timedelta(days=notice_days())
        for rate, day in (("4", today()), ("4", earliest - timedelta(days=1))):
            r = set_rate(client, gna, rate, day)
            assert r.status_code == 422 and r.json()["detail"] == {
                "code": "notice_period", "message": "A lower rate takes effect only after the notice period.",
                "field": "effective_from", "earliest_effective_from": earliest.isoformat()}
        assert ok(set_rate(client, gna, "4", earliest))["change"]["status"] == "scheduled"
        # Below the individual rate, too: 7 today, then 6 is a reduction.
        other = make_associate(db_session)
        ok(set_rate(client, other, "7", today()))
        assert set_rate(client, other, "6", today()).json()["detail"]["code"] == "notice_period"
        # Back to the standard 5 % from an individual 7 % is a reduction as well.
        assert set_rate(client, other, None, today()).json()["detail"]["code"] == "notice_period"
        # The same rate again is no change: allowed today.
        ok(set_rate(client, other, "7", today()))

    def test_the_increase_switch(self, client, db_session, standard, monkeypatch):
        _cfg(monkeypatch, individual_rate_increase_needs_notice=True)
        gna = make_associate(db_session)
        r = set_rate(client, gna, "7", today())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "notice_period"
        assert r.json()["detail"]["message"] == "A rate change takes effect only after the notice period."
        ok(set_rate(client, gna, "7", today() + timedelta(days=notice_days())))
        view = client.get(url(gna), headers=ADMIN).json()["notice"]
        assert view["increase_needs_notice"] and view["increase_earliest_effective_from"] == \
            (today() + timedelta(days=notice_days())).isoformat()

    def test_with_no_confirmed_rate_there_is_nothing_to_give_notice_of(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "3", today()))

    def test_validation(self, client, db_session, standard, monkeypatch):
        gna = make_associate(db_session)
        for rate in ("101", "-1", "abc", "5.12345", "NaN"):
            r = set_rate(client, gna, rate, today())
            assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_rate", rate
            assert r.json()["detail"]["field"] == "rate_percent"
        for reason in ("", "   ", "too short", None):
            r = client.post(url(gna), json={"rate_percent": "7", "effective_from": today().isoformat(),
                                            "reason": reason}, headers=ADMIN)
            assert r.status_code == 422 and r.json()["detail"]["code"] == "reason_required", reason
        r = set_rate(client, gna, "7", today() - timedelta(days=1))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "effective_in_past"
        assert r.json()["detail"]["earliest_effective_from"] == today().isoformat()
        # rate_percent is required (null means "back to standard", so it cannot be left out).
        r = client.post(url(gna), json={"effective_from": today().isoformat(), "reason": REASON}, headers=ADMIN)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        # Percentages are decimal strings; a JSON number is FastAPI's standard 422.
        r = client.post(url(gna), json={"rate_percent": 7.5, "effective_from": today().isoformat(), "reason": REASON},
                        headers=ADMIN)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
        assert client.post(url("GNA-XX-00-0000"), json={"rate_percent": "7", "effective_from": today().isoformat(),
                                                         "reason": REASON}, headers=ADMIN).status_code == 404
        assert db_session.execute(select(AssociateCommissionRate)).first() is None

    def test_bounds(self, client, db_session, standard, monkeypatch):
        _cfg(monkeypatch, individual_rate_min_percent="4", individual_rate_max_percent="9.5")
        gna = make_associate(db_session)
        for rate in ("3.9999", "9.5001"):
            r = set_rate(client, gna, rate, today() + timedelta(days=notice_days()))
            assert r.status_code == 422 and r.json()["detail"] == {
                "code": "out_of_bounds", "message": "The rate is outside the range Grid Nova allows for individual rates.",
                "field": "rate_percent", "min_percent": "4.0000", "max_percent": "9.5000"}
        ok(set_rate(client, gna, "9.5", today()))
        assert client.get(url(gna), headers=MGMT).json()["bounds"] == {"min_percent": "4.0000", "max_percent": "9.5000"}

    def test_config_validation(self):
        from app.commissions.config import CommissionConfigError, _validate
        good = commission_config.load()
        for bad in ({"individual_rate_increase_needs_notice": "no"}, {"individual_rate_min_percent": "101"},
                    {"individual_rate_min_percent": "6", "individual_rate_max_percent": "5"}):
            with pytest.raises(CommissionConfigError):
                _validate({**good, "commissions": {**good["commissions"], **bad}})
        missing = {k: v for k, v in good["commissions"].items() if k != "individual_rate_max_percent"}
        with pytest.raises(CommissionConfigError):
            _validate({**good, "commissions": missing})

    def test_who(self, client, db_session, standard):
        gna = make_associate(db_session)
        for who in (MGMT, ACCOUNTS, SALES, OPS):
            assert set_rate(client, gna, "7", today(), who=who).status_code == 403
        assert client.get(url(gna), headers=MGMT).status_code == 200
        assert client.get(f"{DASH}/associates/{gna}/performance", headers=MGMT).status_code == 200
        for who in (ACCOUNTS, SALES, OPS):
            assert client.get(url(gna), headers=who).status_code == 403
            assert client.get(f"{DASH}/associates/{gna}/performance", headers=who).status_code == 403
        assert client.get(url(gna), headers=headers(gna)).status_code == 401
        assert client.get(url("GNA-XX-00-0000"), headers=ADMIN).status_code == 404

    def test_audited_and_append_only(self, client, db_session, standard):
        gna = make_associate(db_session)
        row_id = ok(set_rate(client, gna, "7", today()))["change"]["individual_rate_id"]
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "associate_commission_rate")
                                   ).scalar_one()
        assert (audit.action, audit.entity_id, audit.performed_by) == ("individual_rate_set", str(row_id), "emp-admin-1")
        assert audit.before_value == {"rate_percent": "5.0000", "rate_percent_day_before": "5.0000"}
        assert audit.after_value == {"associate_id": gna, "rate_key": "personal_sale.associate.sales",
                                     "rate_percent": "7.0000", "effective_rate_percent": "7.0000",
                                     "effective_from": today().isoformat(), "reduction": False,
                                     "notice_required": False, "reason": REASON}
        later = today() + timedelta(days=notice_days())
        ok(set_rate(client, gna, None, later))
        cleared = db_session.execute(select(AuditLog).where(AuditLog.action == "individual_rate_cleared")).scalar_one()
        assert cleared.after_value["rate_percent"] is None and cleared.after_value["effective_rate_percent"] == "5.0000"
        assert cleared.after_value["reduction"] and cleared.after_value["notice_required"]
        for statement in (f"UPDATE associate_commission_rates SET rate_percent = 9 WHERE id = {row_id}",
                          f"DELETE FROM associate_commission_rates WHERE id = {row_id}"):
            with pytest.raises((IntegrityError, DBAPIError)):
                db_session.execute(text(statement))
            db_session.rollback()
        # The database itself refuses a short reason, however the row arrives.
        with pytest.raises((IntegrityError, DBAPIError)):
            db_session.add(AssociateCommissionRate(associate_id=pk(db_session, gna), rate_key="x", rate_percent=1,
                                                   effective_from=today(), reason=" short ", set_by="x"))
            db_session.flush()
        db_session.rollback()


# ── What an Admin and the Associate see ──────────────────────────────────────────

class TestViews:
    def test_admin_view(self, client, db_session, standard):
        gna = make_associate(db_session)
        view = client.get(url(gna), headers=ADMIN).json()
        assert view["standard"] == {"rate_percent": "5.0000", "schedule_id": standard.id,
                                    "schedule_version": standard.version, "effective_from": "2026-01-01"}
        assert view["individual"] is None and view["history"] == [] and view["scheduled_changes"] == []
        assert view["effective"] == {"rate_percent": "5.0000", "source": "standard", "effective_from": "2026-01-01",
                                     "pending_reason": None}
        assert view["rate_key"] == "personal_sale.associate.sales" and view["notice"]["notice_days"] == notice_days()
        first = ok(set_rate(client, gna, "7", today()))["change"]["individual_rate_id"]
        later = today() + timedelta(days=notice_days())
        second = ok(set_rate(client, gna, "6", later, reason="Two complaints about site conduct"))
        view = second["rate"]
        assert view["individual"] == {"individual_rate_id": first, "rate_percent": "7.0000",
                                      "effective_from": today().isoformat()}
        assert view["effective"]["rate_percent"] == "7.0000"
        assert view["scheduled_changes"] == [{"effective_from": later.isoformat(), "rate_percent": "6.0000",
                                              "source": "individual"}]
        assert [(h["individual_rate_id"], h["status"], h["reason"]) for h in view["history"]] == \
            [(second["change"]["individual_rate_id"], "scheduled", "Two complaints about site conduct"),
             (first, "in_force", REASON)]
        assert client.get(url(gna), headers=MGMT).json() == view

    def test_a_scheduled_standard_change_shows_while_standard_applies(self, client, db_session, standard):
        gna = make_associate(db_session)
        later = today() + timedelta(days=20)
        publish(db_session, effective_from=later, overrides={"personal_sale.associate.sales": "5.5"})
        assert client.get(url(gna), headers=ADMIN).json()["scheduled_changes"] == [
            {"effective_from": later.isoformat(), "rate_percent": "5.5000", "source": "standard"}]
        ok(set_rate(client, gna, "8", today()))   # an individual rate hides the standard change
        assert client.get(url(gna), headers=ADMIN).json()["scheduled_changes"] == []

    def test_the_associate_sees_only_their_own_rate_and_never_the_reason(self, client, db_session, standard):
        gna, other = make_associate(db_session), make_associate(db_session)
        ok(set_rate(client, gna, "7", today(), reason="Confidential: top seller in the district"))
        later = today() + timedelta(days=notice_days())
        ok(set_rate(client, gna, "6.5", later))
        r = client.get(MINE, headers=headers(gna))
        assert r.status_code == 200, r.text
        assert r.json() == {"rate_key": "personal_sale.associate.sales", "today": today().isoformat(),
                            "effective": {"rate_percent": "7.0000", "source": "individual",
                                          "effective_from": today().isoformat(), "pending_reason": None},
                            "scheduled_changes": [{"effective_from": later.isoformat(), "rate_percent": "6.5000",
                                                   "source": "individual"}]}
        assert "Confidential" not in r.text and "set_by" not in r.text and '"reason"' not in r.text
        theirs = client.get(MINE, headers=headers(other)).json()
        assert theirs["effective"]["rate_percent"] == "5.0000" and theirs["scheduled_changes"] == []
        assert client.get(MINE, headers=ADMIN).status_code == 401

    def test_before_any_confirmed_rate(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        assert client.get(MINE, headers=headers(gna)).json()["effective"] == {
            "rate_percent": None, "source": None, "effective_from": None, "pending_reason": "placeholder_rates"}
        assert client.get(url(gna), headers=ADMIN).json()["standard"] is None

    def test_the_estimate_uses_the_individual_rate(self, client, db_session, standard):
        gna = make_associate(db_session)
        oid = accepted(client, db_session, gna)                         # not signed: today's rate
        ok(set_rate(client, gna, "7", today()))
        got = client.get(f"{ONB}/{oid}", headers=headers(gna)).json()["estimated_commission"]
        assert (got["amount"], got["rate"], got["rate_source"]) == ("7000.00", "7.0000", "individual")
        listed = next(i for i in client.get(ONB, headers=headers(gna)).json()["items"] if i["onboarding_id"] == oid)
        assert listed["estimated_commission"] == got


# ── Telling the Associate ────────────────────────────────────────────────────────

class TestNotification:
    def test_home_feed_item_and_email_without_the_reason(self, client, db_session, standard, notifications,
                                                         monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", True)
        gna = make_associate(db_session)
        later = today() + timedelta(days=notice_days())
        ok(set_rate(client, gna, "7", today(), reason="Internal: strong conversion rate"))
        ok(set_rate(client, gna, "6", later))
        mail = [p for p in notifications.of("associate_email") if p["event_type"] == "associate_commission_rate_changed"]
        assert len(mail) == 2
        when = f"{later.day} {later.strftime('%B %Y')}"
        assert mail[1]["lines"][0] == f"Your commission rate changes from 7% to 6% from {when}."
        assert mail[0]["lines"][0].startswith("Your commission rate changes from 5% to 7% from ")
        assert all("Internal" not in json.dumps(m) and REASON not in json.dumps(m) for m in mail)
        assert mail[0]["link_url"].endswith("/associates/portal/earnings")
        items = [i for i in client.get(HOME, headers=headers(gna)).json()["items"]
                 if i["kind"] == "commission_rate_changed"]
        assert [i["params"] for i in items] == [
            {"rate_percent_from": "5.0000", "rate_percent_to": "7.0000", "effective_from": today().isoformat(),
             "scheduled": False},
            {"rate_percent_from": "7.0000", "rate_percent_to": "6.0000", "effective_from": later.isoformat(),
             "scheduled": True}]
        assert items[0]["message_key"] == "todo.commission_rate_changed" and items[0]["priority"] == 3
        other = make_associate(db_session)
        assert not [i for i in client.get(HOME, headers=headers(other)).json()["items"]
                    if i["kind"] == "commission_rate_changed"]

    def test_a_same_day_correction_replaces_the_item(self, client, db_session, standard):
        gna = make_associate(db_session)
        ok(set_rate(client, gna, "9", today()))
        ok(set_rate(client, gna, "9.5", today()))
        items = [i["params"] for i in client.get(HOME, headers=headers(gna)).json()["items"]
                 if i["kind"] == "commission_rate_changed"]
        assert items == [{"rate_percent_from": "5.0000", "rate_percent_to": "9.5000",
                          "effective_from": today().isoformat(), "scheduled": False}]

    def test_no_email_when_notifications_are_off(self, client, db_session, standard, notifications, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", False)
        ok(set_rate(client, make_associate(db_session), "7", today()))
        assert not [p for p in notifications.of("associate_email")
                    if p["event_type"] == "associate_commission_rate_changed"]


# ── The first real schedule ──────────────────────────────────────────────────────

class TestFirstRealSchedule:
    def test_first_real_schedule_takes_effect_today_the_second_needs_notice(self, client, db_session,
                                                                          onboarding_ready):
        listing = client.get(SCHEDULES, headers=ADMIN).json()
        assert listing["next_real_schedule_is_first"] and listing["earliest_effective_from"] == today().isoformat()
        r = client.post(SCHEDULES, json={"effective_from": today().isoformat(), "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 201, r.text
        assert r.json()["in_effect"] and not r.json()["is_placeholder"]
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "published")).scalar_one()
        assert audit.after_value["first_real_schedule"] is True
        r = client.post(SCHEDULES, json={"effective_from": today().isoformat(), "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "notice_period"
        assert r.json()["detail"]["earliest_effective_from"] == (today() + timedelta(days=notice_days())).isoformat()
        listing = client.get(SCHEDULES, headers=ADMIN).json()
        assert not listing["next_real_schedule_is_first"]

    def test_a_placeholder_does_not_count_and_the_past_is_still_refused(self, client, db_session, onboarding_ready):
        publish(db_session, is_placeholder=True)
        r = client.post(SCHEDULES, json={"effective_from": (today() - timedelta(days=1)).isoformat(),
                                         "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "effective_in_past"
        r = client.post(SCHEDULES, json={"effective_from": today().isoformat(), "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 201, r.text

    def test_a_future_dated_real_schedule_still_counts_as_published(self, client, db_session, onboarding_ready):
        publish(db_session, effective_from=today() + timedelta(days=30))     # real, not yet in force
        r = client.post(SCHEDULES, json={"effective_from": today().isoformat(), "rates": TEST_RATES}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "notice_period"


# ── Performance ──────────────────────────────────────────────────────────────────

class TestPerformance:
    def test_summary(self, client, db_session, standard):
        gna = make_associate(db_session)
        oid = accepted(client, db_session, gna, stage=ProjectStage.agreement_signed)
        record_and_verify(client, oid, "21780.00", "UTR-1")
        onboarding = db_session.get(CustomerOnboarding, oid)
        onboarding.verified_at = datetime.now(timezone.utc)       # the call itself is tested elsewhere
        db_session.commit()
        advance_to(db_session, oid, ProjectStage.commissioned)
        r = client.get(f"{DASH}/associates/{gna}/performance", headers=ADMIN)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["period"] == {"date_from": today().replace(day=1).isoformat(), "date_to": today().isoformat()}
        assert body["customers"] == {"onboarded": 1, "verified": 1, "commissioned": 1}
        assert body["verified_payments"] == {"credits": "21780.00", "reversals": "0.00", "net": "21780.00"}
        assert body["commission"] == {"earned": "1000.00", "earned_personal_sale": "1000.00", "reversed": "0.00",
                                      "pending_count": 0}
        other = client.get(f"{DASH}/associates/{make_associate(db_session)}/performance", headers=MGMT).json()
        assert other["customers"]["onboarded"] == 0 and other["commission"]["earned"] == "0.00"
        long_ago = client.get(f"{DASH}/associates/{gna}/performance",
                              params={"date_from": "2025-01-01", "date_to": "2025-01-31"}, headers=ADMIN).json()
        assert long_ago["customers"]["onboarded"] == 0 and long_ago["verified_payments"]["credits"] == "0.00"
        bad = client.get(f"{DASH}/associates/{gna}/performance",
                         params={"date_from": "2026-02-01", "date_to": "2026-01-01"}, headers=ADMIN)
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_period"


# ── Migration 0054 up and down ───────────────────────────────────────────────────

def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": database_url, "ENVIRONMENT": "development"}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True)


def _triggers(engine) -> set[str]:
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            return set(conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).scalars())
        return set(conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars())


def test_migration_0054_up_and_down(empty_database_url):
    assert _run_bootstrap(empty_database_url).returncode == 0
    engine = create_engine(empty_database_url)
    try:
        ledger_checks = {c["name"] for c in inspect(engine).get_check_constraints("commission_entries")}
        down = _alembic(empty_database_url, "downgrade", "0053_attribution_until_completed")
        assert down.returncode == 0, down.stderr
        engine.dispose()
        insp = inspect(engine)
        assert "associate_commission_rates" not in insp.get_table_names()
        assert "individual_rate_id" not in {c["name"] for c in insp.get_columns("commission_entries")}
        # The SQLite rebuild kept what commission_entries had: checks, the partial index, the guard.
        assert {c["name"] for c in insp.get_check_constraints("commission_entries")} == ledger_checks
        assert "uq_commission_entries_earn_once" in {i["name"] for i in insp.get_indexes("commission_entries")}
        guards = {"commission_entries_guard"} | ({"commission_entries_no_delete"}
                                                 if engine.dialect.name == "sqlite" else set())
        assert guards <= _triggers(engine)
        assert not any(t.startswith("associate_commission_rates") for t in _triggers(engine))
        up = _alembic(empty_database_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        engine.dispose()
        insp = inspect(engine)
        assert "individual_rate_id" in {c["name"] for c in insp.get_columns("commission_entries")}
        assert {"associate_commission_rates_append_only"} <= _triggers(engine) if engine.dialect.name == "postgresql" \
            else {"associate_commission_rates_no_update", "associate_commission_rates_no_delete"} <= _triggers(engine)
    finally:
        engine.dispose()
