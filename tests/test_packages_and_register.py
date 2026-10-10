"""PM-A Step 6 — GET /api/v1/pricing/packages, and the serial register's approved-make
check (catalogue from the nameplate, the staff-only make_not_in_package flag)."""
import json

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, OPS, SALES, associate_in_execution, exec_url, headers, kyc_ready, onboarding_ready, staff_exec_url,
    territories,
)


class TestPackagesEndpoint:
    def test_public_shape(self, client):
        r = client.get("/api/v1/pricing/packages")
        assert r.status_code == 200
        body = r.json()
        assert body["price_list_version"] == 1 and body["destination"] == "West Bengal"
        assert body["phase_ranges"] == {"SINGLE": {"min_kw": 1, "max_kw": 10}, "THREE": {"min_kw": 3, "max_kw": 10}}
        packages = {p["package"]: p for p in body["packages"]}
        assert packages["NOVA_ASSURED"]["display_name"] == "Nova Assured"
        assert packages["NOVA_ASSURED"]["equipment_tier"] == "standard"
        assert packages["NOVA_SIGNATURE"]["equipment"]["solar_panel"] == {
            "makes": ["TATA", "Waaree", "Adani", "Vikram"], "or_equivalent": True, "technology": ["Mono PERC", "TOPCon"]}
        assert [m["label"] for m in body["terms"]["payment_schedule"]] == [
            "On Order", "Against Material Bill", "Before Commissioning"]
        assert body["terms"]["standard_warranty"] == "5 years from handover"
        assert "models" not in json.dumps(body) and "dcr" not in json.dumps(body).lower()

    def test_price_table_follows_the_display_policy(self, client, monkeypatch):
        body = client.get("/api/v1/pricing/packages").json()
        assert body["price_display"] == "exact" and len(body["prices"]) == 36
        row = next(p for p in body["prices"] if (p["package"], p["phase"], p["kw"]) == ("NOVA_SIGNATURE", "THREE", 10))
        assert row["final"] == "744004.80"
        assert not any(p["phase"] == "THREE" and p["kw"] < 3 for p in body["prices"])
        from app.pricing import policy
        monkeypatch.setitem(policy.load(), "public_price_display", "hidden")
        from app.api.routes import pricing
        pricing._packages_body.cache_clear()  # built once per process from the config (BE-1 S10)
        hidden = client.get("/api/v1/pricing/packages").json()
        assert "prices" not in hidden and hidden["price_display"] == "hidden"


def _catalogue(client, who, oid, **body):
    data = {"category": "module", "brand": "Sova Solar", "model_name": "SS-540M", "is_dcr_compliant": True, **body}
    return client.post(f"{staff_exec_url(oid)}/catalogue", json=data, headers=who)


class TestCatalogueFromNameplate:
    def test_approved_make(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        r = _catalogue(client, OPS, oid)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["package"] == "NOVA_ASSURED" and body["in_package"] is True
        assert body["equipment"]["model_name"] == "SS-540M" and body["equipment"]["is_dcr_compliant"] is True

    def test_other_make_needs_an_equivalent_reason(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        r = _catalogue(client, OPS, oid, brand="Waaree")          # a Signature make, not Assured
        assert r.status_code == 422 and r.json()["detail"]["code"] == "make_not_approved"
        assert r.json()["detail"]["approved_makes"] == ["Sova Solar", "HR Solar", "V-Sole"]
        r = _catalogue(client, ADMIN, oid, brand="Waaree", equivalent_reason="Sova out of stock; same rating.")
        assert r.status_code == 201 and r.json()["in_package"] is False
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "catalogued_from_nameplate")).scalar_one()
        assert audit.after_value["equivalent_reason"] == "Sova out of stock; same rating."

    def test_unknown_package_needs_a_reason(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session)
        assert _catalogue(client, OPS, oid).status_code == 422
        assert _catalogue(client, OPS, oid, equivalent_reason="Package to be confirmed.").status_code == 201

    def test_component_without_a_supplied_list_is_not_checked(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        r = _catalogue(client, OPS, oid, category="structure", brand="Any Fab", model_name="GI-1")
        assert r.status_code == 201 and r.json()["in_package"] is None

    @pytest.mark.parametrize("who", [SALES, {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}])
    def test_roles(self, client, db_session, onboarding_ready, who):
        _, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        assert _catalogue(client, who, oid).status_code == 403


class TestMakeNotInPackageFlag:
    def test_staff_only_non_blocking(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        eq = _catalogue(client, OPS, oid, brand="Waaree", equivalent_reason="Equivalent rating.").json()["equipment"]
        r = client.post(f"{staff_exec_url(oid)}/units", headers=OPS, json={
            "category": "module", "equipment_id": eq["id"], "serial_number": "WAA-0001", "rating_wp": "540",
            "is_dcr": True})
        assert r.status_code == 201, r.text
        assert r.json()["unit"]["make_not_in_package"] is True
        staff = client.get(f"{staff_exec_url(oid)}/units", headers=OPS).json()
        assert "make_not_in_package" in staff["flags"] and staff["units"][0]["make_not_in_package"] is True
        detail = client.get(staff_exec_url(oid), headers=OPS).json()
        assert "make_not_in_package" in json.dumps(detail)
        associate = client.get(exec_url(oid), headers=headers(gna)).json()
        assert "make_not_in_package" not in json.dumps(associate)

    def test_approved_make_is_not_flagged(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        eq = _catalogue(client, OPS, oid, brand="sova solar").json()["equipment"]    # case-insensitive
        r = client.post(f"{staff_exec_url(oid)}/units", headers=OPS, json={
            "category": "module", "equipment_id": eq["id"], "serial_number": "SOV-0001", "rating_wp": "540",
            "is_dcr": True})
        assert r.json()["unit"]["make_not_in_package"] is False
