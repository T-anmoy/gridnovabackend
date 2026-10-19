"""BE-8 — reading the archive: filters, paging, statistics, and what each role sees."""
import pytest

from tests.archive_support import ADMIN, BASE, ROLES, import_rows, row, who
from tests.archive_support import territories  # noqa: F401 — fixture

FULL = dict(
    customer_name="Asha Roy", customer_phone="9800000000", customer_email="asha@example.test", discom="WBSEDCL",
    consumer_number="123456789", contract_value_inr="250000", subsidy_amount_inr="78000", block="Alipurduar - I",
    address_text="12 Station Road", pincode="736121", latitude="26.49", longitude="89.52", module_make="Waaree",
    module_model="WS-540", module_wattage_wp="540", module_count="6", module_dcr="yes", inverter_make="Growatt",
    inverter_model="MIN-3000", inverter_capacity_kw="3", inverter_count="1", structure_type="GI", battery_details="none",
    other_equipment="ACDB", order_date="01/01/2025", installation_date="01/03/2025", net_meter_date="20/03/2025",
    warranty_end_modules="15/03/2050", warranty_end_inverter="15/03/2035", om_status="AMC active",
    notes="Roof was leaking", documents="Report|https://example.test/r.pdf",
)
FIELD_GROUPS = {
    "customer_contact": ["customer_phone", "customer_email", "consumer_number"],
    "money": ["contract_value_inr", "subsidy_amount_inr", "amount_received_inr", "amount_outstanding_inr"],
    "location_detail": ["block", "address_text", "pincode", "latitude", "longitude"],
    "customer_identity": ["customer_name", "discom"],
    "service": ["om_status", "notes", "documents", "data_quality_flags", "open_service_requests"],
    "import_meta": ["import_batch_id", "source_row"],
}
# role -> groups it must NOT see (see config/archive.yaml)
HIDDEN = {
    "admin": [], "management": [],
    "accounts": ["customer_contact", "import_meta"],
    "operations": ["customer_contact", "money", "import_meta"],
    "engineer": ["customer_contact", "money", "import_meta"],
    "sales": ["customer_contact", "money", "location_detail", "customer_identity", "service", "import_meta"],
}
ALWAYS = ["scheme_or_segment", "capacity_kw", "state", "district", "commissioning_date", "module_make", "inverter_make"]


@pytest.fixture
def archive(client, territories):
    import_rows(client, [
        row(grid_nova_ref="GN-1", capacity_kw="3", commissioning_date="15/03/2025", **FULL),
        row(grid_nova_ref="GN-2", capacity_kw="10", commissioning_date="10/06/2024", district="Kolkata",
            module_make="Adani", inverter_make="Sungrow", scheme_or_segment="commercial"),
        row(grid_nova_ref="GN-3", capacity_kw="", pump_hp="5", scheme_or_segment="pm_kusum_pump",
            commissioning_date="01/12/2023", state="Bihar", district="Araria"),
        row(grid_nova_ref="GN-4", capacity_kw="100", commissioning_date="05/05/2025", scheme_or_segment="industrial",
            district="Kolkata"),
    ])


def listing(client, role="admin", **params):
    r = client.get(f"{BASE}/projects", params=params, headers=who(role))
    assert r.status_code == 200, r.text
    return r.json()


def refs(body) -> list:
    return [i["ref"] for i in body["items"]]


class TestFilters:
    def test_newest_first_and_everything_by_default(self, client, archive):
        body = listing(client)
        assert refs(body) == ["GN-4", "GN-1", "GN-2", "GN-3"] and body["total"] == 4

    @pytest.mark.parametrize("params, expected", [
        ({"state": "west bengal"}, {"GN-1", "GN-2", "GN-4"}), ({"state": "Bihar"}, {"GN-3"}),
        ({"district": "kolkata"}, {"GN-2", "GN-4"}), ({"scheme": "commercial"}, {"GN-2"}),
        ({"capacity_min": "10"}, {"GN-2", "GN-4"}), ({"capacity_max": "3"}, {"GN-1"}),
        ({"capacity_min": "3", "capacity_max": "10"}, {"GN-1", "GN-2"}),
        ({"commissioned_from": "2025-01-01"}, {"GN-1", "GN-4"}), ({"commissioned_to": "2024-12-31"}, {"GN-2", "GN-3"}),
        ({"commissioned_from": "2024-06-10", "commissioned_to": "2025-03-15"}, {"GN-1", "GN-2"}),
        ({"module_make": "waar"}, {"GN-1"}), ({"inverter_make": "SUNGROW"}, {"GN-2"}),
        ({"q": "gn-3"}, {"GN-3"}), ({"q": "asha"}, {"GN-1"}), ({"q": "station road"}, {"GN-1"}),
        ({"state": "west bengal", "scheme": "industrial"}, {"GN-4"}), ({"state": "nowhere"}, set()),
    ])
    def test_filters(self, client, archive, params, expected):
        body = listing(client, **params)
        assert set(refs(body)) == expected and body["total"] == len(expected)

    def test_a_like_wildcard_in_q_is_literal(self, client, archive):
        assert listing(client, q="%")["total"] == 0 and listing(client, q="_")["total"] == 0

    def test_paging(self, client, archive):
        first = listing(client, page=1, page_size=3)
        second = listing(client, page=2, page_size=3)
        assert (len(first["items"]), first["total"], first["page"], first["page_size"]) == (3, 4, 1, 3)
        assert refs(second) == ["GN-3"] and listing(client, page=9, page_size=3)["items"] == []

    @pytest.mark.parametrize("params, code, field", [
        ({"scheme": "wind"}, "unknown_scheme", "scheme"), ({"capacity_min": "x"}, "invalid_number", "capacity_min"),
        ({"capacity_max": "-1"}, "invalid_number", "capacity_max"),
        ({"capacity_min": "9", "capacity_max": "1"}, "invalid_range", "capacity_min"),
        ({"commissioned_from": "03/2025"}, "invalid_date", "commissioned_from"),
        ({"commissioned_to": "nope"}, "invalid_date", "commissioned_to"),
        ({"commissioned_from": "2025-02-01", "commissioned_to": "2025-01-01"}, "invalid_range", "commissioned_from"),
    ])
    def test_structured_422(self, client, archive, params, code, field):
        r = client.get(f"{BASE}/projects", params=params, headers=ADMIN)
        assert r.status_code == 422
        assert r.json()["detail"] == {"code": code, "message": r.json()["detail"]["message"], "field": field}

    def test_page_bounds_are_422(self, client, archive):
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
            assert client.get(f"{BASE}/projects", params=params, headers=ADMIN).status_code == 422

    def test_stats_take_the_same_filters(self, client, archive):
        assert client.get(f"{BASE}/stats", params={"scheme": "wind"}, headers=ADMIN).status_code == 422
        assert client.get(f"{BASE}/stats", params={"state": "bihar"}, headers=ADMIN).json()["total"]["count"] == 1


class TestStats:
    def test_counts_and_capacity_sums(self, client, archive):
        body = client.get(f"{BASE}/stats", headers=ADMIN).json()
        assert body["total"] == {"count": 4, "capacity_kw": "113.000"}
        schemes = {s["scheme"]: (s["count"], s["capacity_kw"]) for s in body["by_scheme"]}
        assert schemes["pm_kusum_pump"] == (1, "0.000") and schemes["industrial"] == (1, "100.000")
        # Matched places are named as the territory master names them (LGD style).
        assert {s["state"].lower(): s["count"] for s in body["by_state"]} == {"west bengal": 3, "bihar": 1}
        assert {(d["state"].lower(), d["district"].lower()): d["count"] for d in body["by_district"]} == {
            ("west bengal", "alipurduar"): 1, ("west bengal", "kolkata"): 2, ("bihar", "araria"): 1}
        assert [(y["year"], y["count"]) for y in body["by_year"]] == [(2023, 1), (2024, 1), (2025, 2)]

    def test_empty_archive(self, client):
        body = client.get(f"{BASE}/stats", headers=ADMIN).json()
        assert body["total"] == {"count": 0, "capacity_kw": "0.000"} and body["by_scheme"] == []


class TestRoleVisibility:
    @pytest.mark.parametrize("role", ROLES)
    def test_dossier_fields_per_role(self, client, archive, role):
        pid = next(i["id"] for i in listing(client)["items"] if i["ref"] == "GN-1")
        r = client.get(f"{BASE}/projects/{pid}", headers=who(role))
        assert r.status_code == 200, r.text
        fields = r.json()["fields"]
        for name in ALWAYS:
            assert name in fields, (role, name)
        for group in FIELD_GROUPS:
            for name in FIELD_GROUPS[group]:
                if group in HIDDEN[role]:
                    assert name not in fields, f"{role} sees {name}"
                elif name in ("amount_received_inr", "amount_outstanding_inr", "open_service_requests"):
                    continue   # live-only
                else:
                    assert name in fields, f"{role} lost {name}"

    @pytest.mark.parametrize("role", ROLES)
    def test_forbidden_values_appear_nowhere_in_the_response(self, client, archive, role):
        pid = next(i["id"] for i in listing(client)["items"] if i["ref"] == "GN-1")
        text = client.get(f"{BASE}/projects/{pid}", headers=who(role)).text + str(listing(client, role)["items"])
        secrets = {"customer_contact": ["9800000000", "asha@example.test", "123456789"],
                   "money": ["250000", "78000"], "location_detail": ["Station Road", "736121"],
                   "customer_identity": ["Asha Roy", "WBSEDCL"], "service": ["Roof was leaking", "AMC active"]}
        for group in HIDDEN[role]:
            for secret in secrets.get(group, []):
                assert secret not in text, f"{role}: {secret} leaked"

    @pytest.mark.parametrize("role", ROLES)
    def test_sections_per_role(self, client, archive, role):
        pid = next(i["id"] for i in listing(client)["items"] if i["ref"] == "GN-1")
        sections = client.get(f"{BASE}/projects/{pid}", headers=who(role)).json()["sections"]
        assert ("equipment" in sections) and ("stages" in sections) and ("warranty" in sections)
        assert ("documents" in sections) == ("service" not in HIDDEN[role])
        assert ("service_requests" in sections) == ("service" not in HIDDEN[role])

    @pytest.mark.parametrize("role", ROLES)
    def test_cards_per_role(self, client, archive, role):
        card = listing(client, role, q="GN-1")["items"][0]
        assert "customer_name" in card["fields"] if "customer_identity" not in HIDDEN[role] else \
            "customer_name" not in card["fields"]
        assert set(card["fields"]) <= {"scheme_or_segment", "system_type", "capacity_kw", "pump_hp", "street_lamp_count",
                                       "state", "district", "block", "commissioning_date", "module_make",
                                       "inverter_make", "customer_name"}
        if "location_detail" in HIDDEN[role]:
            assert "block" not in card["fields"]

    def test_search_cannot_reveal_a_hidden_value(self, client, archive):
        for term in ("asha", "station road", "alipurduar - i"):
            assert listing(client, "sales", q=term)["total"] == 0, term
            assert listing(client, "admin", q=term)["total"] == 1, term
        assert listing(client, "sales", q="GN-1")["total"] == 1       # the Grid Nova ref is not personal
        assert listing(client, "sales", q="kolkata")["total"] == 2    # district-level location is allowed
        assert listing(client, "operations", q="asha")["total"] == 1  # a customer name is not contact data

    def test_provenance_on_every_field(self, client, archive):
        pid = next(i["id"] for i in listing(client)["items"] if i["ref"] == "GN-1")
        body = client.get(f"{BASE}/projects/{pid}", headers=ADMIN).json()
        assert body["source"] == "archive" and body["ref"] == "GN-1" and body["id"] == pid
        for name, cell in body["fields"].items():
            assert set(cell) == {"value", "source_table", "record_id"}, name
            assert cell["source_table"] == "archived_projects" and cell["record_id"] == pid
        assert body["fields"]["contract_value_inr"]["value"] == "250000.00"
        assert body["fields"]["commissioning_date"]["value"] == "2025-03-15"
        assert body["fields"]["capacity_kw"]["value"] == "3.000"

    def test_ref_falls_back_to_arc_id(self, client):
        import_rows(client, [row(grid_nova_ref="")])
        item = listing(client)["items"][0]
        assert item["ref"] == f"ARC-{item['id']}"

    def test_unknown_project_is_a_structured_404(self, client, archive):
        r = client.get(f"{BASE}/projects/99999", headers=ADMIN)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "project_not_found"
