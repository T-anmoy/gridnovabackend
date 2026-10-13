"""BE-5 Phase 1 — the handover pack, its gate and the warranty register."""
from datetime import date

import pytest
from sqlalchemy import select

from app.handover import config, warranty
from app.memory.models import AuditLog, HandoverPack, InstalledUnit, ProjectStage, ProjectWarranty, WarrantyItem
from tests.execution_support import staff_exec_url
from tests.handover_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, ENG, MGMT, OPS, SALES, catalogue, hand_url, kyc_ready, notifications, onboarding_ready, png,
    prepare, record_handover, stub_pdf, territories,
)


def items(client, oid, who=OPS):
    r = client.get(hand_url(oid), headers=who)
    assert r.status_code == 200, r.text
    return r.json()


class TestChecklist:
    def test_items_come_from_records_and_manual_ticks(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False)
        body = items(client, oid)
        done = {i["key"]: i["done"] for i in body["items"]}
        assert done["commissioning_report"] and done["register_verified"] and done["final_payment"]
        assert not done["orientation"] and not done["acknowledgement"] and not done["om_contact"]
        assert body["complete"] is False and set(body["missing"]) == {"orientation", "acknowledgement", "om_contact"}

    def test_nothing_done_without_records(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False, payments=False, register=False, documents=False)
        body = items(client, oid)
        assert {"commissioning_report", "register_verified", "final_payment"} <= set(body["missing"])

    def test_ticks_are_audited_and_reversible(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False)
        assert client.post(hand_url(oid, "/orientation"), json={"done": True, "note": "n"}, headers=OPS).status_code == 200
        r = client.post(hand_url(oid, "/orientation"), json={"done": False}, headers=OPS)
        assert not [i for i in r.json()["items"] if i["key"] == "orientation"][0]["done"]
        actions = [a.action for a in db_session.execute(select(AuditLog).where(AuditLog.entity_type == "handover_pack"))
                   .scalars()]
        assert actions == ["orientation_ticked", "orientation_unticked"]

    def test_acknowledgement_signature_must_be_png(self, client, db_session, onboarding_ready, catalogue, monkeypatch):
        from app.storage import r2
        monkeypatch.setattr(r2, "upload_bytes", lambda *a, **k: "k")
        oid = prepare(client, db_session, catalogue, ticks=False)
        bad = client.post(hand_url(oid, "/acknowledgement"), data={"method": "signature", "customer_name": "A"},
                          files={"file": ("s.jpg", b"\xff\xd8\xff" + b"0" * 20, "image/jpeg")}, headers=OPS)
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_file"
        ok = client.post(hand_url(oid, "/acknowledgement"), data={"method": "signature", "customer_name": "A"},
                         files={"file": ("s.png", png(), "image/png")}, headers=OPS)
        assert ok.status_code == 200
        ack = [i for i in ok.json()["items"] if i["key"] == "acknowledgement"][0]
        assert ack["done"] and ack["method"] == "signature"

    def test_roles(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False)
        for who in (OPS, ADMIN, MGMT, ENG, SALES):
            assert client.get(hand_url(oid), headers=who).status_code in (200, 403)
        assert client.get(hand_url(oid), headers=ACCOUNTS).status_code == 403
        for who in (MGMT, ENG, SALES, ACCOUNTS):
            assert client.post(hand_url(oid, "/orientation"), json={"done": True}, headers=who).status_code == 403
        assert client.get(hand_url(oid), headers=MGMT).status_code == 200
        assert client.get(hand_url(oid), headers=ENG).status_code == 200


class TestGate:
    def test_incomplete_pack_is_a_structured_422(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False)
        r = record_handover(client, oid)
        assert r.status_code == 422
        d = r.json()["detail"]
        assert d["code"] == "handover_pack_incomplete"
        assert set(d["missing"]) == {"orientation", "acknowledgement", "om_contact"}

    def test_complete_pack_records_and_writes_the_register(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue)
        assert record_handover(client, oid).status_code == 200
        w = db_session.execute(select(ProjectWarranty)).scalar_one()
        assert w.system_warranty_years == 5
        assert w.system_warranty_end == warranty.add_years(w.handover_date, 5)
        assert w.om_free_until == warranty.add_months(w.handover_date, 12)
        items_ = db_session.execute(select(WarrantyItem)).scalars().all()
        assert len(items_) == 3 and {i.source for i in items_} == {"none"}   # no register dates, no defaults
        assert client.post(hand_url(oid, "/orientation"), json={"done": False}, headers=OPS).json()["detail"]["code"] \
            == "handover_recorded"

    def test_register_dates_are_kept(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue)
        unit = db_session.execute(select(InstalledUnit).order_by(InstalledUnit.id)).scalars().first()
        unit.warranty_start, unit.warranty_end = date(2026, 1, 1), date(2036, 1, 1)
        db_session.commit()
        assert record_handover(client, oid).status_code == 200
        item = db_session.execute(select(WarrantyItem).where(WarrantyItem.unit_id == unit.id)).scalar_one()
        assert (item.warranty_end, item.source) == (date(2036, 1, 1), "register")

    def test_configured_unit_default_is_used(self, client, db_session, onboarding_ready, catalogue, monkeypatch):
        monkeypatch.setitem(config.warranty()["unit_default_years"], "inverter", 7)
        oid = prepare(client, db_session, catalogue)
        assert record_handover(client, oid).status_code == 200
        inv = db_session.execute(select(WarrantyItem).where(WarrantyItem.category == "inverter")).scalar_one()
        assert inv.source == "default" and inv.warranty_end == warranty.add_years(inv.warranty_start, 7)

    def test_gate_can_be_switched_off(self, client, db_session, onboarding_ready, catalogue, monkeypatch):
        monkeypatch.setitem(config.handover(), "pack_required", False)
        oid = prepare(client, db_session, catalogue, ticks=False)
        assert record_handover(client, oid).status_code == 200

    def test_grandfathered_project_is_not_rechecked(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue, ticks=False, payments=False, register=False, documents=False)
        advance_to_handover(db_session, oid)
        body = items(client, oid)
        assert body["handover_recorded"] and body["warranty"]["system"]["years"] == 5


def advance_to_handover(db, oid):
    from tests.handover_support import advance_to
    advance_to(db, oid, ProjectStage.handover_complete)


def test_leap_day_and_month_arithmetic():
    assert warranty.add_years(date(2024, 2, 29), 5) == date(2029, 2, 28)
    assert warranty.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert warranty.add_months(date(2026, 11, 15), 12) == date(2027, 11, 15)
