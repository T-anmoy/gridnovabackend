"""Proposal Studio (BE-18) test helpers: the sample documents of the brief's visual set and
the API steps a test repeats. Every name, address and number is invented."""
from __future__ import annotations

from decimal import Decimal

from app.proposals import document as d

D = "/api/v1/dashboard"
DESK = {"X-Employee-Id": "emp-desk-1", "X-Employee-Role": "proposal_desk"}
DESK2 = {"X-Employee-Id": "emp-desk-2", "X-Employee-Role": "proposal_desk"}
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
MGMT = {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"}


def _org(name, lines, attention=None):
    return d.Addressee(customer_type="organisation", title="M/s", name=name, attention=attention, address_lines=lines,
                       mobile="9830012345", salutation="Dear Sir / Madam,")


def residential_3kw_pmsg() -> d.StudioDocument:
    doc = d.new_studio_document("residential_rooftop")
    doc.addressee = d.Addressee(customer_type="individual", title="Mr.", name="Test Kumar Das",
                                address_lines=["12 Example Lane, Habra", "North 24 Parganas, West Bengal – 743263"],
                                mobile="9830000001", aadhaar_last4="1234", salutation="Dear Sir,")
    doc.connection = d.Connection(discom="WBSEDCL", consumer_number="100000001", sub_division="Habra CCC",
                                  sanctioned_load_kw=Decimal("3.52"), category="Residential", phase="single")
    doc.site = d.Site(address_lines=list(doc.addressee.address_lines), town="Habra", state="West Bengal")
    doc.pm_surya_ghar = d.PmSuryaGhar(subsidy_opted=True, portal_registered=True, approved_capacity_kwp=Decimal(3))
    doc.plant = d.Plant(capacity_kwp=Decimal(3), package="NOVA_ASSURED", dcr=True)
    doc.price = d.StudioPrice(mode="price_list")
    return _settle(doc)


def residential_5kw_comparison() -> d.StudioDocument:
    doc = d.new_studio_document("residential_rooftop")
    doc.addressee = d.Addressee(customer_type="individual", title="Ms.", name="Example Roy",
                                address_lines=["7 Sample Road, Salt Lake", "Kolkata – 700091, West Bengal"],
                                mobile="9830000002", salutation="Dear Madam,")
    doc.connection = d.Connection(discom="WBSEDCL", consumer_number="100000002", category="Residential", phase="three",
                                  sanctioned_load_kw=Decimal(6))
    doc.site = d.Site(address_lines=list(doc.addressee.address_lines), town="Kolkata", state="West Bengal")
    doc.plant = d.Plant(capacity_kwp=Decimal(5), package="NOVA_ASSURED", dcr=False)
    doc.price = d.StudioPrice(mode="price_list")
    doc.display = d.Display(show_package_comparison=True)
    doc.cover_highlight = "Nova Assured package · Mono PERC modules"
    return _settle(doc)


def commercial_52kw() -> d.StudioDocument:
    doc = d.new_studio_document("commercial_rooftop")
    doc.addressee = _org("Example Metal Works", ["1, Example Road", "Kolkata – 700006, West Bengal"],
                         attention="Mr. Test Director (Director)")
    doc.connection = d.Connection(category="Commercial", phase="three", sanctioned_load_kw=Decimal(60))
    doc.site = d.Site(address_lines=["Example Mouza, Example Village", "P.O. – Example, Amta",
                                     "Dist. – Howrah – 711401, West Bengal"], town="Amta, Howrah", state="West Bengal")
    doc.plant = d.Plant(capacity_kwp=Decimal(52))
    doc.price = d.StudioPrice(mode="staff_basic", basic=Decimal("2860000"))
    return _settle(doc)


def housing_society_60kw() -> d.StudioDocument:
    doc = d.new_studio_document("housing_society")
    doc.project.houses = 24
    doc.addressee = _org("Example Greens Co-operative Housing Society Ltd.",
                         ["Block A, Example Greens, New Town", "Kolkata – 700156, West Bengal"],
                         attention="The Secretary")
    doc.connection = d.Connection(discom="WBSEDCL", category="Residential", phase="three", sanctioned_load_kw=Decimal(80))
    doc.site = d.Site(address_lines=list(doc.addressee.address_lines), town="New Town", state="West Bengal")
    doc.pm_surya_ghar = d.PmSuryaGhar(subsidy_opted=True)
    doc.plant = d.Plant(capacity_kwp=Decimal(60), dcr=True)
    doc.price = d.StudioPrice(mode="staff_basic", rate_per_kwp=Decimal("52000"))
    return _settle(doc)


def industrial_250kw() -> d.StudioDocument:
    doc = d.new_studio_document("industrial_rooftop")
    doc.addressee = _org("Example Steel Castings Pvt. Ltd.", ["Plot 9, Example Industrial Estate",
                                                              "Howrah – 711411, West Bengal"],
                         attention="Mr. Sample Mehta (Plant Head)")
    doc.connection = d.Connection(discom="WBSEDCL", category="Industrial", phase="three", sanctioned_load_kw=Decimal(300))
    doc.site = d.Site(address_lines=list(doc.addressee.address_lines), town="Howrah", state="West Bengal")
    doc.plant = d.Plant(capacity_kwp=Decimal(250))
    doc.price = d.StudioPrice(mode="staff_basic", basic=Decimal("12500000"),
                              discount=d.Discount(amount=Decimal("500000"), reason="Order placed this quarter"))
    doc.savings = d.SavingsInputs(monthly_units=Decimal(40000), tariff_per_unit=Decimal("8.50"))
    doc.display = d.Display(show_discount_line=True, show_savings=True)
    return _settle(doc)


def ground_mounted_1mw() -> d.StudioDocument:
    doc = d.new_studio_document("ground_mounted")
    doc.addressee = _org("Example Agro Processing Ltd.", ["Example Mouza, Example Block", "Bankura – 722101, West Bengal"])
    doc.connection = d.Connection(category="Commercial", phase="three")
    doc.site = d.Site(address_lines=list(doc.addressee.address_lines), town="Bankura", state="West Bengal")
    doc.plant = d.Plant(capacity_kwp=Decimal(1000))
    doc.price = d.StudioPrice(mode="staff_basic", rate_per_kwp=Decimal("38000"))
    return _settle(doc)


def street_lighting_40() -> d.StudioDocument:
    doc = d.new_studio_document("street_lighting")
    doc.addressee = _org("Example Gram Panchayat", ["Example Village, Example Block", "Nadia – 741101, West Bengal"],
                         attention="The Pradhan")
    doc.site = d.Site(address_lines=["Example Village Main Road"], town="Nadia", state="West Bengal")
    doc.items = [
        d.ItemRow(description="Solar street light, all-in-one", specification="30 W LED, LiFePO4 battery",
                  make="Branded", quantity=Decimal(40), unit="Nos", unit_rate=Decimal("18500"),
                  category="solar_street_light"),
        d.ItemRow(description="GI pole with foundation", specification="6 m, hot-dip galvanised", make="Branded",
                  quantity=Decimal(40), unit="Nos", unit_rate=Decimal("6200"), category="solar_street_light"),
        d.ItemRow(description="Installation, testing & commissioning", make="–", quantity=Decimal(1), unit="Lot",
                  unit_rate=Decimal("60000"), category="installation_service"),
    ]
    doc.customer_scope = ["Provide the pole locations and clear access to each location.",
                          "Provide safe storage for the material on receipt at site."]
    return _settle(doc)


def _settle(doc: d.StudioDocument) -> d.StudioDocument:
    doc = d.StudioDocument.model_validate(d.dump(doc))
    d.requote(doc)
    d.refresh_defaults(doc)
    return d.StudioDocument.model_validate(d.dump(doc))


SAMPLES = {
    "residential_3kw_pmsg": residential_3kw_pmsg,
    "commercial_52kw": commercial_52kw,
    "housing_society_60kw_24_houses": housing_society_60kw,
    "industrial_250kw_savings_discount": industrial_250kw,
    "ground_mounted_1mwp": ground_mounted_1mw,
    "street_lighting_40_itemised": street_lighting_40,
    "residential_5kw_package_comparison": residential_5kw_comparison,
}


# ── API steps ────────────────────────────────────────────────────────────────────

CUSTOMER = {"customer_type": "individual", "title": "Mr.", "name": "Test Kumar Das", "mobile": "9830000001",
            "email": "test.customer@example.in",
            "address_lines": ["12 Example Lane, Habra", "North 24 Parganas, West Bengal – 743263"]}


def new_customer(client, headers=DESK, **over) -> dict:
    r = client.post(f"{D}/studio/customers", headers=headers, json={**CUSTOMER, **over})
    assert r.status_code == 201, r.text
    return r.json()["customer"]


def new_proposal(client, project_type="residential_rooftop", headers=DESK, customer_id=None) -> dict:
    cid = customer_id or new_customer(client, headers)["id"]
    r = client.post(f"{D}/studio/proposals", headers=headers, json={"customer_id": cid, "project_type": project_type})
    assert r.status_code == 201, r.text
    return r.json()


def edit(client, pid, section, value, headers=DESK, base_revision=None):
    if base_revision is None:
        base_revision = client.get(f"{D}/studio/proposals/{pid}", headers=headers).json()["revision"]
    return client.patch(f"{D}/studio/proposals/{pid}/document", headers=headers,
                        json={"base_revision": base_revision, "section": section, "value": value})


def ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


RESIDENTIAL_3KW = [
    ("site", {"address_lines": ["12 Example Lane, Habra", "North 24 Parganas, West Bengal – 743263"],
              "town": "Habra", "state": "West Bengal"}),
    ("connection", {"discom": "WBSEDCL", "consumer_number": "100000001", "category": "Residential",
                    "phase": "single", "sanctioned_load_kw": "4"}),
    ("plant", {"capacity_kwp": "3", "package": "NOVA_ASSURED", "dcr": True}),
    ("pm_surya_ghar", {"subsidy_opted": True}),
    ("price", {"mode": "price_list"}),
]


def fill(client, pid, steps=RESIDENTIAL_3KW, headers=DESK) -> dict:
    view = None
    for section, value in steps:
        view = ok(edit(client, pid, section, value, headers=headers))
    return view


def acknowledge_all(client, pid, headers=DESK) -> dict:
    view = client.get(f"{D}/studio/proposals/{pid}", headers=headers).json()
    if view["warnings_unacknowledged"]:
        view = ok(client.post(f"{D}/studio/proposals/{pid}/acknowledge-warnings", headers=headers,
                              json={"fingerprints": view["warnings_unacknowledged"]}))
    return view


def issued_residential(client, headers=DESK) -> tuple[dict, str]:
    """A 3 kWp PM Surya Ghar proposal, issued. Needs fake_storage and a Fernet key."""
    view = new_proposal(client, headers=headers)
    pid = view["proposal"]["id"]
    fill(client, pid, headers=headers)
    acknowledge_all(client, pid, headers=headers)
    out = ok(client.post(f"{D}/studio/proposals/{pid}/issue", headers=headers))
    return out, out["public_url"].rsplit("/", 1)[1]
