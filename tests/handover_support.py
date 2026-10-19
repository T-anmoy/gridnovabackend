"""Builders for the BE-5 handover, warranty and service-request tests."""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.memory.models import CustomerOnboarding, Equipment, ProjectStage
from scripts import seed_dummy_data
from tests.payments_support import (  # noqa: F401 — fixtures re-exported
    ACCOUNTS, ADMIN, DASH, ENG, MGMT, OPS, PAY, RECORDER, SALES, accepted_customer, advance_to, headers, kyc_ready,
    neft, notifications, onboarding_ready, record, stub_pdf, territories,
)
from tests.execution_support import PDF, staff_exec_url, upload

HAND = f"{DASH}/onboardings"


def png(size=(4, 4), color=(10, 20, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def catalogue(db_session):
    seed_dummy_data.seed(db_session, "development")
    rows = {e.model_name: e.id for e in db_session.execute(select(Equipment)).scalars()}
    return {"dcr": rows["DUMMY-M540-DCR"], "inv": rows["DUMMY-INV-5K"]}


@pytest.fixture
def real_handover_text(monkeypatch):
    """Stand in Grid Nova's real O&M terms (the shipped config holds a dummy, which
    correctly holds the pack)."""
    from app.handover import config
    monkeypatch.setitem(config.warranty(), "om_after_free_terms", "Annual maintenance plan at the then-current rate.")


def hand_url(oid: int, tail: str = "") -> str:
    return f"{HAND}/{oid}/handover{tail}"


def prepare(client, db, catalogue, *, email="asha@example.test", ticks=True, payments=True, register=True,
            documents=True, stage=ProjectStage.commissioned) -> int:
    """A Grid Nova-executed project advanced to `commissioned`, with everything the pack
    computes from records in place, and (ticks=True) the three manual items done. Returns
    the onboarding id; `handover_complete` is NOT recorded."""
    gna, oid = accepted_customer(client, db, stage=ProjectStage.agreement_signed, customer_email=email)
    advance_to(db, oid, stage)
    base = staff_exec_url(oid)
    if documents:
        for doc in ("commissioning_report", "net_meter_report", "handover_checklist"):
            assert upload(client, OPS, base, document_type=doc).status_code == 201
    if register:
        rows = [{"category": "module", "equipment_id": catalogue["dcr"], "serial_number": f"HW-MOD-{oid}-{i}",
                 "rating_wp": "540", "is_dcr": True} for i in range(2)]
        rows.append({"category": "inverter", "equipment_id": catalogue["inv"], "serial_number": f"HW-INV-{oid}",
                     "rating_kw": "5"})
        assert client.post(f"{base}/units/bulk", json={"units": rows}, headers=OPS).status_code == 201
        assert client.post(f"{base}/register/verify", headers=OPS).status_code == 200
    if payments:
        for n, amount in enumerate(("20000.00", "60000.00", "20000.00")):
            pid = record(client, ACCOUNTS, oid, neft(amount=amount, reference=f"UTR-HW-{oid}-{n}"), staff=True).json()["payment_id"]
            assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).status_code == 200
    if ticks:
        tick(client, oid)
    return oid


def tick(client, oid, who=OPS):
    assert client.post(hand_url(oid, "/orientation"), json={"done": True, "note": "Walked through the app"},
                       headers=who).status_code == 200
    assert client.post(hand_url(oid, "/om-contact"), json={"done": True}, headers=who).status_code == 200
    r = client.post(hand_url(oid, "/acknowledgement"), data={"method": "in_person", "customer_name": "Asha Roy"},
                    headers=who)
    assert r.status_code == 200, r.text


def record_handover(client, oid, who=OPS):
    return client.post(f"{staff_exec_url(oid)}/handover_complete", json={}, headers=who)
