"""BE-8 — the Project Archive importer: template, validation codes, dry run, commit, void."""
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.archive import config as archive_config
from app.archive.importer import TEMPLATE_COLUMNS
from app.memory.models import ArchiveImportBatch, ArchivedProject, AuditLog
from tests.archive_support import ADMIN, BASE, codes, csv_bytes, import_rows, row, upload, who
from tests.archive_support import territories  # noqa: F401 — fixture


def count(db) -> int:
    return db.execute(select(func.count()).select_from(ArchivedProject)).scalar_one()


class TestTemplate:
    def test_header_and_a_commented_example(self, client):
        r = client.get(f"{BASE}/template.csv", headers=ADMIN)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        lines = r.text.strip().splitlines()
        assert lines[0].split(",") == list(TEMPLATE_COLUMNS)
        assert len(lines) == 2 and lines[1].startswith("#EXAMPLE")

    def test_the_template_itself_imports_nothing(self, client, db_session):
        body = upload(client, client.get(f"{BASE}/template.csv", headers=ADMIN).content).json()
        assert body["row_count"] == 0 and body["accepted_count"] == 0 and body["errors"] == []

    @pytest.mark.parametrize("role", ["management", "accounts", "operations", "engineer", "sales"])
    def test_admin_only(self, client, role):
        assert client.get(f"{BASE}/template.csv", headers=who(role)).status_code == 403
        assert upload(client, csv_bytes([row()]), who_=who(role)).status_code == 403


class TestDryRunAndCommit:
    def test_dry_run_writes_nothing(self, client, db_session):
        r = upload(client, csv_bytes([row(), row(grid_nova_ref="GN-0002")]))
        body = r.json()
        assert r.status_code == 200 and body["dry_run"] is True
        assert (body["row_count"], body["accepted_count"], body["rejected_count"]) == (2, 2, 0)
        assert count(db_session) == 0
        assert db_session.execute(select(func.count()).select_from(ArchiveImportBatch)).scalar_one() == 0
        assert db_session.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_type == "archive_import")).scalar_one() == 0

    def test_commit_writes_accepted_rows_and_skips_rejected(self, client, db_session):
        data = csv_bytes([row(), row(grid_nova_ref="GN-0002", state=""), row(grid_nova_ref="GN-0003")])
        token = upload(client, data).json()["batch_token"]
        r = upload(client, data, dry_run=False, token=token)
        body = r.json()
        assert r.status_code == 200 and body["dry_run"] is False and body["batch_id"]
        assert (body["accepted_count"], body["rejected_count"]) == (2, 1)
        assert [e["row"] for e in body["errors"]] == [3]
        assert count(db_session) == 2
        batch = db_session.get(ArchiveImportBatch, body["batch_id"])
        assert batch.status.value == "imported" and batch.sha256 == token and batch.uploaded_by == "emp-admin-1"
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "archive_import")).scalar_one()
        assert audit.action == "imported" and audit.after_value["accepted"] == 2

    def test_commit_needs_the_token_of_this_file(self, client, db_session):
        data = csv_bytes([row()])
        r = upload(client, data, dry_run=False)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "batch_token_required"
        r = upload(client, data, dry_run=False, token="0" * 64)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "batch_token_mismatch"
        assert count(db_session) == 0

    def test_same_file_is_not_imported_twice(self, client, db_session):
        import_rows(client, [row()])
        data = csv_bytes([row()])
        dry = upload(client, data).json()
        assert dry["already_imported_batch_id"]
        r = upload(client, data, dry_run=False, token=dry["batch_token"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_imported"

    def test_nothing_to_import(self, client):
        data = csv_bytes([row(state="")])
        token = upload(client, data).json()["batch_token"]
        r = upload(client, data, dry_run=False, token=token)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "nothing_to_import"

    def test_batches_are_listed_and_reported(self, client):
        batch_id = import_rows(client, [row()]).json()["batch_id"]
        listing = client.get(f"{BASE}/imports", headers=ADMIN).json()["items"]
        assert [b["id"] for b in listing] == [batch_id] and listing[0]["status"] == "imported"
        detail = client.get(f"{BASE}/imports/{batch_id}", headers=ADMIN).json()
        assert detail["report"]["accepted_count"] == 1 and detail["filename"] == "projects.csv"
        assert client.get(f"{BASE}/imports/999", headers=ADMIN).json()["detail"]["code"] == "batch_not_found"


class TestValidationCodes:
    def one(self, client, **over):
        return upload(client, csv_bytes([row(**over)])).json()

    @pytest.mark.parametrize("column", ["scheme_or_segment", "state", "district", "commissioning_date"])
    def test_required_columns_in_a_row(self, client, column):
        body = self.one(client, **{column: ""})
        assert body["accepted_count"] == 0
        assert {(e["code"], e["column"]) for e in body["errors"]} == {("missing_required", column)}

    def test_at_least_one_size_field(self, client):
        body = self.one(client, capacity_kw="")
        assert "missing_size" in codes(body)
        assert self.one(client, capacity_kw="", pump_hp="5", scheme_or_segment="pm_kusum_pump")["accepted_count"] == 1
        assert self.one(client, capacity_kw="", street_lamp_count="40",
                        scheme_or_segment="street_lighting")["accepted_count"] == 1

    @pytest.mark.parametrize("column, value", [("scheme_or_segment", "nuclear"), ("system_type", "wind"),
                                              ("module_dcr", "maybe")])
    def test_invalid_choice(self, client, column, value):
        body = self.one(client, **{column: value})
        assert {(e["code"], e["column"]) for e in body["errors"]} == {("invalid_choice", column)}

    def test_choices_are_forgiving_about_case_and_spacing(self, client):
        body = self.one(client, scheme_or_segment="PM Surya Ghar - Residential", system_type="On-Grid", module_dcr="Y")
        assert body["accepted_count"] == 1

    @pytest.mark.parametrize("column", ["pump_hp", "contract_value_inr", "module_wattage_wp", "module_count",
                                       "inverter_count", "street_lamp_count", "latitude"])
    def test_invalid_number(self, client, column):
        body = self.one(client, **{column: "lots"})
        assert (("invalid_number", column)) in {(e["code"], e["column"]) for e in body["errors"]}

    def test_whole_numbers_only_for_counts(self, client):
        assert "invalid_number" in codes(self.one(client, module_count="6.5"))

    @pytest.mark.parametrize("column, value", [("latitude", "95"), ("module_count", "99999999999"),
                                              ("contract_value_inr", "9" * 20)])
    def test_out_of_range(self, client, column, value):
        extra = {"longitude": "88"} if column == "latitude" else {}
        assert "out_of_range" in codes(self.one(client, **{column: value}, **extra))

    @pytest.mark.parametrize("value", ["31/02/2025", "2025-13-01", "yesterday", "13/2025", "01/01/1850"])
    def test_invalid_date(self, client, value):
        assert "invalid_date" in codes(self.one(client, commissioning_date=value))

    def test_too_long(self, client):
        assert "too_long" in codes(self.one(client, module_make="x" * 121))

    def test_documents(self, client):
        assert self.one(client, documents="Report|https://example.test/a.pdf")["accepted_count"] == 1
        assert self.one(client, documents='[{"label": "A", "url": "https://example.test/a"}]')["accepted_count"] == 1
        for bad in ("Report|javascript:alert(1)", "not a link", "[1, 2]", "[{"):
            assert "invalid_documents" in codes(self.one(client, documents=bad)), bad

    def test_reserved_references(self, client):
        for ref in ("GNE-WB-RES-2026-00001", "ARC-7"):
            assert "reserved_ref" in codes(self.one(client, grid_nova_ref=ref))

    def test_more_cells_than_columns(self, client):
        data = csv_bytes([row()]).rstrip() + b",extra,cells\r\n"
        assert "too_many_cells" in codes(upload(client, data).json())

    def test_missing_and_duplicate_columns_reject_the_whole_file(self, client):
        cols = [c for c in TEMPLATE_COLUMNS if c != "district"]
        body = upload(client, csv_bytes([row()], columns=cols)).json()
        assert body["accepted_count"] == 0 and [(e["code"], e["column"]) for e in body["errors"]] == \
            [("missing_column", "district")]
        body = upload(client, csv_bytes([row()], columns=list(TEMPLATE_COLUMNS) + ["state"])).json()
        assert "duplicate_column" in codes(body)
        no_size = [c for c in TEMPLATE_COLUMNS if c not in ("capacity_kw", "pump_hp", "street_lamp_count")]
        assert "missing_column" in codes(upload(client, csv_bytes([row()], columns=no_size)).json())

    def test_unknown_columns_are_ignored_with_a_warning(self, client):
        data = csv_bytes([{**row(), "colour": "blue"}], columns=list(TEMPLATE_COLUMNS) + ["colour"])
        body = upload(client, data).json()
        assert body["accepted_count"] == 1 and "unknown_column" in codes(body, "warnings")

    def test_header_names_are_forgiving(self, client):
        lines = csv_bytes([row()]).decode().split("\r\n")
        lines[0] = ",".join(c.upper().replace("_", " ") for c in TEMPLATE_COLUMNS)
        assert upload(client, "\r\n".join(lines).encode()).json()["accepted_count"] == 1

    def test_errors_name_the_spreadsheet_row(self, client):
        body = upload(client, csv_bytes([row(), row(grid_nova_ref="GN-2"), row(grid_nova_ref="GN-3", state="")])).json()
        assert [e["row"] for e in body["errors"]] == [4]


class TestDuplicatesAndWarnings:
    def test_duplicate_ref_in_the_file(self, client):
        body = upload(client, csv_bytes([row(), row()])).json()
        assert (body["accepted_count"], body["rejected_count"]) == (1, 1)
        assert body["errors"][0]["code"] == "duplicate_grid_nova_ref" and "row 2" in body["errors"][0]["message"]

    def test_duplicate_ref_in_the_database(self, client):
        import_rows(client, [row()])
        body = upload(client, csv_bytes([row(), row(grid_nova_ref="GN-9")])).json()
        assert (body["accepted_count"], body["rejected_count"]) == (1, 1)
        assert "already in the archive" in body["errors"][0]["message"]

    def test_blank_refs_are_never_duplicates(self, client):
        assert upload(client, csv_bytes([row(grid_nova_ref=""), row(grid_nova_ref="")])).json()["accepted_count"] == 2

    def test_duplicate_consumer_number_is_a_warning(self, client):
        body = upload(client, csv_bytes([row(consumer_number="12 34-56"),
                                         row(grid_nova_ref="GN-2", consumer_number="123456")])).json()
        assert body["accepted_count"] == 2 and [w["row"] for w in body["warnings"]
                                                 if w["code"] == "duplicate_consumer_number"] == [3]
        import_rows(client, [row(consumer_number="777")])
        body = upload(client, csv_bytes([row(grid_nova_ref="GN-5", consumer_number="777")])).json()
        assert "duplicate_consumer_number" in codes(body, "warnings") and body["accepted_count"] == 1

    def test_unknown_place_names_are_warnings(self, client, territories):
        body = upload(client, csv_bytes([row(state="Atlantis", district="X"),
                                         row(grid_nova_ref="2", district="Nowhere"),
                                         row(grid_nova_ref="3", block="Nowhere Block")])).json()
        assert body["accepted_count"] == 3
        assert {w["code"] for w in body["warnings"]} == {"unknown_state", "unknown_district", "unknown_block"}

    def test_places_match_the_territory_master_but_keep_the_typed_text(self, client, db_session, territories):
        import_rows(client, [row(state="west bengal", district="ALIPURDUAR", block="Alipurduar - I", ),
                             row(grid_nova_ref="2", state="WB", district="664")])
        a, b = db_session.execute(select(ArchivedProject).order_by(ArchivedProject.id)).scalars()
        assert (a.state, a.district, a.block) == ("west bengal", "ALIPURDUAR", "Alipurduar - I")
        assert a.state_id and a.district_id and a.district_id == b.district_id and b.state_id == a.state_id

    def test_capacity_warnings(self, client, territories):
        body = upload(client, csv_bytes([row(capacity_kw=""), row(grid_nova_ref="2", capacity_kw="big", pump_hp="3"),
                                         row(grid_nova_ref="3", capacity_kw="big")])).json()
        assert codes(body, "warnings") == {"missing_capacity", "capacity_not_a_number"}
        assert body["accepted_count"] == 1   # row 3 has no other size; row 2 has pump_hp; row 1 has none -> rejected
        assert [e["code"] for e in body["errors"]] == ["missing_size", "missing_size"]

    def test_coordinates(self, client, territories):
        body = upload(client, csv_bytes([row(latitude="51.5", longitude="-0.12"),
                                         row(grid_nova_ref="2", latitude="26.5", longitude="89.5"),
                                         row(grid_nova_ref="3", latitude="26.5")])).json()
        assert body["accepted_count"] == 3
        assert [(w["row"], w["code"]) for w in body["warnings"]] == [(2, "coordinates_outside_india"),
                                                                     (4, "incomplete_coordinates")]

    def test_future_and_month_only_dates(self, client, db_session):
        far = f"01/01/{date.today().year + 2}"
        body = upload(client, csv_bytes([row(commissioning_date=far)])).json()
        assert "future_date" in codes(body, "warnings") and body["accepted_count"] == 1
        import_rows(client, [row(grid_nova_ref="M", commissioning_date="03/2025", warranty_end_modules=far)])
        stored = db_session.execute(select(ArchivedProject)).scalar_one()
        assert stored.commissioning_date == date(2025, 3, 1)
        assert "date_month_only" in {f["code"] for f in stored.data_quality_flags}
        assert "future_date" not in {f["code"] for f in stored.data_quality_flags}   # a warranty end is meant to be future

    def test_pincode_and_email(self, client, territories):
        body = upload(client, csv_bytes([row(pincode="12", customer_email="nope")])).json()
        assert codes(body, "warnings") == {"invalid_pincode", "invalid_email"} and body["accepted_count"] == 1

    def test_warnings_are_stored_with_the_row(self, client, db_session):
        import_rows(client, [row(capacity_kw="", pump_hp="2", state="Atlantis")])
        flags = db_session.execute(select(ArchivedProject)).scalar_one().data_quality_flags
        assert {f["code"] for f in flags} == {"missing_capacity", "unknown_state"}   # no territories seeded; the district is not judged

    def test_personal_data_notice(self, client):
        clean = upload(client, csv_bytes([row()])).json()
        assert clean["notices"] == []
        body = upload(client, csv_bytes([row(customer_phone="9800000000", consumer_number="1"),
                                         row(grid_nova_ref="2", customer_email="a@b.test")])).json()
        notice = body["notices"][0]
        assert notice["code"] == "personal_data_present" and notice["rows"] == 2
        assert notice["columns"] == ["customer_phone", "customer_email", "consumer_number"]
        assert notice["message"] == "personal data imported — visible to Admin/Management only"
        assert upload(client, csv_bytes([row(customer_name="Asha")])).json()["notices"] == []


class TestParsing:
    @pytest.mark.parametrize("raw, expected", [("15/03/2025", date(2025, 3, 15)), ("15-03-2025", date(2025, 3, 15)),
                                              ("2025-03-15", date(2025, 3, 15)), ("5/3/2025", date(2025, 3, 5)),
                                              ("03/2025", date(2025, 3, 1)), ("2025-03", date(2025, 3, 1))])
    def test_date_formats(self, client, db_session, raw, expected):
        import_rows(client, [row(commissioning_date=raw)])
        assert db_session.execute(select(ArchivedProject.commissioning_date)).scalar_one() == expected

    def test_indian_number_grouping_and_rupee_sign(self, client, db_session):
        import_rows(client, [row(capacity_kw="1,250.5 kW", contract_value_inr="₹ 12,34,567.50",
                                 subsidy_amount_inr="Rs. 78,000", module_wattage_wp="540 Wp")])
        r = db_session.execute(select(ArchivedProject)).scalar_one()
        assert (r.capacity_kw, r.contract_value_inr, r.subsidy_amount_inr, r.module_wattage_wp) == \
            (Decimal("1250.500"), Decimal("1234567.50"), Decimal("78000.00"), Decimal("540.00"))

    def test_blank_cells_are_null(self, client, db_session):
        import_rows(client, [row()])
        r = db_session.execute(select(ArchivedProject)).scalar_one()
        assert r.block is None and r.contract_value_inr is None and r.documents == []

    def test_bom_is_accepted(self, client):
        assert upload(client, csv_bytes([row()], bom=True)).json()["accepted_count"] == 1

    def test_not_utf8_is_refused(self, client):
        r = upload(client, csv_bytes([row(customer_name="Zoë")]).replace("Zoë".encode(), "Zo\xeb".encode("latin-1")))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_encoding"
        assert upload(client, b"").json()["detail"]["code"] == "empty_file"

    def test_unicode_survives(self, client, db_session):
        import_rows(client, [row(customer_name="অসীম দাস", address_text="Line 1\nLine 2")])
        r = db_session.execute(select(ArchivedProject)).scalar_one()
        assert (r.customer_name, r.address_text) == ("অসীম দাস", "Line 1\nLine 2")

    @pytest.mark.parametrize("cell", ["=HYPERLINK(\"http://evil.test\",\"x\")", "+1+1", "-2+3", "@SUM(A1)",
                                      "=cmd|' /C calc'!A0"])
    def test_formula_cells_are_stored_as_text_and_never_evaluated(self, client, db_session, cell):
        import_rows(client, [row(notes=cell, customer_name=cell, module_make=cell)])
        r = db_session.execute(select(ArchivedProject)).scalar_one()
        assert (r.notes, r.customer_name, r.module_make) == (cell, cell, cell)

    def test_control_characters_are_stripped(self, client, db_session):
        import_rows(client, [row(customer_name="As\x07ha\x0b")])
        assert db_session.execute(select(ArchivedProject.customer_name)).scalar_one() == "Asha"

    def test_oversize_file_is_refused(self, client, monkeypatch):
        monkeypatch.setitem(archive_config.load()["import"], "max_file_mb", 1)
        big = csv_bytes([row(notes="x" * 900)] * 1500)
        assert len(big) > 1024 * 1024
        assert upload(client, big).status_code == 413

    def test_5000_rows_are_fast(self, client, db_session):
        rows = [row(grid_nova_ref=f"GN-{i:05d}", district="Alipurduar", capacity_kw=str(1 + i % 9),
                    customer_name=f"Customer {i}", module_make="Make", notes="note") for i in range(5000)]
        data = csv_bytes(rows)
        started = time.perf_counter()
        dry = upload(client, data).json()
        done = upload(client, data, dry_run=False, token=dry["batch_token"]).json()
        elapsed = time.perf_counter() - started
        assert done["accepted_count"] == 5000 and count(db_session) == 5000
        assert elapsed < 10, f"{elapsed:.1f}s for dry run + import"


class TestVoid:
    def test_void_hides_the_batch_everywhere_and_is_audited(self, client, db_session):
        batch_id = import_rows(client, [row(), row(grid_nova_ref="GN-2")]).json()["batch_id"]
        listing = client.get(f"{BASE}/projects", headers=ADMIN).json()
        pid = listing["items"][0]["id"]
        assert listing["total"] == 2
        r = client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": "wrong file"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "voided" and r.json()["void_reason"] == "wrong file"
        assert client.get(f"{BASE}/projects", headers=ADMIN).json()["total"] == 0
        assert client.get(f"{BASE}/projects/{pid}", headers=ADMIN).status_code == 404
        assert client.get(f"{BASE}/stats", headers=ADMIN).json()["total"]["count"] == 0
        assert count(db_session) == 2   # never deleted
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "archive_import",
                                                          AuditLog.action == "voided")).scalar_one()
        assert audit.performed_by == "emp-admin-1" and audit.after_value["rows_hidden"] == 2

    def test_reason_is_required_and_double_void_refused(self, client):
        batch_id = import_rows(client, [row()]).json()["batch_id"]
        assert client.post(f"{BASE}/imports/{batch_id}/void", json={}, headers=ADMIN).status_code == 422
        assert client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": " "}, headers=ADMIN).status_code == 422
        assert client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": "ok then"}, headers=ADMIN).status_code == 200
        r = client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": "again"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_voided"
        assert client.post(f"{BASE}/imports/999/void", json={"reason": "none"}, headers=ADMIN).status_code == 404

    def test_a_voided_file_can_be_imported_again(self, client):
        first = import_rows(client, [row()]).json()["batch_id"]
        client.post(f"{BASE}/imports/{first}/void", json={"reason": "redo"}, headers=ADMIN)
        again = import_rows(client, [row()]).json()
        assert again["batch_id"] != first
        assert client.get(f"{BASE}/projects", headers=ADMIN).json()["total"] == 1

    @pytest.mark.parametrize("role", ["management", "accounts", "operations", "engineer", "sales"])
    def test_admin_only(self, client, role):
        batch_id = import_rows(client, [row()]).json()["batch_id"]
        assert client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": "no way"}, headers=who(role)).status_code == 403
        assert client.get(f"{BASE}/imports", headers=who(role)).status_code == 403
        assert client.get(f"{BASE}/imports/{batch_id}", headers=who(role)).status_code == 403


class TestRegistryAndLimits:
    def test_arc_entries_are_registered(self):
        from app.placeholders import registry
        reg = registry.load()
        assert reg.by_id["ARC-1"].blocking == "blocks_feature" and reg.by_id["ARC-2"].blocking == "tunable"
        assert "[UNCONFIRMED]" in (registry.REPO_ROOT / "config" / "archive.yaml").read_text(encoding="utf-8")

    def test_arc_1_resolves_once_projects_are_imported_and_reopens_on_void(self, client, db_session):
        from app.placeholders import registry
        entry = registry.load().by_id["ARC-1"]
        assert registry.evaluate(entry, db_session)["resolved"] is False
        batch_id = import_rows(client, [row()]).json()["batch_id"]
        assert registry.evaluate(entry, db_session)["resolved"] is True
        client.post(f"{BASE}/imports/{batch_id}/void", json={"reason": "test"}, headers=ADMIN)
        db_session.expire_all()
        assert registry.evaluate(entry, db_session)["resolved"] is False

    def test_the_upload_route_is_bounded_by_the_configured_size(self):
        from app.security.upload_limit import MULTIPART_OVERHEAD_BYTES, limit_for
        allowed, _ = limit_for("/api/v1/dashboard/archive/imports")
        assert allowed == archive_config.import_limits()["max_file_mb"] * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES
        assert limit_for("/api/v1/dashboard/archive/imports/1/void") is None
