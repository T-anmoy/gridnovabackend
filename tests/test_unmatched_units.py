"""HF-C — associates read the equipment catalogue (no prices, ever), register units
whose model is not in the catalogue yet ("unmatched", brand/model as printed), and
Operations/Admin match them before the register can be verified. Also: customer name on
the staff payment lists."""
import json

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, Equipment, EquipmentCategory, InstalledUnit
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, OPS, SALES, associate_in_execution, exec_url, headers, kyc_ready, make_associate, notifications,
    onboarding_ready, staff_exec_url, territories,
)

CATALOGUE = "/api/v1/associate/equipment"


def _equipment(db, **kw):
    data = {"category": EquipmentCategory.module, "brand": "Sova Solar", "model_name": "SS-540M",
            "is_dcr_compliant": True, "is_active": True, "procurement_price_inr": 12345.0, **kw}
    row = Equipment(**data)
    db.add(row)
    db.commit()
    return row


UNMATCHED_MODULE = {"category": "module", "serial_number": "UNM-0001", "rating_wp": "540", "is_dcr": True,
                    "brand_as_printed": "Sova Solar", "model_as_printed": "SS-545M Bifacial"}


class TestAssociateCatalogue:
    def test_shape_never_has_a_price(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        _equipment(db_session)
        body = client.get(CATALOGUE, headers=headers(gna)).json()
        [item] = body["items"]
        assert set(item) == {"id", "category", "brand", "model_name", "specification", "is_dcr_compliant",
                             "is_placeholder"}
        assert "procurement" not in json.dumps(body) and "12345" not in json.dumps(body)
        assert "price" not in json.dumps(body)

    def test_filters_and_active_only(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        _equipment(db_session)
        _equipment(db_session, category=EquipmentCategory.inverter, brand="Deye", model_name="SUN-5K",
                   is_dcr_compliant=False)
        _equipment(db_session, brand="Retired", model_name="OLD-1", is_active=False)
        get = lambda **p: [i["model_name"] for i in client.get(CATALOGUE, params=p, headers=headers(gna)).json()["items"]]
        assert get() == ["SUN-5K", "SS-540M"] or set(get()) == {"SUN-5K", "SS-540M"}
        assert get(category="inverter") == ["SUN-5K"]
        assert get(q="sova") == ["SS-540M"] and get(q="sun-5") == ["SUN-5K"]
        assert client.get(CATALOGUE, params={"category": "spaceship"}, headers=headers(gna)).status_code == 422

    def test_placeholder_rows_only_outside_production(self, client, db_session, onboarding_ready, monkeypatch):
        from types import SimpleNamespace
        import app.api.routes.associate_equipment as mod
        gna = make_associate(db_session)
        _equipment(db_session, brand="DUMMY", model_name="DUMMY-M540", is_placeholder=True)
        assert client.get(CATALOGUE, headers=headers(gna)).json()["items"][0]["is_placeholder"] is True
        # Only the route's view of the environment changes (auth keeps its dev headers).
        monkeypatch.setattr(mod, "settings", SimpleNamespace(is_production=True, is_production_like=True))
        assert client.get(CATALOGUE, headers=headers(gna)).json()["items"] == []

    def test_auth(self, client, db_session, onboarding_ready):
        assert client.get(CATALOGUE).status_code == 401
        assert client.get(CATALOGUE, headers=ADMIN).status_code == 401          # dev-header mode
        suspended = make_associate(db_session, status=__import__("app.memory.models", fromlist=["AssociateStatus"])
                                   .AssociateStatus.suspended)
        assert client.get(CATALOGUE, headers=headers(suspended)).status_code == 403

    def test_rate_limited(self, client, db_session, onboarding_ready):
        from app.api.routes import associate_equipment
        gna = make_associate(db_session)
        for _ in range(associate_equipment.RATE_LIMIT):
            assert client.get(CATALOGUE, headers=headers(gna)).status_code == 200
        r = client.get(CATALOGUE, headers=headers(gna))
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


class TestUnmatchedUnits:
    def test_associate_registers_an_unmatched_module(self, client, db_session, onboarding_ready, notifications):
        gna, oid = associate_in_execution(client, db_session)
        r = client.post(f"{exec_url(oid)}/units", json=UNMATCHED_MODULE, headers=headers(gna))
        assert r.status_code == 201, r.text
        unit = r.json()["unit"]
        assert unit["equipment"] is None and unit["brand_as_printed"] == "Sova Solar"
        assert unit["model_as_printed"] == "SS-545M Bifacial" and unit["is_dcr"] is True
        assert "catalogue_unmatched" not in json.dumps(r.json())        # associates never see the flag
        [mail] = notifications.of("catalogue_unmatched")
        assert mail["onboarding_id"] == oid and mail["count"] == 1
        associate_view = client.get(exec_url(oid), headers=headers(gna)).json()
        assert "catalogue_unmatched" not in json.dumps(associate_view)

    @pytest.mark.parametrize("drop,field", [("brand_as_printed", "brand_as_printed"),
                                            ("model_as_printed", "model_as_printed")])
    def test_printed_make_is_required_without_a_catalogue_item(self, client, db_session, onboarding_ready, drop, field):
        gna, oid = associate_in_execution(client, db_session)
        body = {k: v for k, v in UNMATCHED_MODULE.items() if k != drop}
        r = client.post(f"{exec_url(oid)}/units", json=body, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "register_rows_invalid"
        [error] = r.json()["detail"]["errors"]
        assert (error["code"], error["field"]) == ("equipment_required", field)

    def test_printed_fields_are_length_limited(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        r = client.post(f"{exec_url(oid)}/units", json={**UNMATCHED_MODULE, "model_as_printed": "x" * 101},
                        headers=headers(gna))
        assert r.status_code == 422

    def test_module_still_needs_a_dcr_declaration(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        body = {k: v for k, v in UNMATCHED_MODULE.items() if k != "is_dcr"}
        r = client.post(f"{exec_url(oid)}/units", json=body, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == "dcr_required"

    def test_staff_see_the_flag_and_the_queue_marks_it(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        client.post(f"{exec_url(oid)}/units", json=UNMATCHED_MODULE, headers=headers(gna))
        reg = client.get(f"{staff_exec_url(oid)}/units", headers=OPS).json()
        assert reg["units"][0]["catalogue_unmatched"] is True and "catalogue_unmatched" in reg["flags"]
        queue = client.get("/api/v1/dashboard/execution", params={"needs_attention": True}, headers=OPS).json()
        row = next(i for i in queue["items"] if i["onboarding_id"] == oid)
        assert "catalogue_unmatched" in row["attention_reasons"]

    def test_serial_uniqueness_is_unchanged(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        client.post(f"{exec_url(oid)}/units", json=UNMATCHED_MODULE, headers=headers(gna))
        gna2, oid2 = associate_in_execution(client, db_session)
        r = client.post(f"{exec_url(oid2)}/units", json=UNMATCHED_MODULE, headers=headers(gna2))
        assert r.status_code == 409


class TestMatchAndVerify:
    def _setup(self, client, db_session):
        gna, oid = associate_in_execution(client, db_session)
        uid = client.post(f"{exec_url(oid)}/units", json=UNMATCHED_MODULE, headers=headers(gna)).json()["unit"]["unit_id"]
        inverter = _equipment(db_session, category=EquipmentCategory.inverter, brand="Deye", model_name="SUN-5K",
                              is_dcr_compliant=False)
        r = client.post(f"{exec_url(oid)}/units", headers=headers(gna), json={
            "category": "inverter", "equipment_id": inverter.id, "serial_number": "INV-0001", "rating_kw": "5"})
        assert r.status_code == 201, r.text
        return gna, oid, uid, inverter

    def test_register_verify_refused_while_unmatched(self, client, db_session, onboarding_ready):
        gna, oid, uid, _ = self._setup(client, db_session)
        r = client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "units_unmatched"
        assert r.json()["detail"]["unit_ids"] == [uid]

    def test_match_rules_then_verify(self, client, db_session, onboarding_ready):
        gna, oid, uid, inverter = self._setup(client, db_session)
        url = f"{staff_exec_url(oid)}/units/{uid}/match"
        r = client.post(url, json={"equipment_id": inverter.id}, headers=OPS)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "equipment_category_mismatch"
        non_dcr = _equipment(db_session, model_name="SS-545M-N", is_dcr_compliant=False)
        r = client.post(url, json={"equipment_id": non_dcr.id}, headers=OPS)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "dcr_mismatch"
        retired = _equipment(db_session, model_name="SS-OLD", is_active=False)
        assert client.post(url, json={"equipment_id": retired.id}, headers=OPS).json()["detail"]["code"] == \
            "equipment_inactive"
        assert client.post(url, json={"equipment_id": 99999}, headers=OPS).json()["detail"]["code"] == \
            "unknown_equipment"
        # Create the catalogue item from the nameplate, then match.
        created = client.post(f"{staff_exec_url(oid)}/catalogue", headers=OPS, json={
            "category": "module", "brand": "Sova Solar", "model_name": "SS-545M Bifacial", "is_dcr_compliant": True,
            "equivalent_reason": "Package not recorded on this project."}).json()["equipment"]
        r = client.post(url, json={"equipment_id": created["id"]}, headers=OPS)
        assert r.status_code == 200, r.text
        unit = r.json()["unit"]
        assert unit["equipment"]["id"] == created["id"] and unit["catalogue_unmatched"] is False
        assert unit["brand_as_printed"] == "Sova Solar"                       # kept as declared
        assert "catalogue_unmatched" not in r.json()["register"]["flags"]
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "matched")).scalar_one()
        assert audit.before_value["equipment_id"] is None and audit.after_value["equipment_id"] == created["id"]
        assert client.post(url, json={"equipment_id": created["id"]}, headers=OPS).json()["detail"]["code"] == \
            "already_matched"
        assert client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS).status_code == 200

    @pytest.mark.parametrize("who", [SALES, {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}])
    def test_match_roles(self, client, db_session, onboarding_ready, who):
        gna, oid, uid, _ = self._setup(client, db_session)
        match = _equipment(db_session, model_name="SS-545M Bifacial")
        assert client.post(f"{staff_exec_url(oid)}/units/{uid}/match", json={"equipment_id": match.id},
                           headers=who).status_code == 403

    def test_associate_can_correct_a_unit_back_to_unmatched(self, client, db_session, onboarding_ready, notifications):
        gna, oid = associate_in_execution(client, db_session)
        item = _equipment(db_session)
        uid = client.post(f"{exec_url(oid)}/units", headers=headers(gna), json={
            "category": "module", "equipment_id": item.id, "serial_number": "M-0001", "rating_wp": "540",
            "is_dcr": True}).json()["unit"]["unit_id"]
        r = client.patch(f"{exec_url(oid)}/units/{uid}", headers=headers(gna), json={
            "equipment_id": None, "brand_as_printed": "Sova Solar", "model_as_printed": "SS-550M"})
        assert r.status_code == 200, r.text
        assert r.json()["unit"]["equipment"] is None and r.json()["unit"]["model_as_printed"] == "SS-550M"
        assert len(notifications.of("catalogue_unmatched")) == 1
        assert db_session.get(InstalledUnit, uid).equipment_id is None

    def test_unmatched_brand_outside_the_package_is_flagged(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session, equipment_package="nova_assured")
        client.post(f"{exec_url(oid)}/units", headers=headers(gna),
                    json={**UNMATCHED_MODULE, "brand_as_printed": "Waaree"})
        reg = client.get(f"{staff_exec_url(oid)}/units", headers=OPS).json()
        assert reg["units"][0]["make_not_in_package"] is True and "make_not_in_package" in reg["flags"]


class TestPaymentListsNameTheCustomer:
    def test_rows_carry_customer_and_reference(self, client, db_session, onboarding_ready):
        from tests.payments_support import ACCOUNTS, accepted_customer, neft, pay_url
        gna, oid = accepted_customer(client, db_session)
        client.post(pay_url(oid), json=neft(), headers=headers(gna))
        payments = client.get("/api/v1/dashboard/payments", headers=ACCOUNTS).json()["items"]
        milestones = client.get("/api/v1/dashboard/payment-milestones", headers=ACCOUNTS).json()["items"]
        for row in payments + milestones:
            assert row["customer_name"] and row["project_reference_id"].startswith("GNE-")
        assert client.get("/api/v1/dashboard/payments", headers=OPS).status_code == 403
