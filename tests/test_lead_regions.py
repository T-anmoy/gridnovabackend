"""BE-19 — region scope on the lead list and lead detail (BE-17 applied it only to
associates and applications). A lead's state: its associate onboarding's site state, else
the project's state text when it names a state; unknown-state leads stay visible."""
import pytest
from sqlalchemy import select

from app.memory.models import CustomerOnboarding, Employee, LeadSource, TerritoryState
from tests.onboarding_support import (  # noqa: F401 — fixtures
    BASE, headers, kyc_ready, make_associate, onboarding_body, onboarding_ready, territories,
)
from tests.associate_support import BR, WB
from tests.test_access_control import D, multi, seed

MGMT_BR = multi("emp-mgmt-br", "management")


def _state(db, code):
    return db.execute(select(TerritoryState).where(TerritoryState.lgd_code == code)).scalar_one()


@pytest.fixture
def bihar_manager(db_session, territories):
    emp = seed(db_session, "emp-mgmt-br", "management")
    emp.regions = [_state(db_session, BR).name]
    db_session.commit()
    return emp


def _lead(memory, db, email, state=None):
    customer = memory.get_or_create_customer(email=email, name=email.split("@")[0])
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.other)
    lead.project.state = state
    db.commit()
    return lead.id


def test_list_and_detail(client, memory, db_session, bihar_manager):
    wb_name = _state(db_session, WB).name
    in_bihar = _lead(memory, db_session, "bihar@example.com", state=_state(db_session, BR).name.title())
    in_wb = _lead(memory, db_session, "wb@example.com", state=f"  {wb_name.lower()} ")
    unknown = _lead(memory, db_session, "chat@example.com", state=None)
    free_text = _lead(memory, db_session, "wb-abbrev@example.com", state="WB")   # not a state's name: unknown
    ids = {i["id"] for i in client.get(f"{D}/leads", headers=MGMT_BR).json()["items"]}
    assert ids == {in_bihar, unknown, free_text}
    assert client.get(f"{D}/leads", headers=MGMT_BR).json()["total"] == 3
    r = client.get(f"{D}/leads/{in_wb}", headers=MGMT_BR)
    assert r.status_code == 403 and r.json()["detail"] == "This record is outside the regions you work in."
    for lead_id in (in_bihar, unknown, free_text):
        assert client.get(f"{D}/leads/{lead_id}", headers=MGMT_BR).status_code == 200


def test_no_regions_sees_everything(client, memory, db_session, territories):
    seed(db_session, "emp-mgmt-all", "management")
    a = _lead(memory, db_session, "one@example.com", state=_state(db_session, WB).name)
    b = _lead(memory, db_session, "two@example.com", state=_state(db_session, BR).name)
    ids = {i["id"] for i in client.get(f"{D}/leads", headers=multi("emp-mgmt-all", "management")).json()["items"]}
    assert {a, b} <= ids


def test_associate_sourced_lead_uses_the_site_state(client, db_session, onboarding_ready, bihar_manager):
    gna = make_associate(db_session)
    r = client.post(BASE, json=onboarding_body(), headers=headers(gna))   # an Alipurduar (West Bengal) site
    assert r.status_code == 201, r.text
    onboarding = db_session.get(CustomerOnboarding, r.json()["onboarding_id"])
    onboarding_lead = onboarding.lead_id
    # Even if the project text claimed Bihar, the site state decides.
    from app.memory.models import Lead
    db_session.get(Lead, onboarding_lead).project.state = _state(db_session, BR).name
    db_session.commit()
    ids = {i["id"] for i in client.get(f"{D}/leads", headers=MGMT_BR).json()["items"]}
    assert onboarding_lead not in ids
    assert client.get(f"{D}/leads/{onboarding_lead}", headers=MGMT_BR).status_code == 403
    db_session.get(Employee, "emp-mgmt-br").regions = [_state(db_session, WB).name]
    db_session.commit()
    assert client.get(f"{D}/leads/{onboarding_lead}", headers=MGMT_BR).status_code == 200
