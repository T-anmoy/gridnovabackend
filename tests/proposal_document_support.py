"""Helpers for BE-13 proposal documents (app/proposals/document.py): read, edit and
complete a drafted proposal's document through the dashboard routes, and read the text
of a rendered PDF. Test data is invented — never a real customer's."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest

DASH = "/api/v1/dashboard"
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}

# What staff type when the system holds none of it (a chat lead has no site assessment).
FILL = {
    "addressee": {"customer_type": "individual", "name": "Test Customer",
                  "address_lines": ["12 Test Road", "Kolkata – 700001, West Bengal"]},
    "site": {"address_lines": ["12 Test Road", "Kolkata – 700001, West Bengal"], "town": "Kolkata",
             "state": "West Bengal"},
    "connection": {"discom": "WBSEDCL", "consumer_number": "100000001", "category": "Residential",
                   "phase": "single"},
    "plant": {"capacity_kwp": "3", "package": "NOVA_ASSURED", "dcr": False},
}


def document(client, proposal_id: int, headers: dict = ADMIN) -> dict:
    r = client.get(f"{DASH}/proposals/{proposal_id}/document", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def edit(client, proposal_id: int, section: str, value, headers: dict = ADMIN, base_revision: int | None = None):
    if base_revision is None:
        base_revision = document(client, proposal_id)["revision"]
    return client.patch(f"{DASH}/proposals/{proposal_id}/document", headers=headers,
                        json={"base_revision": base_revision, "section": section, "value": value})


def complete_document(client, proposal_id: int, headers: dict = ADMIN, basic_price: str = "250000") -> dict:
    """Fill every section the document still misses (as staff would), and set a price
    when it has none. Returns the final document view."""
    view = document(client, proposal_id)
    fields = {m["field"] for m in view["missing_required"]}
    doc = view["document"]
    for section, value in FILL.items():
        if any(f.startswith(section + ".") for f in fields):
            # Only the gaps are filled; what the system already holds is kept.
            merged = {**value, **{k: v for k, v in doc[section].items() if v not in (None, [], "")}}
            r = edit(client, proposal_id, section, merged, headers=ADMIN)
            assert r.status_code == 200, r.text
            doc = r.json()["document"]
    view = document(client, proposal_id)
    if any(m["field"] == "price" for m in view["missing_required"]):
        r = client.patch(f"{DASH}/proposals/{proposal_id}/adjust-pricing", headers=headers,
                         json={"basic_price": basic_price, "reason": "Set for the test"})
        assert r.status_code == 200, r.text
        view = document(client, proposal_id)
    assert view["missing_required"] == [], view["missing_required"]
    return view


def require_weasyprint():
    try:
        import weasyprint  # noqa: F401
    except OSError:
        pytest.skip("WeasyPrint's native libraries are not available")


def pdf_text(pdf: bytes) -> list[str]:
    """The text of each page (pypdf)."""
    from pypdf import PdfReader
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


# ── Documents with the two samples' figures (invented names and addresses) ───────

def compact_document():
    from app.proposals import document as d
    doc = d.ProposalDocument(
        segment="residential",
        addressee=d.Addressee(customer_type="individual", title="Mr.", name="Test Customer",
                              address_lines=["Habra, North 24 Parganas", "West Bengal – 743263"], mobile="9830000001",
                              aadhaar_last4="3832", salutation="Dear Sir,"),
        connection=d.Connection(discom="WBSEDCL", consumer_number="100000001", sub_division="Habra / Ashoknagar CCC",
                                sanctioned_load_kw=Decimal("3.52"), category="Residential", phase="single"),
        site=d.Site(address_lines=["Habra, North 24 Parganas", "West Bengal – 743263"], town="Habra",
                    state="West Bengal"),
        pm_surya_ghar=d.PmSuryaGhar(subsidy_opted=True, portal_registered=True, approved_capacity_kwp=Decimal(3)),
        plant=d.Plant(capacity_kwp=Decimal(3), package="NOVA_ASSURED", dcr=True),
        price=d.Price(mode="price_list", basic=Decimal("185000.00"), price_list_version=1))
    d.refresh_defaults(doc)
    return doc


def detailed_document():
    from app.proposals import document as d
    doc = d.ProposalDocument(
        segment="commercial",
        addressee=d.Addressee(customer_type="organisation", title="M/s", name="Example Metal Works",
                              attention="Mr. Test Director (Director)",
                              address_lines=["1, Example Road", "Kolkata – 700006, West Bengal"], salutation="Dear Sir,"),
        connection=d.Connection(category="Commercial", phase="three"),
        site=d.Site(address_lines=["Example Mouza, Example Village", "P.O. – Example, Amta",
                                   "Dist. – Howrah – 711401, West Bengal"], town="Amta, Howrah", state="West Bengal"),
        plant=d.Plant(capacity_kwp=Decimal(52)),
        price=d.Price(mode="staff_basic", basic=Decimal("2860000")))
    d.refresh_defaults(doc)
    return doc


SAMPLE_DAY = date(2026, 10, 23)
