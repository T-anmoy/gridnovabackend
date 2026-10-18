"""BE-9 — Nova Records' read-only tools, each × each staff role: scoping (whose projects and
leads a role reaches) and redaction (which fields it sees) happen inside the tool."""
import pytest

from app.records import tools
from tests.records_support import records, refs, run, today, viewer  # noqa: F401 — fixtures

LIVE_SALES1 = "GNE-WB-RES-2026-00125"     # first lead assigned to emp-sales-1
LIVE_SALES2 = "GNE-WB-RES-2026-00126"     # assigned to emp-sales-2
LIVE_UNASSIGNED = "GNE-WB-RES-2026-00127"
ALL_LIVE = {LIVE_SALES1, LIVE_SALES2, LIVE_UNASSIGNED}
ARCHIVE_REFS = {f"GN-ARC-01{n:02d}" for n in range(1, 11)}
MONEY_FIELDS = {"contract_value_inr", "subsidy_amount_inr", "amount_received_inr", "amount_outstanding_inr"}
CONTACT_FIELDS = {"customer_phone", "customer_email", "consumer_number"}


def _search(db, role, today, n=1, **filters):
    return run(db, role, "search_projects", {"filters": filters, "limit": 50}, today, n)


class TestSearchScoping:
    @pytest.mark.parametrize("role", ["admin", "management", "accounts", "engineer"])
    def test_full_live_and_archive_view(self, db_session, records, today, role):
        got = set(refs(_search(db_session, role, today)))
        assert got == ALL_LIVE | ARCHIVE_REFS

    def test_sales_sees_own_and_unassigned_live_projects_only(self, db_session, records, today):
        got = set(refs(_search(db_session, "sales", today)))
        assert LIVE_SALES1 in got and LIVE_UNASSIGNED in got and LIVE_SALES2 not in got
        other = set(refs(_search(db_session, "sales", today, n=2)))
        assert LIVE_SALES2 in other and LIVE_SALES1 not in other

    def test_operations_sees_projects_in_execution(self, db_session, records, today):
        assert ALL_LIVE <= set(refs(_search(db_session, "operations", today)))

    def test_lead_only_projects_are_not_projects(self, db_session, records, today):
        got = set(refs(_search(db_session, "admin", today, source="live")))
        assert got == ALL_LIVE

    def test_the_total_counts_only_what_the_role_may_see(self, db_session, records, today):
        admin = _search(db_session, "admin", today, district="Nadia")
        sales = _search(db_session, "sales", today, district="Nadia")
        assert admin["total"] == 4 and sales["total"] == 3


class TestRedaction:
    @pytest.mark.parametrize("role", ["operations", "engineer", "sales"])
    def test_no_money_for_roles_without_it(self, db_session, records, today, role):
        record = run(db_session, role, "project_dossier", {"ref": "GN-ARC-0101"}, today)["record"]
        assert not MONEY_FIELDS & set(record["fields"])

    @pytest.mark.parametrize("role", ["accounts", "operations", "engineer", "sales"])
    def test_no_contact_details_outside_admin_and_management(self, db_session, records, today, role):
        record = run(db_session, role, "project_dossier", {"ref": "GN-ARC-0101"}, today)["record"]
        assert not CONTACT_FIELDS & set(record["fields"])

    def test_sales_sees_no_customer_names_or_addresses(self, db_session, records, today):
        rows = _search(db_session, "sales", today, district="Howrah")["rows"]
        assert rows and all("customer_name" not in r and "block" not in r for r in rows)

    def test_admin_sees_everything(self, db_session, records, today):
        record = run(db_session, "admin", "project_dossier", {"ref": "GN-ARC-0101"}, today)["record"]
        assert record["fields"]["contract_value_inr"] == "310000.00"
        assert record["fields"]["customer_phone"] == "9830000101"

    def test_never_kyc(self, db_session, records, today):
        out = run(db_session, "admin", "associate_summary", {}, today)
        text = repr(out)
        assert "pan" not in {k for r in out["rows"] for k in r} and "fixture-not-a-pan" not in text
        assert "fixture-not-an-account" not in text and "FIXT0000000" not in text


class TestFilterPermission:
    @pytest.mark.parametrize("filters, field", [
        ({"customer_name": "Sen"}, "customer_name"), ({"block": "Bidhannagar"}, "block"),
        ({"near": {"place": "Kasba"}}, "near"), ({"associate": "GNA-WB-26-90001"}, "associate")])
    def test_sales_cannot_search_by_a_hidden_field(self, db_session, records, today, filters, field):
        with pytest.raises(tools.ToolError) as exc:
            _search(db_session, "sales", today, **filters)
        assert exc.value.code == "filter_not_permitted" and exc.value.field == field

    def test_text_search_only_reads_visible_fields(self, db_session, records, today):
        # "Salt Lake" is only in an address, which Sales may not see.
        assert _search(db_session, "sales", today, text="Salt Lake")["total"] == 0
        assert _search(db_session, "admin", today, text="Salt Lake")["total"] == 1


class TestFilters:
    def test_howrah_5kw_plus_2025(self, db_session, records, today):
        out = _search(db_session, "admin", today, district="Howrah", capacity_min_kw="5",
                      commissioned_from="2025-01-01", commissioned_to="2025-12-31")
        assert refs(out) == ["GN-ARC-0102", "GN-ARC-0101"]
        assert {r["inverter_make"] for r in out["rows"]} == {"Sungrow", "Growatt"}

    def test_make_filters_reach_the_live_register(self, db_session, records, today):
        out = _search(db_session, "admin", today, source="live", module_make="waaree")
        assert set(refs(out)) == {LIVE_SALES1, LIVE_UNASSIGNED}

    def test_reached_stage(self, db_session, records, today):
        out = _search(db_session, "admin", today, source="live", stage="inspection_net_meter")
        assert set(refs(out)) == {LIVE_SALES1, LIVE_UNASSIGNED}

    def test_near_a_place_on_record(self, db_session, records, today):
        out = _search(db_session, "admin", today, near={"place": "Kasba", "km": 2})
        assert set(refs(out)) == {"GN-ARC-0105", "GN-ARC-0107"}
        assert all(r["distance_km"] <= 2 for r in out["rows"])
        assert out["searched"]["anchor_projects"] == 2

    def test_near_coordinates(self, db_session, records, today):
        out = _search(db_session, "operations", today, near={"lat": 22.5867, "lng": 88.4171, "km": 1})
        assert refs(out) == ["GN-ARC-0106"]

    def test_near_unknown_place_finds_nothing(self, db_session, records, today):
        out = _search(db_session, "admin", today, near={"place": "Atlantis"})
        assert out["total"] == 0 and out["searched"]["anchor_projects"] == 0

    def test_limit_is_capped(self, db_session, records, today):
        with pytest.raises(ValueError):
            run(db_session, "admin", "search_projects", {"limit": 51}, today)

    def test_unknown_argument_is_rejected(self, db_session, records, today):
        with pytest.raises(ValueError):
            run(db_session, "admin", "search_projects", {"filters": {"colour": "red"}}, today)


class TestAggregate:
    def test_counts_by_district_in_sql(self, db_session, records, today):
        out = run(db_session, "admin", "aggregate", {"metric": "count", "group_by": "district"}, today)["aggregate"]
        assert out["projects"] == 13 and out["total"] == "13"
        assert {g["group"]: g["value"] for g in out["groups"]} == {
            "Howrah": "5", "Nadia": "4", "Kolkata": "2", "North 24 Parganas": "1", "Patna": "1"}

    def test_capacity_sum(self, db_session, records, today):
        out = run(db_session, "admin", "aggregate", {"metric": "sum_capacity_kw", "filters": {
            "commissioned_from": "2025-01-01", "commissioned_to": "2025-12-31"}}, today)["aggregate"]
        # 5 + 10 + 3 + 4 + 3 + 7.5 + 8 (archive, 2025)
        assert out["total"] == "40.500" and out["projects"] == 7

    def test_outstanding_after_inspection(self, db_session, records, today):
        out = run(db_session, "accounts", "aggregate", {"metric": "sum_outstanding", "filters": {
            "source": "live", "stage": "inspection_net_meter"}}, today)["aggregate"]
        assert out["total"] == "60000.00" and out["projects"] == 2

    def test_associate_ranking(self, db_session, records, today):
        from app.records.router import quarter_bounds
        start, end = quarter_bounds(today)
        out = run(db_session, "admin", "aggregate", {"metric": "count", "group_by": "associate", "filters": {
            "source": "live", "district": "Nadia", "created_from": start.isoformat(),
            "created_to": end.isoformat()}}, today)["aggregate"]
        assert out["groups"][0] == {"group": "GNA-WB-26-90001", "value": "2", "projects": 2,
                                    "associate_name": "Mitali Das"}

    @pytest.mark.parametrize("role", ["operations", "engineer", "sales"])
    def test_money_metrics_are_refused_without_money(self, db_session, records, today, role):
        with pytest.raises(tools.ToolError) as exc:
            run(db_session, role, "aggregate", {"metric": "sum_contract_value"}, today)
        assert exc.value.code == "not_permitted"

    def test_sales_cannot_group_by_associate(self, db_session, records, today):
        with pytest.raises(tools.ToolError):
            run(db_session, "sales", "aggregate", {"metric": "count", "group_by": "associate"}, today)

    def test_sales_counts_only_its_projects(self, db_session, records, today):
        admin = run(db_session, "admin", "aggregate", {"metric": "count", "filters": {"source": "live"}}, today)
        sales = run(db_session, "sales", "aggregate", {"metric": "count", "filters": {"source": "live"}}, today)
        assert admin["aggregate"]["total"] == "3" and sales["aggregate"]["total"] == "2"


class TestOtherTools:
    def test_warranty_expiring(self, db_session, records, today):
        out = run(db_session, "engineer", "warranty_expiring", {"days": 90}, today)
        assert set(refs(out)) == {"GN-ARC-0101", LIVE_SALES1}

    @pytest.mark.parametrize("role, allowed", [("admin", True), ("management", True), ("accounts", True),
                                               ("sales", False), ("operations", False), ("engineer", False)])
    def test_payments_status_roles(self, db_session, records, today, role, allowed):
        if not allowed:
            with pytest.raises(tools.ToolError):
                run(db_session, role, "payments_status", {}, today)
            return
        out = run(db_session, role, "payments_status", {"only_outstanding": True}, today)
        assert out["aggregate"]["total_outstanding"] == "220000.00"
        assert set(refs(out)) == {LIVE_SALES1, LIVE_SALES2}

    @pytest.mark.parametrize("role, allowed", [("admin", True), ("management", True), ("sales", True),
                                               ("engineer", True), ("operations", False), ("accounts", False)])
    def test_lead_lookup_roles(self, db_session, records, today, role, allowed):
        if not allowed:
            with pytest.raises(tools.ToolError):
                run(db_session, role, "lead_lookup", {}, today)
            return
        out = run(db_session, role, "lead_lookup", {"limit": 50}, today)
        assert out["rows"]
        if role == "engineer":
            assert all("customer_name" not in r for r in out["rows"])
            assert all(not k.startswith("field_") or k[6:] in tools.TECHNICAL_LEAD_FIELDS for r in out["rows"] for k in r)

    def test_sales_cannot_look_up_another_reps_lead(self, db_session, records, today):
        lead = records["leads"]["priya"]["lead_id"]
        assert run(db_session, "sales", "lead_lookup", {"lead_id": lead}, today)["rows"] == []
        assert run(db_session, "sales", "lead_lookup", {"lead_id": lead}, today, n=2)["rows"]

    def test_pipeline_summary_is_scoped(self, db_session, records, today):
        admin = run(db_session, "admin", "pipeline_summary", {}, today)["aggregate"]
        sales = run(db_session, "sales", "pipeline_summary", {}, today)["aggregate"]
        assert admin["total"] == "5" and sales["total"] == "3"

    @pytest.mark.parametrize("role, allowed", [("admin", True), ("management", True), ("sales", True),
                                               ("engineer", False), ("operations", False), ("accounts", False)])
    def test_customer_records_roles(self, db_session, records, today, role, allowed):
        cid = records["live"][LIVE_SALES1]["customer_id"]
        args = {"customer_id": cid, "question": "what does he prefer?"}
        if not allowed:
            with pytest.raises(tools.ToolError):
                run(db_session, role, "customer_records", args, today)
            return
        out = run(db_session, role, "customer_records", args, today)
        assert out["record"]["customer_ref"] == f"C{cid}" and "[L" in out["record"]["context"]

    def test_customer_records_scope_for_sales(self, db_session, records, today):
        cid = records["live"][LIVE_SALES2]["customer_id"]
        out = run(db_session, "sales", "customer_records", {"customer_id": cid, "question": "x"}, today)
        assert out["record"] is None

    def test_service_requests_carry_the_description_as_untrusted(self, db_session, records, today):
        out = run(db_session, "operations", "service_requests", {"status": "open"}, today)
        assert refs(out) == ["SR-1", LIVE_SALES1] and out["rows"][0]["project_ref"] == LIVE_SALES1
        assert "description" not in out["rows"][0] and out["untrusted"][0]["field"] == "description"
        with pytest.raises(tools.ToolError):
            run(db_session, "accounts", "service_requests", {}, today)

    @pytest.mark.parametrize("role, sees", [("admin", True), ("accounts", True), ("management", False)])
    def test_commission_totals_admin_and_accounts_only(self, db_session, records, today, role, sees):
        rows = run(db_session, role, "associate_summary", {"associate_id": "GNA-WB-26-90001"}, today)["rows"]
        assert rows[0]["customers_onboarded"] == "2"
        assert ("commission_earned_inr" in rows[0]) is sees

    def test_catalogue_prices_only_for_price_roles(self, db_session, records, today):
        args = {"package": "NOVA_ASSURED", "phase": "single_phase", "kw": 3}
        with pytest.raises(tools.ToolError):
            run(db_session, "engineer", "catalogue_lookup", args, today)
        out = run(db_session, "sales", "catalogue_lookup", args, today)
        assert out["record"]["ref"] == "PRICE-NOVA_ASSURED-single_phase-3"
        makes = run(db_session, "engineer", "catalogue_lookup", {"component": "inverter"}, today)
        assert any("Growatt" in r["makes"] for r in makes["rows"])

    def test_company_reference(self, db_session, records, today):
        out = run(db_session, "operations", "company_reference", {}, today)
        assert "Kasba Industrial Estate" in out["record"]["facts"]

    def test_dossier_of_a_hidden_project_is_not_on_record(self, db_session, records, today):
        assert run(db_session, "sales", "project_dossier", {"ref": LIVE_SALES2}, today)["record"] is None
        assert run(db_session, "admin", "project_dossier", {"ref": "GNE-XX-RES-2026-99999"}, today)["record"] is None

    def test_paths_only_for_pages_the_role_opens(self, db_session, records, today):
        live = records["live"][LIVE_SALES1]
        admin = run(db_session, "admin", "project_dossier", {"ref": LIVE_SALES1}, today)["refs"][0]
        accounts = run(db_session, "accounts", "project_dossier", {"ref": LIVE_SALES1}, today)["refs"][0]
        assert admin["path"] == f"/dashboard/operations/{live['onboarding_id']}"
        assert "path" not in accounts
        arc = run(db_session, "sales", "project_dossier", {"ref": "GN-ARC-0101"}, today)["refs"][0]
        assert arc["path"] == f"/dashboard/archive/{records['archive']['GN-ARC-0101']}"


def test_every_tool_has_a_schema():
    schemas = tools.schemas()
    assert set(schemas) == set(tools.TOOLS)
    assert all(s["parameters"]["type"] == "object" for s in schemas.values())


def test_no_tool_writes(db_session, records, today):
    """Every tool runs and the session has nothing new, changed or deleted."""
    for name in tools.TOOLS:
        args = {"ref": "GN-ARC-0101"} if name == "project_dossier" else \
            {"metric": "count"} if name == "aggregate" else \
            {"question": "anything", "name": "Sunil"} if name == "customer_records" else {}
        tools.run(db_session, viewer("admin"), name, args, today)
        assert not (db_session.new or db_session.dirty or db_session.deleted), name


class TestLiveBlock:
    """A live project's block is its onboarding's territory block (BE-9 correction)."""

    def test_block_filter_finds_live_and_archived(self, db_session, records, today):
        assert refs(_search(db_session, "admin", today, block="Krishnanagar - I")) == [LIVE_SALES1]
        assert refs(_search(db_session, "operations", today, block="Bidhannagar")) == ["GN-ARC-0106"]

    def test_block_shown_like_the_archive_block(self, db_session, records, today):
        rows = {r["ref"]: r for r in _search(db_session, "engineer", today)["rows"]}
        assert rows[LIVE_SALES1]["block"] == "Krishnanagar - I"
        assert rows["GN-ARC-0106"]["block"] == "Bidhannagar"
        dossier = run(db_session, "admin", "project_dossier", {"ref": LIVE_SALES1}, today)["record"]["fields"]
        assert dossier["block"] == "Krishnanagar - I"

    def test_no_block_id_means_no_block(self, db_session, records, today):
        fields = run(db_session, "admin", "project_dossier", {"ref": LIVE_SALES2}, today)["record"]["fields"]
        assert fields["block"] is None and fields["district"] == "Nadia"
        row = next(r for r in _search(db_session, "admin", today)["rows"] if r["ref"] == LIVE_SALES2)
        assert row["block"] is None

    def test_group_by_block(self, db_session, records, today):
        out = run(db_session, "admin", "aggregate", {"metric": "count", "group_by": "block"}, today)["aggregate"]
        groups = {g["group"]: g["value"] for g in out["groups"]}
        assert groups["Krishnanagar - I"] == "1" and groups["Bidhannagar"] == "1"

    def test_text_search_reaches_the_live_block(self, db_session, records, today):
        assert LIVE_SALES1 in refs(_search(db_session, "admin", today, text="Krishnanagar - I"))

    def test_sales_still_cannot_see_or_use_block(self, db_session, records, today):
        rows = _search(db_session, "sales", today)["rows"]
        assert rows and all("block" not in r for r in rows)
        assert "block" not in run(db_session, "sales", "project_dossier", {"ref": LIVE_SALES1}, today)["record"]["fields"]
        for args in ({"metric": "count", "group_by": "block"},):
            with pytest.raises(tools.ToolError):
                run(db_session, "sales", "aggregate", args, today)
        with pytest.raises(tools.ToolError):
            _search(db_session, "sales", today, block="Krishnanagar - I")
        # "Krishnanagar - I" is not searchable through free text either
        assert LIVE_SALES1 not in refs(_search(db_session, "sales", today, text="Krishnanagar - I"))


class TestCitationPaths:
    def test_customer_refs_never_link(self, db_session, records, today):
        cid = records["live"][LIVE_SALES1]["customer_id"]
        out = run(db_session, "admin", "customer_records", {"customer_id": cid, "question": "x"}, today)
        customer = next(r for r in out["refs"] if r["kind"] == "customer")
        assert customer["path"] is None
        lead = next(r for r in out["refs"] if r["kind"] == "lead")
        assert lead["path"] == f"/dashboard/leads/{records['live'][LIVE_SALES1]['lead_id']}"
        leads = run(db_session, "admin", "lead_lookup", {"limit": 50}, today)
        assert all(r["path"] is None for r in leads["refs"] if r["kind"] == "customer")

    @pytest.mark.parametrize("role", ["admin", "management", "engineer", "operations", "sales"])
    def test_service_requests_link_to_their_page(self, db_session, records, today, role):
        out = run(db_session, role, "service_requests", {}, today)
        sr = next(r for r in out["refs"] if r["kind"] == "service_request")
        assert sr["path"] == f"/dashboard/service-requests/{sr['ref'][3:]}"

    def test_a_leads_onboarded_project_links_to_operations(self, db_session, records, today):
        live = records["live"][LIVE_SALES1]
        out = run(db_session, "admin", "lead_lookup", {"lead_id": live["lead_id"]}, today)
        project = next(r for r in out["refs"] if r["kind"] == "live_project")
        assert project["path"] == f"/dashboard/operations/{live['onboarding_id']}"
        priya = records["leads"]["priya"]
        out = run(db_session, "admin", "lead_lookup", {"lead_id": priya["lead_id"]}, today)
        project = next(r for r in out["refs"] if r["kind"] == "live_project")
        assert project["path"] == f"/dashboard/leads/{priya['lead_id']}"

    @pytest.mark.parametrize("kind_tool, args", [("associate_summary", {}), ("company_reference", {}),
                                                 ("catalogue_lookup", {})])
    def test_records_without_a_page_have_no_path(self, db_session, records, today, kind_tool, args):
        assert all(not r.get("path") for r in run(db_session, "admin", kind_tool, args, today)["refs"])

    def test_config_holds_exactly_the_existing_pages(self):
        from app.records import config
        assert set(config.load()["paths"]) == {"live_project", "lead", "service_request", "archive"}
        data = {**config.load(), "paths": {**config.load()["paths"], "customer": "/dashboard/customers/{customer_id}"}}
        with pytest.raises(config.RecordsConfigError):
            config._validate(data)
