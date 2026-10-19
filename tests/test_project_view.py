"""BE-8 — app/projects/view.py: live and archived projects in one shape, redacted by role."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.archive import config
from app.archive.config import ArchiveConfigError
from app.memory.models import ArchivedProject, CustomerOnboarding, Lead, Project, ProjectStage
from app.projects import view
from tests.archive_support import ADMIN, BASE, import_rows, row, who
from tests.archive_support import territories  # noqa: F401 — fixture
from tests.handover_support import (  # noqa: F401 — fixtures
    catalogue, kyc_ready, notifications, onboarding_ready, prepare, stub_pdf,
)

EMPLOYEE = lambda role, eid=None: SimpleNamespace(role=config_role(role), employee_id=eid or f"emp-{role}-1")   # noqa: E731


def config_role(name):
    from app.memory.models import EmployeeRole
    return EmployeeRole(name)


@pytest.fixture
def live(client, db_session, onboarding_ready, catalogue):
    """A commissioned Grid Nova-executed project: two DCR modules, one inverter, 100000 accepted, all of it paid."""
    oid = prepare(client, db_session, catalogue, stage=ProjectStage.commissioned)
    onboarding = db_session.get(CustomerOnboarding, oid)
    return db_session.get(Project, onboarding.project_id)


@pytest.fixture
def archived(client, db_session, territories):
    import_rows(client, [row(customer_name="Asha Roy", customer_phone="9800000000", contract_value_inr="250000",
                             module_make="Waaree", module_count="6", module_wattage_wp="540", module_dcr="yes",
                             inverter_make="Growatt", inverter_count="1", inverter_capacity_kw="3",
                             installation_date="01/03/2025", warranty_end_modules="15/03/2050",
                             documents="Report|https://example.test/r.pdf")])
    return db_session.execute(select(ArchivedProject)).scalar_one()


class TestConfig:
    def test_every_field_the_view_can_return_is_in_exactly_one_group(self, db_session, live, archived):
        groups = config.field_groups()
        admin = EMPLOYEE("admin")
        for subject in (live, archived):
            assert set(view.project_dossier(db_session, subject, admin)["fields"]) <= set(groups)

    def test_every_role_is_given_a_map(self):
        assert set(config.load()["visibility"]["roles"]) == {"admin", "management", "accounts", "operations",
                                                              "engineer", "sales"}
        assert config.groups_for("nobody") == frozenset()

    @pytest.mark.parametrize("change, message", [
        (lambda d: d["visibility"]["groups"]["money"].append("capacity_kw"), "in both"),
        (lambda d: d["visibility"]["roles"].pop("sales"), "exactly"),
        (lambda d: d["visibility"]["roles"]["sales"].append("imaginary"), "unknown group"),
        (lambda d: d["import"].update(max_rows=0), "positive"),
    ])
    def test_a_bad_file_is_refused(self, change, message):
        import copy
        data = copy.deepcopy(config.load())
        change(data)
        with pytest.raises(ArchiveConfigError, match=message):
            config._validate(data)


class TestArchivedProject:
    def test_dossier(self, db_session, archived):
        body = view.project_dossier(db_session, archived, EMPLOYEE("admin"))
        assert body["source"] == "archive" and body["ref"] == "GN-0001" and body["id"] == archived.id
        assert body["fields"]["contract_value_inr"] == {"value": "250000.00", "source_table": "archived_projects",
                                                         "record_id": archived.id}
        equipment = {e["value"]["category"]: e["value"] for e in body["sections"]["equipment"]}
        assert equipment["module"]["make"] == "Waaree" and equipment["module"]["count"] == 6
        assert [s["value"]["stage"] for s in body["sections"]["stages"]] == ["installed", "commissioned"]
        assert body["sections"]["stages"][0]["value"]["date"] == "2025-03-01"
        assert body["sections"]["warranty"][0]["value"]["module_warranty_end"] == "2050-03-15"
        assert body["sections"]["documents"][0]["value"] == {"label": "Report", "url": "https://example.test/r.pdf"}

    def test_redaction_is_done_inside_the_function(self, db_session, archived):
        sales = view.project_dossier(db_session, archived, EMPLOYEE("sales"))
        text = str(sales)
        assert "Asha" not in text and "9800000000" not in text and "250000" not in text
        assert "customer_name" not in sales["fields"] and "state" in sales["fields"]
        card = view.project_card(db_session, archived, EMPLOYEE("sales"))
        assert "customer_name" not in card["fields"] and card["ref"] == "GN-0001"

    def test_a_voided_row_is_not_viewable(self, client, db_session, archived):
        from datetime import datetime, timezone
        archived.voided_at = datetime.now(timezone.utc)
        db_session.commit()
        with pytest.raises(view.ViewForbidden):
            view.project_card(db_session, archived, EMPLOYEE("admin"))
        assert view.find_subject(db_session, "GN-0001") is None and view.find_subject(db_session, f"ARC-{archived.id}") is None


class TestLiveProject:
    def test_dossier(self, db_session, live):
        body = view.project_dossier(db_session, live, EMPLOYEE("admin"))
        f = body["fields"]
        assert body["source"] == "live" and body["ref"] == live.reference_id and body["ref"].startswith("GNE-")
        assert f["contract_value_inr"]["value"] == "100000.00" and f["contract_value_inr"]["source_table"] == "proposals"
        assert f["amount_received_inr"]["value"] == "100000.00" and f["amount_outstanding_inr"]["value"] == "0.00"
        assert f["module_count"]["value"] == 2 and f["module_make"]["source_table"] == "installed_units"
        assert f["current_stage"]["value"] == "commissioned"
        assert f["commissioning_date"]["source_table"] == "project_stage_events" and f["commissioning_date"]["value"]
        assert f["associate_id"]["value"].startswith("GNA-") and f["customer_name"]["source_table"] == "customers"
        modules = [e for e in body["sections"]["equipment"] if e["value"]["category"] == "module"]
        assert len(modules) == 1 and modules[0]["value"]["count"] == 2 and len(modules[0]["record_id"]) == 2
        assert "commissioned" in [s["value"]["stage"] for s in body["sections"]["stages"]]

    def test_same_shape_as_an_archived_project(self, db_session, live, archived):
        a = view.project_dossier(db_session, live, EMPLOYEE("admin"))
        b = view.project_dossier(db_session, archived, EMPLOYEE("admin"))
        assert set(a) == set(b) == {"source", "ref", "id", "fields", "sections"}
        assert set(a["sections"]) == set(b["sections"])
        # Everything the archive holds is also a field of a live project, except the import bookkeeping.
        assert set(b["fields"]) - {"import_batch_id", "source_row"} <= set(a["fields"])
        for cell in list(a["fields"].values()) + list(b["fields"].values()):
            assert set(cell) == {"value", "source_table", "record_id"}
        eq_live = a["sections"]["equipment"][0]["value"]
        eq_arch = b["sections"]["equipment"][0]["value"]
        assert set(eq_live) == set(eq_arch) == {"category", "make", "model", "rating", "count", "dcr"}
        assert set(a["sections"]["warranty"][0]["value"] if a["sections"]["warranty"] else b["sections"]["warranty"][0]["value"]) \
            == {"handover_date", "system_warranty_end", "om_free_until", "module_warranty_end", "inverter_warranty_end"}

    @pytest.mark.parametrize("role, hidden", [
        ("accounts", ["customer_phone", "customer_email", "consumer_number"]),
        ("operations", ["customer_phone", "contract_value_inr", "amount_received_inr", "amount_outstanding_inr"]),
        ("engineer", ["customer_email", "contract_value_inr", "amount_received_inr"]),
        ("sales", ["customer_name", "customer_phone", "address_text", "contract_value_inr", "associate_id",
                   "notes", "block"]),
    ])
    def test_role_redaction(self, db_session, live, role, hidden):
        body = view.project_dossier(db_session, live, EMPLOYEE(role, "emp-sales-1"))
        for name in hidden:
            assert name not in body["fields"], (role, name)
        assert "state" in body["fields"] and "capacity_kw" in body["fields"]
        assert "100000" not in str(body) or role in ("accounts",)
        assert "Asha" not in str(body) or role != "sales"

    def test_management_and_accounts_see_money(self, db_session, live):
        for role in ("management", "accounts"):
            assert "contract_value_inr" in view.project_dossier(db_session, live, EMPLOYEE(role))["fields"]

    def test_sales_scoping_is_no_weaker_than_the_lead_rule(self, db_session, live):
        lead = db_session.execute(select(Lead).where(Lead.project_id == live.id)).scalars().first()
        assert lead.assigned_employee_id is None
        assert view.project_card(db_session, live, EMPLOYEE("sales", "emp-sales-1"))["ref"] == live.reference_id   # unassigned
        from app.memory.models import Employee, EmployeeRole
        for eid in ("emp-someone-else", "emp-sales-1"):
            if db_session.get(Employee, eid) is None:
                db_session.add(Employee(id=eid, name=eid, email=f"{eid}@example.test", role=EmployeeRole.sales))
        db_session.commit()
        lead.assigned_employee_id = "emp-someone-else"
        db_session.commit()
        with pytest.raises(view.ViewForbidden):
            view.project_dossier(db_session, live, EMPLOYEE("sales", "emp-sales-1"))
        lead.assigned_employee_id = "emp-sales-1"
        db_session.commit()
        assert view.project_dossier(db_session, live, EMPLOYEE("sales", "emp-sales-1"))["ref"] == live.reference_id

    def test_operations_only_sees_projects_in_execution(self, db_session, live):
        from app.memory.models import ProjectExecution
        execution = db_session.execute(select(ProjectExecution).where(ProjectExecution.project_id == live.id)).scalar_one()
        assert view.project_card(db_session, live, EMPLOYEE("operations"))
        db_session.delete(execution)
        db_session.flush()
        with pytest.raises(view.ViewForbidden):
            view.project_card(db_session, live, EMPLOYEE("operations"))
        db_session.rollback()

    def test_find_subject(self, db_session, live, archived):
        assert view.find_subject(db_session, live.reference_id).id == live.id
        assert view.find_subject(db_session, "GN-0001").id == archived.id
        assert view.find_subject(db_session, f"arc-{archived.id}").id == archived.id
        assert view.find_subject(db_session, "GN-NOPE") is None and view.find_subject(db_session, "") is None


class TestNothingExposesPersonalDocuments:
    def test_no_identity_documents_in_either_source(self, db_session, live, archived):
        for subject in (live, archived):
            text = str(view.project_dossier(db_session, subject, EMPLOYEE("admin"))).lower()
            for banned in ("aadhaar", "pan_number", "bank_account", "ifsc", "fernet"):
                assert banned not in text


class TestLiveBlock:
    def test_the_onboardings_block_is_shown(self, db_session, live):
        from app.memory.models import TerritoryBlock
        onboarding = db_session.execute(select(CustomerOnboarding).where(
            CustomerOnboarding.project_id == live.id)).scalar_one()
        expected = db_session.get(TerritoryBlock, onboarding.block_id).name
        field = view.project_dossier(db_session, live, EMPLOYEE("engineer"))["fields"]["block"]
        assert field == {"value": expected, "source_table": "customer_onboardings", "record_id": onboarding.id}
        assert view.project_card(db_session, live, EMPLOYEE("admin"))["fields"]["block"]["value"] == expected

    def test_no_block_id_is_null_not_guessed(self, db_session, live):
        onboarding = db_session.execute(select(CustomerOnboarding).where(
            CustomerOnboarding.project_id == live.id)).scalar_one()
        onboarding.block_id = None
        db_session.flush()
        assert view.project_dossier(db_session, live, EMPLOYEE("admin"))["fields"]["block"]["value"] is None

    def test_sales_never_sees_block(self, db_session, live):
        assert "block" not in view.project_dossier(db_session, live, EMPLOYEE("sales", "emp-sales-1"))["fields"]
