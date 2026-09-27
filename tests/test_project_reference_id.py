"""Project.reference_id — GNE-{STATE}-{TYPE}-{YEAR}-{SEQUENCE}.

Covers the mapping tables, the generation function, wiring through
MemoryManager.create_project(), and — the one thing correctness-in-isolation
can't prove — that the sequence is genuinely race-condition-safe under real
concurrent writers, not just correct when called one at a time.
"""
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.memory import reference_id as refid
from app.memory.db import Base
from app.memory.memory_manager import MemoryManager
from app.memory.models import Project, ProjectFamily


class TestStateCode:
    def test_confirmed_primary_states(self):
        assert refid.state_code("West Bengal") == "WB"
        assert refid.state_code("Bihar") == "BR"

    def test_case_and_whitespace_insensitive(self):
        assert refid.state_code("  west bengal ") == "WB"
        assert refid.state_code("BIHAR") == "BR"

    def test_every_state_and_ut_has_its_iso_code(self):
        assert refid.state_code("Odisha") == "OD"
        assert refid.state_code("Uttar Pradesh") == "UP"
        assert refid.state_code("Uttarakhand") == "UK"
        assert refid.state_code("Telangana") == "TS"
        assert refid.state_code("Chhattisgarh") == "CG"
        assert refid.state_code("Dadra and Nagar Haveli and Daman and Diu") == "DH"
        assert len(refid.STATE_CODES) == 36  # 28 states + 8 union territories
        assert len(set(refid.STATE_CODES.values())) == 36

    def test_no_two_letter_prefix_collision_remains(self):
        """The old fallback took a name's first two letters: Uttar Pradesh -> "UT"."""
        for name, code in refid.STATE_CODES.items():
            assert refid.state_code(name.title()) == code

    def test_former_names_and_spellings(self):
        assert refid.state_code("Orissa") == "OD"
        assert refid.state_code("Uttaranchal") == "UK"
        assert refid.state_code("Pondicherry") == "PY"
        assert refid.state_code("Jammu & Kashmir") == "JK"
        assert refid.state_code("Daman and Diu") == "DH"
        assert refid.state_code("Bihār") == "BR"

    def test_codes_are_accepted_as_input(self):
        assert refid.state_code("wb") == "WB"
        assert refid.state_code("IN-UP") == "UP"

    def test_unrecognised_input_is_the_unknown_marker(self):
        assert refid.state_code("Atlantis") == "XX"
        assert refid.state_code("Ut") == "XX"

    def test_missing_state_uses_the_unknown_marker(self):
        assert refid.state_code(None) == "XX"
        assert refid.state_code("") == "XX"


class TestTypeCode:
    def test_every_project_family_value_is_mapped(self):
        for family in ProjectFamily:
            code = refid.type_code(family)
            assert code != "GEN", f"{family} has no explicit mapping"
            assert len(code) == 3

    def test_accepts_the_raw_string_value_too(self):
        assert refid.type_code("rooftop_solar") == refid.type_code(ProjectFamily.rooftop_solar)

    def test_unknown_or_missing_uses_the_general_marker(self):
        assert refid.type_code(None) == "GEN"
        assert refid.type_code("not_a_real_family") == "GEN"


class TestGenerateReferenceId:
    def test_shape(self, db_session):
        ref = refid.generate_reference_id(
            db_session, state="West Bengal", project_family=ProjectFamily.rooftop_solar, year=2026,
        )
        assert ref == "GNE-WB-ROF-2026-00001"

    def test_sequence_increments_within_the_same_bucket(self, db_session):
        first = refid.generate_reference_id(
            db_session, state="Bihar", project_family=ProjectFamily.ground_mounted, year=2026,
        )
        second = refid.generate_reference_id(
            db_session, state="Bihar", project_family=ProjectFamily.ground_mounted, year=2026,
        )
        assert first == "GNE-BR-GRM-2026-00001"
        assert second == "GNE-BR-GRM-2026-00002"

    def test_sequence_resets_per_bucket_not_globally(self, db_session):
        refid.generate_reference_id(db_session, state="West Bengal", project_family=ProjectFamily.rooftop_solar, year=2026)
        refid.generate_reference_id(db_session, state="West Bengal", project_family=ProjectFamily.rooftop_solar, year=2026)
        # A different state+type in the same year starts its own count at 1,
        # independent of how many WB/ROF projects already exist.
        other = refid.generate_reference_id(db_session, state="Bihar", project_family=ProjectFamily.consulting, year=2026)
        assert other == "GNE-BR-CON-2026-00001"

    def test_sequence_resets_per_year(self, db_session):
        refid.generate_reference_id(db_session, state="West Bengal", project_family=ProjectFamily.water_pump, year=2026)
        next_year = refid.generate_reference_id(db_session, state="West Bengal", project_family=ProjectFamily.water_pump, year=2027)
        assert next_year == "GNE-WB-PMP-2027-00001"


class TestCreateProjectWiring:
    def test_new_project_gets_a_reference_id(self, memory, db_session):
        customer = memory.get_or_create_customer(email="ref-wiring@example.com", name="C")
        db_session.commit()
        project = memory.create_project(customer_id=customer.id, state="West Bengal", project_family=ProjectFamily.rooftop_solar)
        db_session.commit()
        assert project.reference_id.startswith("GNE-WB-ROF-")

    def test_project_with_no_state_or_family_still_gets_a_valid_id(self, memory, db_session):
        customer = memory.get_or_create_customer(email="ref-empty@example.com", name="C")
        db_session.commit()
        project = memory.create_project(customer_id=customer.id)
        db_session.commit()
        assert project.reference_id.startswith("GNE-XX-GEN-")

    def test_reference_id_is_unique_across_two_projects(self, memory, db_session):
        customer = memory.get_or_create_customer(email="ref-unique@example.com", name="C")
        db_session.commit()
        p1 = memory.create_project(customer_id=customer.id)
        p2 = memory.create_project(customer_id=customer.id)
        db_session.commit()
        assert p1.reference_id != p2.reference_id

    def test_chat_created_leads_get_a_referenced_project(self, client, memory):
        r = client.post("/api/v1/chat/message", json={"message": "hi, thinking about solar"})
        assert r.status_code == 200
        lead = memory.get_lead(r.json()["lead_id"])
        project = memory._db.get(Project, lead.project_id)
        assert project.reference_id.startswith("GNE-")

    def test_reference_id_is_never_recomputed_by_an_unrelated_update(self, memory, db_session):
        customer = memory.get_or_create_customer(email="ref-immutable@example.com", name="C")
        db_session.commit()
        project = memory.create_project(customer_id=customer.id)
        db_session.commit()
        original = project.reference_id

        memory.update_project(project.id, performed_by="test", roof_type="RCC")
        db_session.commit()
        assert memory._db.get(Project, project.id).reference_id == original


class TestConcurrentCreationIsRaceConditionSafe:
    """The claim under test: two projects created at (as close to) the same
    instant as this process can arrange, for the SAME bucket, must never
    receive the same sequence number. Uses a real file-backed sqlite database
    (not :memory:, whose per-connection isolation would make "concurrency"
    meaningless) with independent threads and independent sessions/connections
    — genuine concurrent writers, not one connection serialising itself.
    """

    def test_twenty_concurrent_creations_in_the_same_bucket_are_all_distinct(self, tmp_path):
        db_path = tmp_path / "concurrency_test.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)

        results = []
        errors = []
        lock = threading.Lock()

        def _create_one():
            session = SessionLocal()
            try:
                mem = MemoryManager(session)
                customer = mem.get_or_create_customer(email=f"conc-{threading.get_ident()}@example.com", name="C")
                session.commit()
                project = mem.create_project(
                    customer_id=customer.id, state="West Bengal", project_family=ProjectFamily.rooftop_solar,
                )
                session.commit()
                with lock:
                    results.append(project.reference_id)
            except Exception as exc:  # pragma: no cover - failure path under test
                with lock:
                    errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=_create_one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"concurrent creation raised: {errors}"
        assert len(results) == 20
        assert len(set(results)) == 20, "collision: two threads received the same reference_id"

        sequences = sorted(int(r.rsplit("-", 1)[1]) for r in results)
        assert sequences == list(range(1, 21)), "sequence has a gap or a duplicate"
