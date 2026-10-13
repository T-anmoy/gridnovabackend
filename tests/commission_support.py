"""Builders for the BE-4 commission, TDS and payout tests.

The scenario is a real onboarded customer in Alipurduar-I block (Alipurduar district,
West Bengal) whose formal proposal was accepted at a list price of basic 1,00,000.00 +
GST 8,900.00 = final 1,08,900.00, so a 20 % milestone (21,780.00) has a commission
basis of exactly 20,000.00 under basic_pro_rata. Territory seats are claimed through the
real seat service so their claimed_at/released_at history is what the rules read.

Rates here are TEST values chosen for round numbers — not Grid Nova's (none have been
supplied; registry C5-RATES).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.associates import service as associate_service
from app.commissions import periods, rates
from app.memory.models import (
    Associate, CommissionEntry, CustomerOnboarding, ProjectStage, Proposal, ProposalStatus, TerritoryBlock,
    TerritoryDistrict, TerritoryLevel, TerritorySeat, TerritoryState,
)
from tests.execution_support import (  # noqa: F401 — fixtures re-exported
    ACCOUNTS, ADMIN, ALIPURDUAR, ALIPURDUAR_I, DASH, KOLKATA, MGMT, OPS, SALES, advance_to, headers, kyc_ready,
    make_associate, new_customer, onboarding_ready, territories,
)
from tests.associate_support import WB
from tests.payments_support import RECORDER, staff_pay_url

ENG = {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}
ACCOUNTS_2 = {"X-Employee-Id": "emp-acc-3", "X-Employee-Role": "accounts"}
ADMIN_2 = {"X-Employee-Id": "emp-admin-2", "X-Employee-Role": "admin"}

BASIC, FINAL = Decimal("100000.00"), Decimal("108900.00")
TEST_RATES = {
    "personal_sale.associate.sales": "5",
    "personal_sale.associate.execution": "6",
    "personal_sale.preferred_associate.state_head": "4",
    "personal_sale.preferred_associate.district_head": "4.5",
    "personal_sale.preferred_associate.block": "4.75",
    "territory_state": "0.5",
    "territory_district": "1",
    "territory_block": "1.5",
}
PAST = date(2026, 1, 1)


def list_priced_content(basic: Decimal = BASIC, final: Decimal = FINAL) -> dict:
    return {"pricing": {"price_status": "list_price", "basic": str(basic), "gst": str(final - basic),
                        "final": str(final), "total_value": float(final)}}


def accepted(client, db, gna: str, *, content=None, total=FINAL, stage=ProjectStage.accepted, **customer) -> int:
    oid = new_customer(client, gna, **customer)
    onboarding = db.get(CustomerOnboarding, oid)
    db.add(Proposal(lead_id=onboarding.lead_id, version=1, status=ProposalStatus.accepted,
                    content=list_priced_content() if content is None else content, total_value=float(total)))
    db.commit()
    advance_to(db, oid, stage)
    return oid


def seat(db, gna: str, *, back_days: int = 30) -> TerritorySeat:
    """Claim the seat a make_associate Preferred Associate was made for, `back_days` ago."""
    a = db.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one()
    place = associate_service.Place(
        state=db.get(TerritoryState, a.territory_state_id),
        district=db.get(TerritoryDistrict, a.territory_district_id) if a.territory_district_id else None,
        block=db.get(TerritoryBlock, a.territory_block_id) if a.territory_block_id else None)
    row = associate_service.claim_seat(db, a, a.level, place, "emp-admin-1")
    row.claimed_at = datetime.now(timezone.utc) - timedelta(days=back_days)
    db.commit()
    return row


def network(db) -> dict:
    """A State Head of West Bengal, the District Head of Alipurduar and a Block
    Associate of Alipurduar-I, each holding their seat."""
    out = {"state": make_associate(db, level="state_head", territory=WB),
           "district": make_associate(db, level="district_head", territory=ALIPURDUAR),
           "block": make_associate(db, level="block", territory=ALIPURDUAR_I)}
    for gna in out.values():
        seat(db, gna)
    return out


def publish(db, *, effective_from: date = PAST, is_placeholder: bool = False, overrides=None,
            actor: str = "emp-admin-1"):
    """Publish a schedule as if `today` were far enough before effective_from to meet the
    notice period (the notice rule itself is tested through the route)."""
    schedule = rates.publish(db, effective_from=effective_from, is_placeholder=is_placeholder,
                             rates={**TEST_RATES, **(overrides or {})}, note="test rates", actor=actor,
                             today=effective_from - timedelta(days=400))
    db.commit()
    return schedule


def record_and_verify(client, oid: int, amount: str, reference: str, verifier=ACCOUNTS) -> int:
    r = client.post(staff_pay_url(oid), json={"amount": amount, "mode": "neft", "reference": reference},
                    headers=RECORDER)
    assert r.status_code == 201, r.text
    pid = r.json()["payment_id"]
    v = client.post(f"{DASH}/payments/{pid}/verify", headers=verifier)
    assert v.status_code == 200, v.text
    return pid


def record_cheque_and_verify(client, oid: int, amount: str, number: str) -> int:
    body = {"amount": amount, "mode": "cheque", "reference": number, "bank_name": "State Bank of India",
            "instrument_date": "2026-09-20"}
    r = client.post(staff_pay_url(oid), json=body, headers=RECORDER)
    assert r.status_code == 201, r.text
    pid = r.json()["payment_id"]
    assert client.post(f"{DASH}/payments/{pid}/deposit", headers=ACCOUNTS).status_code == 200
    v = client.post(f"{DASH}/payments/{pid}/verify", headers=ACCOUNTS)
    assert v.status_code == 200, v.text
    return pid


def dishonour(client, pid: int, reason: str = "Cheque returned unpaid"):
    r = client.post(f"{DASH}/payments/{pid}/dishonour", json={"reason": reason}, headers=ACCOUNTS)
    assert r.status_code == 200, r.text
    return r


def entries(db, **where) -> list[CommissionEntry]:
    db.expire_all()
    q = select(CommissionEntry)
    for k, v in where.items():
        q = q.where(getattr(CommissionEntry, k) == v)
    return list(db.execute(q.order_by(CommissionEntry.id)).scalars())


def pk(db, gna: str) -> int:
    return db.execute(select(Associate.id).where(Associate.associate_id == gna)).scalar_one()


def by_role(rows) -> dict:
    return {r.earning_role.value: r for r in rows}


def after_month_end() -> date:
    """A 'today' on which the current month has ended (the 5th of next month)."""
    return periods.first_day(periods.next_period(periods.period_of(periods.today_ist()))).replace(day=5)


def this_period() -> str:
    return periods.period_of(periods.today_ist())


def approve_all(client, db, who=ACCOUNTS) -> list[int]:
    ids = [e.id for e in entries(db) if e.status.value == "earned" and e.amount is not None
           and e.kind.value == "earn"]
    if ids:
        r = client.post(f"{DASH}/commissions/approve", json={"entry_ids": ids}, headers=who)
        assert r.status_code == 200, r.text
    return ids
