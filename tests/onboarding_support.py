"""Fixtures and builders for the associate-led onboarding tests.

Associates are inserted directly (the Phase 2A approval flow has its own tests) against
the real-LGD territory subset from tests/associate_support.py. PIN codes are real ones
from data/pincodes/ for the districts used."""
from __future__ import annotations

from datetime import date, datetime, timezone
from itertools import count

import pytest
from sqlalchemy import select

from app.memory.models import (
    Associate, AssociateApplication, AssociateCapability, AssociateStatus, AssociateType,
    PreferredAssociateApplication, TerritoryBlock, TerritoryDistrict, TerritoryLevel, TerritoryState,
)
from tests.associate_support import (  # noqa: F401 — fixtures re-exported
    ALIPURDUAR, ALIPURDUAR_I, ALIPURDUAR_II, ARARIA, ARARIA_BLOCK, BR, KOLKATA, WB, kyc_ready, territories,
)
from tests.conftest import seed_employees

BASE = "/api/v1/associate/onboardings"
DASH = "/api/v1/dashboard"
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
SALES = {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"}
MGMT = {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"}
ENG = {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}

PIN = {ALIPURDUAR: "736121", KOLKATA: "700001", ARARIA: "854311"}
_seq = count(1)


def headers(associate_id: str) -> dict:
    return {"X-Associate-Id": associate_id}


def _row(db, model, code):
    return db.execute(select(model).where(model.lgd_code == code)).scalar_one()


def make_associate(db, *, level: str | None = None, territory: int | None = None,
                   home_district: int = KOLKATA, home_block: int | None = None,
                   status: AssociateStatus = AssociateStatus.active,
                   capability: AssociateCapability = AssociateCapability.sales) -> str:
    """Insert an associate; `level` + `territory` (an LGD code) make it a Preferred
    Associate. `home_district`/`home_block` (LGD codes) are the registered address —
    which is what locates a plain Associate. Returns its GNA id."""
    seed_employees(db, "emp-admin-1")
    n = next(_seq)
    now = datetime.now(timezone.utc)
    home = _row(db, TerritoryDistrict, home_district)
    state = db.get(TerritoryState, home.state_id)
    common = dict(full_name=f"Assoc {n}", date_of_birth=date(1990, 1, 1), address_line="x", state_id=state.id,
                  district_id=home.id, block_id=_row(db, TerritoryBlock, home_block).id if home_block else None,
                  pin_code=PIN[home_district],
                  mobile=f"70000{n:05d}", email=f"assoc{n}@example.test", aadhaar_last4="1234",
                  pan=f"ABCPE{n:04d}F", bank_account_encrypted="enc", bank_account_last4="0000",
                  bank_ifsc="SBIN0001234", kyc_consent_version="v", kyc_consent_accepted_at=now, documents={},
                  verification_call_completed=False)
    associate_id = f"GNA-WB-26-{90000 + n:05d}"
    if level is None:
        app = AssociateApplication(**common, capability=capability)
        db.add(app)
        db.flush()
        row = Associate(associate_id=associate_id, associate_type=AssociateType.associate,
                        associate_application_id=app.id, capability=capability)
    else:
        lvl = TerritoryLevel(level)
        st = dist = blk = None
        if lvl == TerritoryLevel.state_head:
            st = _row(db, TerritoryState, territory)
        elif lvl == TerritoryLevel.district_head:
            dist = _row(db, TerritoryDistrict, territory)
            st = db.get(TerritoryState, dist.state_id)
        else:
            blk = _row(db, TerritoryBlock, territory)
            dist = db.get(TerritoryDistrict, blk.district_id)
            st = db.get(TerritoryState, dist.state_id)
        app = PreferredAssociateApplication(
            **common, requested_level=lvl, requested_state_id=st.id,
            requested_district_id=dist.id if dist else None, requested_block_id=blk.id if blk else None,
            business_type="x", office_address="x", prior_experience="x", team_size=1, references=[])
        db.add(app)
        db.flush()
        row = Associate(associate_id=associate_id, associate_type=AssociateType.preferred_associate,
                        preferred_application_id=app.id, level=lvl, territory_state_id=st.id,
                        territory_district_id=dist.id if dist else None, territory_block_id=blk.id if blk else None)
    row.status = status
    row.full_name, row.email, row.mobile = common["full_name"], common["email"], common["mobile"]
    row.state_id, row.approved_by_employee_id = state.id, "emp-admin-1"
    db.add(row)
    db.commit()
    return associate_id


def onboarding_body(n: int = 1, district: int = ALIPURDUAR, block: int | None = ALIPURDUAR_I, **overrides) -> dict:
    from app.onboarding import config

    state = BR if district == ARARIA else WB
    body = {
        "customer_name": f"Customer {n}",
        "customer_mobile": f"98300{n:05d}",
        "customer_type": "individual",
        "project_family": "rooftop_solar",
        "scheme_linked": False,
        "scheme_name": "none",
        "state_lgd_code": state,
        "district_lgd_code": district,
        "block_lgd_code": block,
        "pin_code": PIN[district],
        "full_address": f"{n} Test Lane",
        "consumer_number": f"CN-{n:06d}",
        "site_type": "rooftop_residential",
        "connection_type": "on_grid",
        "sanctioned_load_kw": 5,
        "desired_output_kw": 3,
        "avg_monthly_units_kwh": 400,
        "dcr_panels_required": True,
        "connection_in_customer_name": True,
        "property_in_customer_name": True,
        "customer_timeline": "within_3_months",
        "notice_version": config.current_notice()[0],
        "notice_acknowledged": True,
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"%" * 64


def upload(client, associate_id, onboarding_id, *, document_type=None, photo_slot=None, content=None, **form):
    kind = "document" if document_type else "photo"
    data = {"kind": kind, **({"document_type": document_type} if document_type else {"photo_slot": photo_slot}), **form}
    body = content if content is not None else (PDF if document_type else PNG)
    name = "file.pdf" if body.startswith(b"%PDF") else "file.png"
    return client.post(f"{BASE}/{onboarding_id}/files", data=data,
                       files={"file": (name, body, "application/octet-stream")}, headers=headers(associate_id))


def upload_required_documents(client, associate_id, onboarding_id) -> None:
    detail = client.get(f"{BASE}/{onboarding_id}", headers=headers(associate_id)).json()
    for doc in detail["checklist"]["missing"]:
        assert upload(client, associate_id, onboarding_id, document_type=doc).status_code == 201


SITE_TEXT = "26.4900, 89.5200"   # in Alipurduar district, where the default test customer is


def confirm_site_location(client, associate_id, onboarding_id, text: str = SITE_TEXT) -> dict:
    """Set the site location (pasted coordinates) and confirm it — what submitting needs
    besides the documents (Phase 3C)."""
    r = client.post(f"{BASE}/{onboarding_id}/site-location", json={"source": "pasted_coordinates", "text": text},
                    headers=headers(associate_id))
    assert r.status_code == 200, r.text
    r = client.post(f"{BASE}/{onboarding_id}/site-location/confirm", headers=headers(associate_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def onboarding_ready(kyc_ready, db_session):
    """Storage, encryption key and the territory subset — plus the staff rows used."""
    seed_employees(db_session, "emp-admin-1", "emp-sales-1")
    return kyc_ready
