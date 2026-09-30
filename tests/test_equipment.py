"""Tests for the Equipment/SKU Master API (/api/v1/equipment).

Covers listing (any authenticated employee, filterable), creation and editing
(Admin-only), and the "never hard-delete" discipline — deactivation is the
only way to retire an entry.
"""
from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


def _valid_payload(**overrides) -> dict:
    payload = {
        "category": "module",
        "brand": "Waaree",
        "model_name": "Vertex-540",
        "specification": "540W monocrystalline, 21.5% efficiency",
        "is_dcr_compliant": True,
        "procurement_price_inr": 12500.0,
    }
    payload.update(overrides)
    return payload


class TestProcurementPriceVisibility:
    """Matches the existing proposal-pricing precedent (dashboard.py's
    `_proposal_summary`): Admin, Sales and Management see commercial figures;
    Engineer does not. The field must be genuinely ABSENT for a role that
    can't see it, not present-with-null, so the frontend can tell "not
    permitted" apart from "no price set yet"."""

    def test_admin_sees_the_real_price(self, client):
        client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        r = client.get("/api/v1/equipment/", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["items"][0]["procurement_price_inr"] == 12500.0

    def test_sales_and_management_also_see_it(self, client):
        client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        for headers in (sales_headers(), management_headers()):
            r = client.get("/api/v1/equipment/", headers=headers)
            assert "procurement_price_inr" in r.json()["items"][0]

    def test_engineer_response_omits_the_field_entirely(self, client):
        client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        r = client.get("/api/v1/equipment/", headers=engineer_headers())
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "procurement_price_inr" not in item
        # Not just missing from this assertion — genuinely not a key at all.
        assert set(item.keys()) == {
            "id", "category", "brand", "model_name", "specification",
            "is_dcr_compliant", "is_active", "is_placeholder", "created_at",
        }

    def test_omitted_when_no_price_is_set_too_not_just_when_one_exists(self, client):
        # Confirms this is a permission omission, not just "happens to be
        # null anyway" — the field is absent for Engineer even when a price
        # genuinely was never entered.
        client.post(
            "/api/v1/equipment/",
            json=_valid_payload(procurement_price_inr=None),
            headers=admin_headers(),
        )
        admin_view = client.get("/api/v1/equipment/", headers=admin_headers()).json()["items"][0]
        engineer_view = client.get("/api/v1/equipment/", headers=engineer_headers()).json()["items"][0]
        assert admin_view["procurement_price_inr"] is None  # present, genuinely null
        assert "procurement_price_inr" not in engineer_view  # absent, not permitted


class TestListEquipment:
    def test_empty_list_returns_200(self, client):
        r = client.get("/api/v1/equipment/", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_any_authenticated_role_can_read(self, client):
        for headers in (admin_headers(), sales_headers(), engineer_headers(), management_headers()):
            r = client.get("/api/v1/equipment/", headers=headers)
            assert r.status_code == 200

    def test_requires_auth(self, client):
        r = client.get("/api/v1/equipment/")
        assert r.status_code == 401

    def test_lists_created_items(self, client):
        client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        r = client.get("/api/v1/equipment/", headers=sales_headers())
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["brand"] == "Waaree"
        assert items[0]["is_active"] is True

    def test_filter_by_category(self, client):
        client.post("/api/v1/equipment/", json=_valid_payload(category="module"), headers=admin_headers())
        client.post(
            "/api/v1/equipment/",
            json=_valid_payload(category="inverter", brand="Growatt", model_name="MIN-5000"),
            headers=admin_headers(),
        )
        r = client.get("/api/v1/equipment/", params={"category": "inverter"}, headers=sales_headers())
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["brand"] == "Growatt"

    def test_invalid_category_422(self, client):
        r = client.get("/api/v1/equipment/", params={"category": "not_a_category"}, headers=admin_headers())
        assert r.status_code == 422

    def test_filter_by_is_active(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": False}, headers=admin_headers())

        r_active = client.get("/api/v1/equipment/", params={"is_active": True}, headers=admin_headers())
        r_inactive = client.get("/api/v1/equipment/", params={"is_active": False}, headers=admin_headers())
        assert r_active.json()["items"] == []
        assert len(r_inactive.json()["items"]) == 1


class TestCreateEquipment:
    def test_admin_can_create(self, client):
        r = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["category"] == "module"
        assert body["brand"] == "Waaree"
        assert body["model_name"] == "Vertex-540"
        assert body["is_dcr_compliant"] is True
        assert body["procurement_price_inr"] == 12500.0
        assert body["is_active"] is True
        assert body["id"] is not None

    def test_non_admin_forbidden(self, client):
        for headers in (sales_headers(), engineer_headers(), management_headers()):
            r = client.post("/api/v1/equipment/", json=_valid_payload(), headers=headers)
            assert r.status_code == 403

    def test_defaults_when_optional_fields_omitted(self, client):
        r = client.post(
            "/api/v1/equipment/",
            json={"category": "bos_component", "brand": "Generic", "model_name": "MC4 Connector"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["is_dcr_compliant"] is False
        assert body["procurement_price_inr"] is None
        assert body["specification"] is None

    def test_missing_required_field_422(self, client):
        r = client.post(
            "/api/v1/equipment/", json={"category": "module", "brand": "Waaree"}, headers=admin_headers(),
        )
        assert r.status_code == 422

    def test_invalid_category_422(self, client):
        r = client.post(
            "/api/v1/equipment/",
            json=_valid_payload(category="not_a_real_category"),
            headers=admin_headers(),
        )
        assert r.status_code == 422

    def test_negative_price_rejected(self, client):
        r = client.post(
            "/api/v1/equipment/", json=_valid_payload(procurement_price_inr=-100), headers=admin_headers(),
        )
        assert r.status_code == 422

    def test_creation_is_audited(self, client, memory):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        logs = memory.list_audit_logs(entity_type="equipment", entity_id=str(item_id))
        assert any(l.action == "created" for l in logs)


class TestUpdateEquipment:
    def test_admin_can_edit_fields(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]

        r = client.patch(
            f"/api/v1/equipment/{item_id}",
            json={"procurement_price_inr": 13000.0, "specification": "Updated spec"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert r.json()["procurement_price_inr"] == 13000.0
        assert r.json()["specification"] == "Updated spec"
        assert r.json()["brand"] == "Waaree"  # untouched fields survive

    def test_deactivate_does_not_delete_the_row(self, client, memory):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]

        r = client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": False}, headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["is_active"] is False

        # Still present and fetchable — never hard-deleted.
        still_there = memory.get_equipment(item_id)
        assert still_there is not None
        assert still_there.brand == "Waaree"

    def test_reactivate(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": False}, headers=admin_headers())

        r = client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": True}, headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["is_active"] is True

    def test_non_admin_forbidden(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        for headers in (sales_headers(), engineer_headers(), management_headers()):
            r = client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": False}, headers=headers)
            assert r.status_code == 403

    def test_nonexistent_returns_404(self, client):
        r = client.patch("/api/v1/equipment/999999", json={"is_active": False}, headers=admin_headers())
        assert r.status_code == 404

    def test_empty_update_rejected(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        r = client.patch(f"/api/v1/equipment/{item_id}", json={}, headers=admin_headers())
        assert r.status_code == 422

    def test_update_is_audited(self, client, memory):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        client.patch(f"/api/v1/equipment/{item_id}", json={"is_active": False}, headers=admin_headers())
        logs = memory.list_audit_logs(entity_type="equipment", entity_id=str(item_id))
        assert any(l.action == "updated" for l in logs)


class TestNoHardDeletePath:
    def test_no_delete_route_exists(self, client):
        create = client.post("/api/v1/equipment/", json=_valid_payload(), headers=admin_headers())
        item_id = create.json()["id"]
        r = client.delete(f"/api/v1/equipment/{item_id}", headers=admin_headers())
        assert r.status_code == 405  # method not allowed — no DELETE route defined at all
