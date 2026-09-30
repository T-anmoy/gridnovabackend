"""Phase 4A Step 4 — the installed-equipment serial register."""
import threading
from datetime import date

import pytest
from sqlalchemy import select

from app.associates.service import AssociateError
from app.execution import config as execution_config
from app.execution import register
from app.memory.models import (
    AssociateCapability, AuditLog, CustomerOnboarding, Equipment, InstalledUnit, ProjectExecution, ProjectStage,
)
from scripts import seed_dummy_data
from tests.conftest import TEST_DATABASE_URL
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, CAMERA, DASH, ENG, OPS, QUEUE, SALES, advance_to, associate_in_execution, exec_url, headers,
    jpeg, kyc_ready, make_associate, new_customer, notifications, onboarding_ready, staff_exec_url, territories,
    upload,
)


@pytest.fixture
def catalogue(db_session):
    seed_dummy_data.seed(db_session, "development")
    rows = {e.model_name: e.id for e in db_session.execute(select(Equipment)).scalars()}
    return {"dcr": rows["DUMMY-M540-DCR"], "dcr2": rows["DUMMY-M575-DCR"], "non_dcr": rows["DUMMY-M550-NDCR"],
            "inv": rows["DUMMY-INV-5K"]}


def module(catalogue, serial, *, kind="dcr", wp="540", is_dcr=None, **extra):
    return {"category": "module", "equipment_id": catalogue[kind], "serial_number": serial, "rating_wp": wp,
            "is_dcr": kind != "non_dcr" if is_dcr is None else is_dcr, **extra}


def inverter(catalogue, serial, kw="5"):
    return {"category": "inverter", "equipment_id": catalogue["inv"], "serial_number": serial, "rating_kw": kw}


def add(client, who, oid, body, staff=False):
    base = staff_exec_url(oid) if staff else exec_url(oid)
    return client.post(f"{base}/units", json=body, headers=who)


class TestNormalisation:
    @pytest.mark.parametrize("raw, normalized", [
        ("  ab 12-34 ", "AB12-34"),
        ("AB\t12\n34", "AB1234"),
        ("ＡＢ１２３４", "AB1234"),              # full-width phone keyboard
        ("AB​12‍34", "AB1234"),       # zero-width characters
        ("wsm/540-0001", "WSM/540-0001"),       # other characters kept
    ])
    def test_rule(self, raw, normalized):
        assert register.normalize_serial(raw) == normalized

    def test_look_alikes_stay_distinct(self):
        assert register.normalize_serial("AB0O1I") != register.normalize_serial("ABO0I1")


class TestEntering:
    def test_module_and_inverter(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session)
        r = add(client, headers(gna), oid, module(catalogue, " sn-0001 ", warranty_start="2026-10-01",
                                                  warranty_end="2051-10-01"))
        assert r.status_code == 201, r.text
        unit = r.json()["unit"]
        assert (unit["serial_number"], unit["serial_normalized"], unit["rating_wp"]) == ("sn-0001", "SN-0001",
                                                                                          "540.00")
        assert unit["equipment"]["is_placeholder"] is True and unit["status"] == "entered"
        assert r.json()["warnings"] == []
        r = add(client, headers(gna), oid, inverter(catalogue, "INV-0001"))
        assert r.status_code == 201 and r.json()["unit"]["rating_kw"] == "5.000"
        assert r.json()["register"]["totals"] == {"modules": 1, "inverters": 1, "other": 0, "inverter_kw": "5.000"}
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "installed_unit")).scalars().all()
        assert [a.action for a in audit] == ["added", "added"]

    @pytest.mark.parametrize("body, code", [
        (lambda c: module(c, "SN-1", is_dcr=False), "dcr_mismatch"),
        (lambda c: module(c, "SN-1", kind="non_dcr", is_dcr=True), "dcr_mismatch"),
        (lambda c: {"category": "module", "serial_number": "SN-1", "rating_wp": "540", "is_dcr": True},
         "equipment_required"),
        (lambda c: {**module(c, "SN-1"), "equipment_id": c["inv"]}, "equipment_category_mismatch"),
        (lambda c: {**module(c, "SN-1"), "rating_wp": None}, "rating_required"),
        (lambda c: module(c, "S1"), "invalid_serial"),
        (lambda c: module(c, "S" * 41), "invalid_serial"),
        (lambda c: module(c, "SN-1", warranty_start="2026-01-01"), "warranty_pair"),
        (lambda c: module(c, "SN-1", warranty_start="2026-01-01", warranty_end="2025-01-01"), "warranty_order"),
        (lambda c: module(c, "SN-1", warranty_start="2026-01-01", warranty_end="2067-01-02"), "warranty_too_long"),
        (lambda c: {**module(c, "SN-1"), "category": "battery"}, "invalid_category"),
    ])
    def test_rules(self, client, db_session, onboarding_ready, catalogue, body, code):
        gna, oid = associate_in_execution(client, db_session)
        r = add(client, headers(gna), oid, body(catalogue))
        assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == code, r.text

    def test_out_of_range_ratings_only_warn(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session)
        r = add(client, headers(gna), oid, module(catalogue, "SN-9", wp="900"))
        assert r.status_code == 201
        assert [w["code"] for w in r.json()["warnings"]] == ["rating_out_of_range"]
        r = add(client, headers(gna), oid, inverter(catalogue, "INV-9", kw="0.2"))
        assert [w["field"] for w in r.json()["warnings"]] == ["rating_kw"]

    def test_grid_nova_mode_is_staff_only(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session, capability=AssociateCapability.sales)
        assert add(client, headers(gna), oid, module(catalogue, "SN-1")).json()["detail"]["code"] == \
            "execution_mode_grid_nova"
        assert add(client, ENG, oid, module(catalogue, "SN-1"), staff=True).status_code == 403
        assert add(client, SALES, oid, module(catalogue, "SN-1"), staff=True).status_code == 403
        r = add(client, OPS, oid, module(catalogue, "SN-1"), staff=True)
        assert r.status_code == 201, r.text      # not swallowed by POST .../execution/{stage}
        assert client.get(f"{staff_exec_url(oid)}/units", headers=ENG).json()["totals"]["modules"] == 1


class TestDuplicates:
    def test_another_project_and_the_privacy_rule(self, client, db_session, onboarding_ready, catalogue):
        first, first_oid = associate_in_execution(client, db_session)
        second, second_oid = associate_in_execution(client, db_session)
        assert add(client, headers(first), first_oid, module(catalogue, "SN-5000")).status_code == 201
        # Same serial, typed differently, on a stranger's project: refused, no details.
        r = add(client, headers(second), second_oid, module(catalogue, " sn-5000 "))
        assert r.status_code == 409
        [err] = r.json()["detail"]["errors"]
        assert err["code"] == "duplicate_serial" and err["held_by"] is None
        assert err["message"] == "This serial number is already registered on another project."
        check = client.get(f"{exec_url(second_oid)}/units/check-serial", params={"serial": "SN - 5000"},
                           headers=headers(second)).json()
        assert check == {"serial_normalized": "SN-5000", "available": False, "on_this_project": False,
                         "message": register.DUPLICATE_MESSAGE, "held_by": None}
        # Staff are told where.
        check = client.get(f"{staff_exec_url(second_oid)}/units/check-serial", params={"serial": "sn-5000"},
                           headers=OPS).json()
        assert check["held_by"]["onboarding_id"] == first_oid and check["held_by"]["project_reference_id"]
        # The owner of both projects is told too — both are inside their scope.
        other_oid = new_customer(client, first)
        advance_to(db_session, other_oid, ProjectStage.agreement_signed)
        r = add(client, headers(first), other_oid, module(catalogue, "SN-5000"))
        assert r.json()["detail"]["errors"][0]["held_by"] == {"onboarding_id": first_oid}
        assert client.get(f"{exec_url(first_oid)}/units/check-serial", params={"serial": "sn-5000"},
                          headers=headers(first)).json()["on_this_project"] is True
        assert client.get(f"{exec_url(first_oid)}/units/check-serial", params={"serial": "NEW-1"},
                          headers=headers(first)).json()["available"] is True

    def test_bulk_is_all_or_nothing_with_per_row_errors(self, client, db_session, onboarding_ready, catalogue):
        other, other_oid = associate_in_execution(client, db_session)
        add(client, headers(other), other_oid, module(catalogue, "TAKEN-1"))
        gna, oid = associate_in_execution(client, db_session)
        rows = [module(catalogue, "BLK-1"), module(catalogue, "BLK-2", is_dcr=False), module(catalogue, "blk-1"),
                module(catalogue, "TAKEN-1"), inverter(catalogue, "BLK-INV")]
        r = client.post(f"{exec_url(oid)}/units/bulk", json={"units": rows}, headers=headers(gna))
        assert r.status_code == 422
        assert [(e["row"], e["code"]) for e in r.json()["detail"]["errors"]] == [
            (1, "dcr_mismatch"), (2, "duplicate_in_batch"), (3, "duplicate_serial")]
        assert db_session.execute(select(InstalledUnit).where(InstalledUnit.onboarding_id == oid)).first() is None
        ok = [module(catalogue, f"BLK-{i}") for i in range(3)] + [inverter(catalogue, "BLK-INV")]
        r = client.post(f"{exec_url(oid)}/units/bulk", json={"units": ok}, headers=headers(gna))
        assert r.status_code == 201 and len(r.json()["units"]) == 4

    def test_bulk_size_limit(self, client, db_session, onboarding_ready, catalogue, monkeypatch):
        monkeypatch.setitem(execution_config.execution()["register"]["sanity"], "bulk_max_rows", 2)
        gna, oid = associate_in_execution(client, db_session)
        rows = [module(catalogue, f"L-{i}") for i in range(3)]
        r = client.post(f"{exec_url(oid)}/units/bulk", json={"units": rows}, headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_rows"

    def test_voiding_frees_the_serial(self, client, db_session, onboarding_ready, catalogue):
        first, first_oid = associate_in_execution(client, db_session)
        unit = add(client, headers(first), first_oid, module(catalogue, "TYPO-1")).json()["unit"]
        url = f"{exec_url(first_oid)}/units/{unit['unit_id']}/void"
        assert client.post(url, json={"reason": " "}, headers=headers(first)).status_code == 422
        r = client.post(url, json={"reason": "Wrong project."}, headers=headers(first))
        assert r.status_code == 200 and r.json()["unit"]["status"] == "voided"
        assert r.json()["unit"]["serial_number"] == "TYPO-1" and r.json()["unit"]["serial_normalized"] is None
        second, second_oid = associate_in_execution(client, db_session)
        assert add(client, headers(second), second_oid, module(catalogue, "TYPO-1")).status_code == 201
        assert db_session.execute(select(InstalledUnit).where(InstalledUnit.serial_number == "TYPO-1")).scalars() \
            .all().__len__() == 2   # never deleted

    def test_database_refuses_a_duplicate_even_past_the_check(self, db_session, onboarding_ready, catalogue,
                                                             client):
        gna, oid = associate_in_execution(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, oid)
        execution = db_session.execute(select(ProjectExecution)).scalar_one()
        viewer = register.Viewer(associate=None)
        viewer.employee = type("E", (), {"employee_id": "emp-ops-1", "is_admin": False})()
        register.add_units(db_session, onboarding, execution, [module(catalogue, "RACE-1")], viewer)
        db_session.commit()
        unit = InstalledUnit(project_id=onboarding.project_id, onboarding_id=oid, category="module",
                             equipment_id=catalogue["dcr"], serial_number="race-1", serial_normalized="RACE-1",
                             rating_wp=540, is_dcr=True, created_by="x", status="entered")
        with pytest.raises(AssociateError) as exc:
            register._insert(db_session, unit, viewer)
        assert exc.value.code == "duplicate_serial"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")
def test_two_concurrent_inserts_exactly_one_wins(client, db_session, onboarding_ready, catalogue, _isolated_db):
    first, first_oid = associate_in_execution(client, db_session)
    second, second_oid = associate_in_execution(client, db_session)
    SessionLocal = _isolated_db
    barrier = threading.Barrier(2)
    results = []

    def attempt(oid):
        db = SessionLocal()
        try:
            onboarding = db.get(CustomerOnboarding, oid)
            execution = db.execute(select(ProjectExecution).where(ProjectExecution.onboarding_id == oid)).scalar_one()
            viewer = register.Viewer()
            viewer.employee = type("E", (), {"employee_id": "emp-ops-1", "is_admin": False})()
            barrier.wait()
            register.add_units(db, onboarding, execution, [module(catalogue, "CONCURRENT-1")], viewer)
            db.commit()
            results.append("ok")
        except AssociateError as exc:
            db.rollback()
            results.append(exc.code)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(oid,)) for oid in (first_oid, second_oid)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(results) == ["duplicate_serial", "ok"] or sorted(results) == ["ok", "register_rows_invalid"]
    assert len(db_session.execute(select(InstalledUnit).where(
        InstalledUnit.serial_normalized == "CONCURRENT-1")).scalars().all()) == 1


class TestVerificationAndLock:
    def _entered(self, client, db_session, catalogue):
        gna, oid = associate_in_execution(client, db_session)
        rows = [module(catalogue, f"MOD-{i}") for i in range(10)] + [inverter(catalogue, "INV-1")]
        assert client.post(f"{exec_url(oid)}/units/bulk", json={"units": rows}, headers=headers(gna)).status_code == 201
        return gna, oid

    def test_needs_modules_and_an_inverter(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session)
        add(client, headers(gna), oid, module(catalogue, "MOD-1"))
        r = client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS)
        assert r.status_code == 422 and r.json()["detail"]["missing"] == ["inverter"]

    def test_verify_locks_the_register(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = self._entered(client, db_session, catalogue)
        assert client.post(f"{staff_exec_url(oid)}/register/verify", headers=SALES).status_code == 403
        r = client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS)
        assert r.status_code == 200 and r.json()["register_verified_at"]
        assert {u["status"] for u in r.json()["register"]["units"]} == {"verified"}
        assert client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS).json()["detail"]["code"] == \
            "register_already_verified"
        unit_id = r.json()["register"]["units"][0]["unit_id"]
        assert client.patch(f"{exec_url(oid)}/units/{unit_id}", json={"serial_number": "MOD-X"},
                            headers=headers(gna)).json()["detail"]["code"] == "register_locked"
        assert client.patch(f"{staff_exec_url(oid)}/units/{unit_id}", json={"serial_number": "MOD-X"},
                            headers=OPS).json()["detail"]["code"] == "register_locked"
        assert add(client, headers(gna), oid, module(catalogue, "LATE-1")).json()["detail"]["code"] == \
            "register_locked"
        assert client.patch(f"{staff_exec_url(oid)}/units/{unit_id}", json={"serial_number": "MOD-X"},
                            headers=ADMIN).json()["detail"]["code"] == "reason_required"
        r = client.patch(f"{staff_exec_url(oid)}/units/{unit_id}",
                         json={"serial_number": "MOD-X", "reason": "Label misread on site."}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["unit"]["serial_normalized"] == "MOD-X"
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "installed_unit",
                                                          AuditLog.action == "updated")).scalar_one()
        assert audit.before_value["serial_number"] == "MOD-0" and audit.after_value["serial_number"] == "MOD-X"
        assert audit.after_value["reason"] == "Label misread on site."

    def test_installed_stage_goes_through_with_a_verified_register(self, client, db_session, onboarding_ready,
                                                                    catalogue):
        gna, oid = self._entered(client, db_session, catalogue)
        advance_to(db_session, oid, ProjectStage.material_dispatched)
        for i, slot in enumerate(execution_config.photo_slots()):
            assert upload(client, headers(gna), exec_url(oid), photo_slot=slot, content=jpeg(seed=200 + i),
                          **CAMERA).status_code == 201
        assert upload(client, headers(gna), exec_url(oid), document_type="oem_dcr_declaration").status_code == 201
        r = client.post(f"{exec_url(oid)}/submissions", json={"stage": "installed"}, headers=headers(gna))
        assert r.json()["detail"]["missing"] == ["register:verified"]
        client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS)
        r = client.post(f"{exec_url(oid)}/submissions", json={"stage": "installed"}, headers=headers(gna))
        assert r.status_code == 201 and r.json()["evidence"]["register_verified_at"]
        assert set(r.json()["evidence"]["photos"]) == set(execution_config.photo_slots())


class TestProjectFlags:
    def test_capacity_mismatch(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session)   # asked for 3 kW
        for i in range(10):                                      # 5.4 kW of modules
            add(client, headers(gna), oid, module(catalogue, f"CAP-{i}"))
        status = client.get(exec_url(oid), headers=headers(gna)).json()
        assert status["register"]["capacity"] == {"installed_module_kw": "5.400", "requested_kw": "3.0",
                                                  "tolerance_percent": "10", "mismatch": True}
        assert status["flags"] == ["capacity_mismatch"]
        item = client.get(QUEUE, headers=OPS).json()["items"][0]
        assert "capacity_mismatch" in item["attention_reasons"]

    def test_dcr_mismatch_on_pm_surya_ghar(self, client, db_session, onboarding_ready, catalogue, notifications):
        gna = make_associate(db_session, capability=AssociateCapability.execution)
        oid = new_customer(client, gna, scheme_linked=True, scheme_name="pm_surya_ghar", desired_output_kw=1.62)
        advance_to(db_session, oid, ProjectStage.portal_agreement_approved)
        add(client, headers(gna), oid, module(catalogue, "DCR-1"))
        assert "dcr_mismatch" not in client.get(exec_url(oid), headers=headers(gna)).json()["flags"]
        add(client, headers(gna), oid, module(catalogue, "DCR-2", kind="non_dcr"))
        add(client, headers(gna), oid, module(catalogue, "DCR-3", kind="non_dcr"))
        status = client.get(staff_exec_url(oid), headers=OPS).json()
        assert status["flags"] == ["dcr_mismatch"] and status["register"]["dcr"]["mismatch"] is True
        assert [p["onboarding_id"] for p in notifications.of("dcr_mismatch")] == [oid]   # once
        # subsidy_claimed is blocked until an Admin records a reason.
        onboarding = db_session.get(CustomerOnboarding, oid)
        from app.onboarding import progress
        stages = progress.applicable(db_session, onboarding)
        advance_to(db_session, oid, stages[stages.index(ProjectStage.subsidy_claimed) - 1])
        r = client.post(f"{exec_url(oid)}/submissions", json={"stage": "subsidy_claimed",
                                                             "text": {"portal_reference": "PMSG-1"}},
                        headers=headers(gna))
        sid = r.json()["submission_id"]
        r = client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=OPS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "dcr_mismatch"
        assert client.post(f"{staff_exec_url(oid)}/dcr-override", json={"reason": "x"}, headers=OPS).status_code \
            == 403
        r = client.post(f"{staff_exec_url(oid)}/dcr-override",
                        json={"reason": "MNRE confirmed the replacement is DCR; paperwork follows."}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["flags"] == []
        assert r.json()["register"]["dcr"]["override"]["by"] == "emp-admin-1"
        assert client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=OPS).status_code == 200

    def test_dcr_email_recipients(self, monkeypatch):
        import httpx
        from app.notifications.notifier import EmailNotifier, LogNotifier
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        n = EmailNotifier("k", "n@x.test", ["admin@x.test"], "https://app.test", LogNotifier(),
                          role_emails={"operations": ["ops@x.test"]})
        n.notify("dcr_mismatch", {"onboarding_id": 7, "customer_name": "Asha",
                                  "project_reference_id": "GNE-WB-ROF-2026-00007"})
        assert sent[0]["to"] == ["ops@x.test", "admin@x.test"]
        assert sent[0]["subject"] == "DCR mismatch: Asha (GNE-WB-ROF-2026-00007)"
        assert "https://app.test/dashboard/onboardings/7" in sent[0]["text"]
