"""Builders for the Phase 4A payments and receipts tests."""
from __future__ import annotations

import pytest

from app.memory.models import AssociateCapability, CustomerOnboarding, ProjectStage, Proposal, ProposalStatus
from tests.execution_support import (  # noqa: F401 — fixtures re-exported
    ACCOUNTS, ADMIN, DASH, ENG, MGMT, OPS, SALES, advance_to, headers, kyc_ready, make_associate, new_customer,
    notifications, onboarding_ready, territories,
)

PAY = "/api/v1/dashboard/payments"


def pay_url(oid: int) -> str:
    return f"/api/v1/associate/onboardings/{oid}/payments"


def staff_pay_url(oid: int) -> str:
    return f"{DASH}/onboardings/{oid}/payments"


def accepted_customer(client, db, *, total=100000.0, stage=ProjectStage.accepted, capability=AssociateCapability.sales,
                      **customer) -> tuple[str, int]:
    """An onboarded customer whose formal proposal (total `total`) was accepted, with
    stages recorded up to `stage` — the payment schedule exists from `accepted` on."""
    gna = make_associate(db, capability=capability)
    oid = new_customer(client, gna, **customer)
    onboarding = db.get(CustomerOnboarding, oid)
    db.add(Proposal(lead_id=onboarding.lead_id, version=1, status=ProposalStatus.accepted, content={},
                    total_value=total))
    db.commit()
    advance_to(db, oid, stage)
    return gna, oid


def neft(amount="20000.00", reference="UTR-0001", **extra) -> dict:
    return {"amount": amount, "mode": "neft", "reference": reference, **extra}


def cheque(amount="20000.00", reference="000123", bank="State Bank of India", **extra) -> dict:
    return {"amount": amount, "mode": "cheque", "reference": reference, "bank_name": bank,
            "instrument_date": "2026-09-20", **extra}


# A second Accounts person. Nobody verifies a payment they recorded (segregation of
# duties), so when a test has "Accounts" record a payment and "Accounts" verify it, the
# recording is done by this colleague.
RECORDER = {"X-Employee-Id": "emp-acc-2", "X-Employee-Role": "accounts"}


def record(client, who, oid, body, staff=False):
    if staff and who == ACCOUNTS:
        who = RECORDER
    return client.post(staff_pay_url(oid) if staff else pay_url(oid), json=body, headers=who)


def proof(client, gna, oid, pid):
    from tests.execution_support import PDF
    return client.post(f"{pay_url(oid)}/{pid}/proof", files={"file": ("proof.pdf", PDF, "application/pdf")},
                       headers=headers(gna))


@pytest.fixture
def stub_pdf(monkeypatch):
    from app.associates import documents
    monkeypatch.setattr(documents, "render_pdf", lambda html: b"%PDF-1.4 receipt " + html.encode()[:64])


@pytest.fixture
def real_receipt_text(monkeypatch):
    """Stand in Grid Nova's real company block and wording (the shipped config holds
    DUMMY text, which correctly holds every receipt)."""
    from app.execution import config
    receipt = config.payments()["receipt"]
    monkeypatch.setitem(receipt, "company", {**receipt["company"], "gstin": "19ABCDE1234F1Z5",
                                             "bank_name": "State Bank of India", "account_number": "000011112222",
                                             "ifsc": "SBIN0001234"})
    monkeypatch.setitem(receipt, "wording", {"heading": "Payment Receipt",
                                             "footer": "This is a payment receipt, not a GST tax invoice."})
