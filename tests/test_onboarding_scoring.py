"""Phase 3A Step 2 — lead scoring v2 and overdue escalation."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.notifications.notifier as notifier_module
from app.memory.models import CustomerOnboarding, Lead, LeadScore, ScoreBand
from app.notifications.notifier import LogNotifier
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, ALIPURDUAR_I, BASE, DASH, SALES, headers, kyc_ready, make_associate, onboarding_body,
    onboarding_ready, territories, upload, upload_required_documents,
)


class Capture(LogNotifier):
    def __init__(self):
        self.events = []

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))


@pytest.fixture
def captured(monkeypatch):
    c = Capture()
    monkeypatch.setattr(notifier_module, "notifier", c)
    return c


def _create(client, gna, **kw):
    r = client.post(BASE, json=onboarding_body(**kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()


def _factors(body) -> dict:
    return {f["factor"]: f for f in body["score"]["factors"]}


class TestScore:
    def test_exact_breakdown_of_a_fresh_customer(self, client, db_session, onboarding_ready):
        body = _create(client, make_associate(db_session))
        f = _factors(body)
        # 3 kW asked, 5 kW sanctioned, 400 kWh/month: 3 × 120 = 360 ≤ 400 × 1.5 → no capacity warning.
        assert {k: v["points"] for k, v in f.items()} == {
            "verification_call": 0, "documents_complete": 0, "subsidy_eligibility": 20, "system_fit": 15,
            "customer_timeline": 7, "site_readiness": 0, "associate_track_record": 5,
        }
        assert body["score"]["score"] == 47 and body["score"]["band"] == "warm"
        assert sum(v["weight"] for v in f.values()) == 100
        assert "fewer than 3" in f["associate_track_record"]["detail"]
        lead = db_session.get(Lead, body["lead_id"])
        assert lead.lead_score == LeadScore.medium  # the band mirrored onto the existing score

    def test_documents_verification_and_photos_raise_it(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        upload_required_documents(client, gna, oid)
        for slot in ("panel_installation_area", "wiring_path"):
            upload(client, gna, oid, photo_slot=slot)
        client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        r = client.post(f"{DASH}/onboardings/{oid}/verification-call",
                        json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
        f = _factors(r.json())
        assert f["documents_complete"]["points"] == 15 and f["verification_call"]["points"] == 20
        assert f["site_readiness"]["points"] == round(10 * 2 / 7, 2)
        assert r.json()["score"]["score"] == 85 and r.json()["score"]["band"] == "hot"
        assert db_session.get(Lead, r.json()["lead_id"]).lead_score == LeadScore.high

    def test_subsidy_eligibility_counts_dcr_only_for_pm_surya_ghar(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        f = _factors(_create(client, gna, n=1, scheme_linked=True, scheme_name="pm_surya_ghar",
                             dcr_panels_required=False, connection_in_customer_name=False))
        assert f["subsidy_eligibility"]["fraction"] == round(1 / 3, 4)
        assert "not met: connection in the customer's name, DCR panels" in f["subsidy_eligibility"]["detail"]
        f = _factors(_create(client, gna, n=2, dcr_panels_required=False))
        assert f["subsidy_eligibility"]["fraction"] == 1.0

    def test_system_fit(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        assert _factors(_create(client, gna, n=1, desired_output_kw=10, sanctioned_load_kw=5,
                                avg_monthly_units_kwh=300))["system_fit"]["fraction"] == 0.0
        assert _factors(_create(client, gna, n=2, desired_output_kw=10, sanctioned_load_kw=20,
                                avg_monthly_units_kwh=300))["system_fit"]["fraction"] == 0.5
        assert _factors(_create(client, gna, n=3, desired_output_kw=None))["system_fit"]["fraction"] == 0.5

    def test_timeline(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        assert _factors(_create(client, gna, n=1, customer_timeline="within_1_month"))["customer_timeline"]["points"] == 10
        assert _factors(_create(client, gna, n=2, customer_timeline=None))["customer_timeline"]["points"] == 0

    def test_track_record(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        for n in (1, 2, 3):
            oid = _create(client, gna, n=n)["onboarding_id"]
            upload_required_documents(client, gna, oid)
            client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
            if n < 3:
                client.post(f"{DASH}/onboardings/{oid}/verification-call",
                            json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
        f = _factors(_create(client, gna, n=4))
        assert f["associate_track_record"]["fraction"] == round(2 / 3, 4)
        assert f["associate_track_record"]["detail"] == "2 of 3 earlier submitted customers were verified"

    def test_response_deadline_follows_the_band(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        assert client.get(f"{BASE}/{oid}", headers=headers(gna)).json()["score"]["response_due_at"] is None
        upload_required_documents(client, gna, oid)
        body = client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).json()
        due = datetime.fromisoformat(body["score"]["response_due_at"])
        submitted = datetime.fromisoformat(body["submitted_at"])
        hours = {"hot": 4, "warm": 24, "cold": 72}[body["score"]["band"]]
        assert due - submitted == timedelta(hours=hours)
        verified = client.post(f"{DASH}/onboardings/{oid}/verification-call",
                               json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN).json()
        assert verified["score"]["response_due_at"] is None

    def test_config_validation(self, monkeypatch):
        from app.onboarding import config
        good = config.load()
        bad = {**good, "scoring": {**good["scoring"], "weights": {**good["scoring"]["weights"], "system_fit": 16}}}
        with pytest.raises(config.OnboardingConfigError, match="sum to 100"):
            config._validate(bad)


class TestEscalation:
    def _submitted(self, client, db_session, gna, n=1, **kw):
        oid = _create(client, gna, n=n, **kw)["onboarding_id"]
        upload_required_documents(client, gna, oid)
        client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        o = db_session.get(CustomerOnboarding, oid)
        db_session.refresh(o)
        return o

    def _run(self, db_session, now):
        from app.jobs.onboarding_escalations import check_onboarding_escalations
        return check_onboarding_escalations(db_session, now=now)

    def test_walks_the_preferred_chain_then_admin(self, client, db_session, onboarding_ready, captured):
        block = make_associate(db_session, level="block", territory=ALIPURDUAR_I)
        district = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        state = make_associate(db_session, level="state_head", territory=19)
        o = self._submitted(client, db_session, make_associate(db_session))
        due = o.response_due_at.replace(tzinfo=timezone.utc) if o.response_due_at.tzinfo is None else o.response_due_at
        period = timedelta(hours={"hot": 4, "warm": 24, "cold": 72}[o.score_band.value])

        assert self._run(db_session, due - timedelta(minutes=1))["escalated"] == []
        seen = []
        for k in range(5):
            result = self._run(db_session, due + period * k + timedelta(minutes=1))
            seen.append([(e["escalation_level"], e["escalated_to"], e["recipients"]) for e in result["escalated"]])
        assert seen == [[(1, "preferred_associates", [block])], [(2, "preferred_associates", [district])],
                        [(3, "preferred_associates", [state])], [(4, "admin", [])], []]
        # Re-running at the same moment sends nothing new.
        assert self._run(db_session, due + period * 10)["escalated"] == []
        overdue = [p for e, p in captured.events if e == "onboarding_overdue"]
        assert [p["escalation_level"] for p in overdue] == [1, 2, 3, 4]
        assert overdue[0]["recipients"][0]["associate_id"] == block

    def test_skips_empty_steps_and_never_the_owner(self, client, db_session, onboarding_ready, captured):
        owner = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        o = self._submitted(client, db_session, owner)
        due = o.response_due_at if o.response_due_at.tzinfo else o.response_due_at.replace(tzinfo=timezone.utc)
        result = self._run(db_session, due + timedelta(minutes=1))
        # No block Preferred, the District Head is the owner, no State Head: straight to Admin.
        assert [(e["escalation_level"], e["escalated_to"]) for e in result["escalated"]] == [(1, "admin")]

    def test_verification_stops_the_clock(self, client, db_session, onboarding_ready, captured):
        o = self._submitted(client, db_session, make_associate(db_session))
        client.post(f"{DASH}/onboardings/{o.id}/verification-call",
                    json={"verification_call_completed": True, "note": "ok"}, headers=ADMIN)
        assert self._run(db_session, datetime.now(timezone.utc) + timedelta(days=30)) == {"checked": 0, "escalated": []}

    def test_route_access(self, client, db_session, onboarding_ready):
        url = f"{DASH}/internal/check-onboarding-escalations"
        assert client.post(url, headers=ADMIN).status_code == 200
        assert client.post(url, headers=SALES).status_code == 403

    def test_email_goes_to_the_escalated_associates(self, monkeypatch):
        import httpx
        from app.notifications.notifier import EmailNotifier
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        n = EmailNotifier("key", "Grid Nova <n@x.test>", ["staff@x.test"], "https://app.test", Capture())
        base = {"onboarding_id": 7, "band": "warm", "score": 50, "response_due_at": "t", "overdue_hours": 3}
        n.notify("onboarding_overdue", {**base, "escalated_to": "preferred_associates",
                                        "recipients": [{"associate_id": "GNA-WB-26-00001", "email": "p@x.test"}]})
        n.notify("onboarding_overdue", {**base, "escalated_to": "admin"})
        assert [m["to"] for m in sent] == [["p@x.test"], ["staff@x.test"]]
        assert "customer #7" in sent[0]["subject"]
