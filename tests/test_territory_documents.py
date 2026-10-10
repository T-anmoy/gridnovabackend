"""BE-1 S9 — a Preferred Associate viewing a downline customer does not get that
customer's documents, contact details or price.

Before: any territory viewer could fetch signed links to every uploaded document (ID
proof, bills), the agreement and the execution files, and saw the customer's mobile,
email and the formal proposal's price. Now (config territory_viewers_see_documents,
an unconfirmed default of false) those are the owner's, and every link is audited.
"""
import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, CustomerOnboarding, Proposal, ProposalStatus
from app.onboarding import config as onboarding_config
from tests.execution_support import (  # noqa: F401 — fixtures
    ALIPURDUAR, BASE, PDF, headers, kyc_ready, make_associate, new_customer, onboarding_ready, territories,
)


@pytest.fixture
def world(client, db_session, onboarding_ready, monkeypatch):
    from app.storage import r2
    monkeypatch.setattr(r2, "upload_bytes", lambda key, body, ct: key)
    monkeypatch.setattr(r2, "signed_get_url", lambda key, **k: (f"https://signed.example/{key}",
                                                               __import__("datetime").datetime(2026, 1, 1)))
    owner = make_associate(db_session, home_district=ALIPURDUAR)
    viewer = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
    oid = new_customer(client, owner, customer_email="buyer@example.com")
    r = client.post(f"{BASE}/{oid}/files", headers=headers(owner), data={"kind": "document",
                    "document_type": "id_proof"}, files={"file": ("id.pdf", PDF, "application/pdf")})
    assert r.status_code == 201, r.text
    file_ref = next(d["file_ref"] for d in r.json()["documents"] if d.get("document_type") == "id_proof")
    lead_id = db_session.get(CustomerOnboarding, oid).lead_id
    db_session.add(Proposal(lead_id=lead_id, version=1, status=ProposalStatus.approved, content={},
                            total_value=123456.0, approved_by_employee_id="emp-admin-1",
                            pdf_storage_key="proposals/1/p.pdf"))
    db_session.commit()
    return {"owner": owner, "viewer": viewer, "oid": oid, "file_ref": file_ref}


def test_the_owner_opens_documents_and_each_link_is_audited(client, db_session, world):
    r = client.get(f"{BASE}/{world['oid']}/files/{world['file_ref']}", headers=headers(world["owner"]))
    assert r.status_code == 200
    rows = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "onboarding_document",
                                                     AuditLog.action == "link_issued")).scalars().all()
    assert len(rows) == 1 and rows[0].after_value["kind"] == "onboarding_file"
    assert rows[0].performed_by == f"associate:{world['owner']}"


@pytest.mark.parametrize("path", ["files/{file_ref}", "agreement"])
def test_a_territory_viewer_cannot_open_documents(client, world, path):
    url = f"{BASE}/{world['oid']}/" + path.format(file_ref=world["file_ref"])
    r = client.get(url, headers=headers(world["viewer"]))
    assert r.status_code == 403
    assert r.json()["detail"] == "Only the associate who onboarded this customer can open its documents."


def test_switching_the_setting_on_lets_the_viewer_in(client, world, monkeypatch):
    monkeypatch.setattr(onboarding_config, "territory_viewers_see_documents", lambda: True)
    r = client.get(f"{BASE}/{world['oid']}/files/{world['file_ref']}", headers=headers(world["viewer"]))
    assert r.status_code == 200


def test_the_viewer_sees_masked_contact_details(client, world):
    viewer = client.get(f"{BASE}/{world['oid']}", headers=headers(world["viewer"])).json()["customer"]
    owner = client.get(f"{BASE}/{world['oid']}", headers=headers(world["owner"])).json()["customer"]
    assert owner["mobile"].endswith(viewer["mobile"][-4:]) and viewer["mobile"].startswith("******")
    assert owner["email"] == "buyer@example.com" and viewer["email"] == "b***@example.com"
    listed = client.get(BASE, headers=headers(world["viewer"])).json()["items"]
    assert [i["customer"]["mobile"] for i in listed if i["onboarding_id"] == world["oid"]] == [viewer["mobile"]]


def test_the_viewer_gets_no_price_and_no_pdf(client, world):
    viewer = client.get(f"{BASE}/{world['oid']}/formal-proposal", headers=headers(world["viewer"])).json()
    owner = client.get(f"{BASE}/{world['oid']}/formal-proposal", headers=headers(world["owner"])).json()
    assert viewer["prepared"] is True and viewer["total_value"] is None and viewer["pdf"] is None
    assert owner["total_value"] == 123456.0 and owner["pdf"]["url"].startswith("https://signed.example/")
