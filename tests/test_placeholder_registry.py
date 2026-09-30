"""Phase 4A Step 1 — the placeholder registry (config/placeholder_registry.yaml).

The honesty test here is the point of the whole registry: any placeholder marker (or
module-level PLACEHOLDER_* constant) in app/, config/ or scripts/ that no entry or
exemption claims fails the build, so the list cannot silently rot."""
import importlib
import shutil

import pytest
import yaml
from sqlalchemy import select

from app.agents import guardrails as g
from app.llm.company_facts import COMPANY_FACTS
from app.memory.models import AuditLog, Equipment, EquipmentCategory, PlaceholderResolution
from app.placeholders import registry
from scripts import seed_dummy_data

URL = "/api/v1/dashboard/placeholders"


def staff(role: str) -> dict:
    return {"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role}


_LOAD = registry.load


@pytest.fixture(autouse=True)
def _fresh_registry():
    _LOAD.cache_clear()
    yield
    _LOAD.cache_clear()


class TestRegistryFile:
    def test_loads_and_every_location_exists(self):
        reg = registry.load()
        assert len(reg.entries) >= 50
        for entry in reg.entries:
            if "env" in entry.location:
                continue
            path = registry.REPO_ROOT / entry.file
            assert path.exists(), entry.id
            if "key" in entry.location:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                registry._yaml_value(data, registry._resolve_key(data, entry.location["key"]))
            else:
                module = importlib.import_module(entry.file[:-3].replace("/", "."))
                for symbol in entry.location["symbols"]:
                    assert symbol == "__doc__" or hasattr(module, symbol), (entry.id, symbol)

    def test_this_phases_manifest_is_registered(self):
        ids = set(registry.load().by_id)
        assert {"E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8",
                "A1", "D1"} <= ids

    def test_every_needed_sentence_is_a_plain_question_or_request(self):
        for entry in registry.load().entries:
            text = entry.needed_from_grid_nova.strip()
            assert text.endswith(("?", ".")) and "[" not in text, entry.id

    def test_invalid_file_is_refused(self):
        with pytest.raises(registry.RegistryError, match="blocking must be one of"):
            registry._validate({"entries": [{
                "id": "X", "area": "pricing", "location": {"env": ["A"]}, "description": "d", "dummy_value": 1,
                "needed_from_grid_nova": "q?", "blocking": "urgent", "probe": "manual"}]})


class TestHonesty:
    def test_every_marker_in_the_repo_is_registered(self):
        orphans, stale = registry.unclaimed()
        assert orphans == [], "unregistered placeholder markers:\n" + "\n".join(
            f"  {h.file}:{h.line} [{h.anchor}] {h.text}" for h in orphans)
        assert stale == [], f"exemptions matching nothing: {stale}"

    def _fake_repo(self, tmp_path, monkeypatch, files: dict):
        for rel, body in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(body, encoding="utf-8")
        monkeypatch.setattr(registry, "REPO_ROOT", tmp_path)

    def test_an_unregistered_marker_fails(self, tmp_path, monkeypatch):
        self._fake_repo(tmp_path, monkeypatch, {
            "config/new.yaml": "fees:\n  visit_charge: 500   # [PLACEHOLDER]\n",
            "app/new.py": 'GREETING = "DUMMY — hello"\n',
            "scripts/x.py": "PLACEHOLDER_FEE = 3\n",
        })
        orphans, _ = registry.unclaimed()
        assert {(h.file, h.anchor) for h in orphans} == {
            ("config/new.yaml", "fees.visit_charge"), ("app/new.py", "GREETING"), ("scripts/x.py", "PLACEHOLDER_FEE")}

    def test_registering_it_clears_the_failure(self, tmp_path, monkeypatch):
        self._fake_repo(tmp_path, monkeypatch, {"config/new.yaml": "fees:\n  visit_charge: 500   # [PLACEHOLDER]\n"})
        entry = registry.Entry(id="T1", area="pricing", location={"file": "config/new.yaml", "key": "fees"},
                               description="d", dummy_value=500, needed_from_grid_nova="q?", blocking="tunable",
                               probe="yaml_contains_marker")
        monkeypatch.setattr(registry, "load", lambda: registry.Registry(entries=[entry], exemptions=[]))
        assert registry.unclaimed() == ([], [])

    def test_a_stale_exemption_fails(self, tmp_path, monkeypatch):
        self._fake_repo(tmp_path, monkeypatch, {"config/new.yaml": "fees: 1\n"})
        ex = registry.Exemption(file="config/new.yaml", anchor="fees", reason="r")
        monkeypatch.setattr(registry, "load", lambda: registry.Registry(entries=[], exemptions=[ex]))
        assert registry.unclaimed() == ([], [ex])


class TestAnchors:
    def test_yaml(self):
        text = ("# header [PLACEHOLDER]\n"
                "top:\n"
                "  # about child\n"
                "  child: 1   # [PLACEHOLDER]\n"
                "  body: >-\n"
                "    text: not a key\n"
                "  items:\n"
                "    - name: a\n"
                "      rate: 0\n"
                "    - plain\n")
        assert registry.yaml_line_anchors(text) == [
            "top", "top", "top.child", "top.child", "top.body", "top.body", "top.items", "top.items.name",
            "top.items.rate", "top.items"]

    def test_python(self):
        text = ('"""doc"""\n'
                "# about X\n"
                "X = 1\n"
                "\n"
                "def f():\n"
                "    return '[PLACEHOLDER]'\n"
                "class C:\n"
                "    def m(self):\n"
                "        pass\n")
        assert registry.python_line_anchors(text) == ["__doc__", "X", "X", "f", "f", "f", "C", "C.m", "C.m"]


class TestProbes:
    def test_yaml_marker_probe_flips_when_the_value_is_supplied(self, tmp_path, monkeypatch):
        entry = registry.load().by_id["CMP-1"]
        assert registry.evaluate(entry)["resolved"] is False
        shutil.copytree(registry.REPO_ROOT / "config", tmp_path / "config")
        path = tmp_path / "config" / "proposal_content.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace('gstin: "[PLACEHOLDER]"', 'gstin: "19ABCDE1234F1Z5"'),
                        encoding="utf-8")
        monkeypatch.setattr(registry, "REPO_ROOT", tmp_path)
        assert registry.evaluate(entry)["resolved"] is True

    def test_comment_marker_on_a_number_counts(self, tmp_path, monkeypatch):
        entry = registry.load().by_id["E2"]
        assert registry.evaluate(entry)["resolved"] is False
        shutil.copytree(registry.REPO_ROOT / "config", tmp_path / "config")
        path = tmp_path / "config" / "execution_payments.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("# [PLACEHOLDER]\n    material_dispatched", "\n"
                                                                 "    material_dispatched"), encoding="utf-8")
        monkeypatch.setattr(registry, "REPO_ROOT", tmp_path)
        assert registry.evaluate(entry)["resolved"] is False   # seven lines still marked
        path.write_text("\n".join(line.split("#")[0].rstrip() if "stuck" not in line else line
                                  for line in path.read_text(encoding="utf-8").splitlines()), encoding="utf-8")
        assert registry.evaluate(entry)["resolved"] is True

    def test_current_version_is_what_counts(self, tmp_path, monkeypatch):
        entry = registry.load().by_id["ONB-1"]
        shutil.copytree(registry.REPO_ROOT / "config", tmp_path / "config")
        path = tmp_path / "config" / "associate_onboarding.yaml"
        text = path.read_text(encoding="utf-8").replace(
            'current_notice_version: "placeholder-v0"\nnotice_versions:\n',
            'current_notice_version: "v1"\nnotice_versions:\n  v1:\n    text: >-\n      Real notice.\n')
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(registry, "REPO_ROOT", tmp_path)
        assert registry.evaluate(entry)["resolved"] is True   # the old version keeps its marker, as history

    def test_constant_probe(self, monkeypatch):
        entry = registry.load().by_id["PRC-8"]
        assert registry.evaluate(entry)["resolved"] is False
        import app.pricing.engine as engine
        monkeypatch.setattr(engine, "PLACEHOLDER_GST_RATE_PERCENT", 12.0)
        assert registry.evaluate(entry)["resolved"] is True

    def test_setting_probe(self, monkeypatch):
        from app.config import settings
        entry = registry.load().by_id["P7"]
        assert registry.evaluate(entry)["resolved"] is False
        monkeypatch.setattr(settings, "accounts_notify_emails", "accounts@gridnova.in")
        assert registry.evaluate(entry)["resolved"] is False   # Operations still blank
        monkeypatch.setattr(settings, "operations_notify_emails", "ops@gridnova.in")
        assert registry.evaluate(entry)["resolved"] is True

    def test_count_query_probe(self, db_session):
        entry = registry.load().by_id["D1"]
        assert registry.evaluate(entry, db_session)["resolved"] is False   # no catalogue at all
        seed_dummy_data.seed(db_session, "development")
        assert registry.evaluate(entry, db_session)["resolved"] is False   # dummies only
        for row in db_session.execute(select(Equipment)).scalars():
            row.is_active = False
        db_session.add(Equipment(category=EquipmentCategory.module, brand="Real", model_name="R1", is_active=True))
        db_session.commit()
        assert registry.evaluate(entry, db_session)["resolved"] is True

    def test_database_probes_are_unknown_without_a_session(self):
        assert registry.evaluate(registry.load().by_id["A1"])["resolved"] is None

    def test_broken_probe_shows_as_unresolved_with_the_error(self, monkeypatch):
        entry = registry.Entry(id="T", area="pricing", location={"file": "config/nope.yaml", "key": "a"},
                               description="d", dummy_value=1, needed_from_grid_nova="q?", blocking="tunable",
                               probe="yaml_contains_marker")
        state = registry.evaluate(entry)
        assert state["resolved"] is False and "FileNotFoundError" in state["error"]


class TestEndpoints:
    @pytest.mark.parametrize("role", ["admin", "management"])
    def test_readable_by_admin_and_management(self, client, role):
        body = client.get(URL, headers=staff(role)).json()
        assert body["summary"]["total"] == len(registry.load().entries)
        assert set(body["summary"]["by_blocking"]) == {"blocks_launch", "blocks_feature", "tunable"}
        assert body["summary"]["by_blocking"]["blocks_launch"]["unresolved"] >= 1
        item = next(i for a in body["areas"] for i in a["items"] if i["id"] == "P5")
        assert item["resolved"] is False and item["blocking"] == "blocks_launch"
        assert item["location"] == "config/execution_payments.yaml → payments.receipt.company"
        assert item["needed_from_grid_nova"].endswith("?")

    @pytest.mark.parametrize("role", ["sales", "engineer", "operations", "accounts"])
    def test_other_roles_refused(self, client, role):
        assert client.get(URL, headers=staff(role)).status_code == 403

    def test_associates_refused(self, client):
        assert client.get(URL, headers={"X-Associate-Id": "GNA-WB-26-00001"}).status_code == 401

    def test_resolve_a_manual_entry(self, client, db_session):
        r = client.post(f"{URL}/A1/resolve", json={"note": "Confirmed by Tanmoy on a call."}, headers=staff("admin"))
        assert r.status_code == 200, r.text
        assert r.json()["resolved"] is True and r.json()["resolution"]["resolved_by"] == "emp-admin-1"
        assert db_session.execute(select(PlaceholderResolution)).scalar_one().entry_id == "A1"
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "placeholder")).scalar_one()
        assert (audit.entity_id, audit.action) == ("A1", "resolved")
        again = client.post(f"{URL}/A1/resolve", json={"note": "x"}, headers=staff("admin"))
        assert again.status_code == 409 and again.json()["detail"]["code"] == "already_resolved"
        listing = client.get(URL, headers=staff("management")).json()
        assert next(i for a in listing["areas"] for i in a["items"] if i["id"] == "A1")["resolved"] is True

    def test_resolve_rules(self, client):
        assert client.post(f"{URL}/A1/resolve", json={"note": "x"}, headers=staff("management")).status_code == 403
        r = client.post(f"{URL}/P5/resolve", json={"note": "x"}, headers=staff("admin"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_manual"
        assert client.post(f"{URL}/NOPE/resolve", json={"note": "x"}, headers=staff("admin")).status_code == 404
        assert client.post(f"{URL}/A1/resolve", json={"note": ""}, headers=staff("admin")).status_code == 422
        assert client.post(f"{URL}/A1/resolve", json={"note": "   "}, headers=staff("admin")).status_code == 422


class TestDummyCatalogue:
    def test_refuses_in_production(self, db_session):
        with pytest.raises(seed_dummy_data.RefusedInProduction):
            seed_dummy_data.seed(db_session, "production")
        assert db_session.execute(select(Equipment)).first() is None

    def test_seeds_once_and_flags_every_row(self, db_session):
        assert seed_dummy_data.seed(db_session, "development") == 5
        assert seed_dummy_data.seed(db_session, "development") == 0
        rows = db_session.execute(select(Equipment)).scalars().all()
        assert len(rows) == 5 and all(r.is_placeholder and r.brand.startswith("DUMMY") for r in rows)
        modules = [r for r in rows if r.category == EquipmentCategory.module]
        assert sorted(r.is_dcr_compliant for r in modules) == [False, True, True]
        assert sum(r.category == EquipmentCategory.inverter for r in rows) == 2

    def test_equipment_listing_shows_the_flag(self, client, db_session):
        seed_dummy_data.seed(db_session, "development")
        items = client.get("/api/v1/equipment/", headers=staff("admin")).json()
        items = items["items"] if isinstance(items, dict) else items
        assert items and all(i["is_placeholder"] is True for i in items)

    def test_nova_never_names_a_dummy_row(self, db_session, memory):
        db_session.add(Equipment(category=EquipmentCategory.module, brand="Waaree", model_name="WSM-540",
                                 is_active=True, is_placeholder=True))
        db_session.commit()
        seed_dummy_data.seed(db_session, "development")
        terms = memory.active_equipment_terms()
        assert terms == []
        result = g.enforce("We usually install Waaree WSM-540 panels.", allowed_text=COMPANY_FACTS,
                           user_message="", fallback="fallback", approved_equipment=terms, customer_text="")
        assert result.blocked and g.UNAPPROVED_EQUIPMENT in {v["code"] for v in result.violations}
