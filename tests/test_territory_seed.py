"""The territory master loader (app/associates/territory_seed.py) and its data files."""
import shutil

import pytest
from sqlalchemy import func, select

from app.associates.territory_seed import DATA_DIR, TerritoryDataError, load_files, seed_territories
from app.memory.models import AuditLog, TerritoryBlock, TerritoryDistrict, TerritoryState


class TestShippedDataFiles:
    def test_counts(self):
        states, districts, blocks = load_files()
        assert {s["iso_code"] for s in states} == {"WB", "BR"}
        by_state = {}
        for d in districts:
            by_state[d["state_lgd_code"]] = by_state.get(d["state_lgd_code"], 0) + 1
        assert by_state == {"10": 38, "19": 23}
        bihar_districts = {d["district_lgd_code"] for d in districts if d["state_lgd_code"] == "10"}
        assert sum(1 for b in blocks if b["district_lgd_code"] in bihar_districts) == 534
        assert len(blocks) == 879  # West Bengal 345 (IGOD's count), Bihar 534

    def test_kalimpong_matches_lgd(self):
        """Code 2862 is Lava and 7354 does not exist (live LGD portal, 2026-09-27);
        Kalimpong has exactly IGOD's four blocks."""
        blocks = {int(b["block_lgd_code"]): b["block_name"] for b in load_files()[2] if b["district_lgd_code"] == "702"}
        assert blocks == {2859: "GORUBATHAN", 2861: "KALIMPONG-I", 2862: "Lava", 7355: "Pedong"}

    def test_state_codes_agree_with_the_reference_id_table(self):
        from app.memory.reference_id import state_code
        for s in load_files()[0]:
            assert state_code(s["state_name"]) == s["iso_code"]


class TestLoader:
    def test_loads_everything_and_is_idempotent(self, db_session):
        first = seed_territories(db_session)
        db_session.commit()
        assert first.inserted == {"states": 2, "districts": 61, "blocks": 879}
        second = seed_territories(db_session)
        db_session.commit()
        assert second.inserted == {"states": 0, "districts": 0, "blocks": 0}
        assert second.updated == {"states": 0, "districts": 0, "blocks": 0}
        assert db_session.execute(select(func.count(TerritoryBlock.id))).scalar_one() == 879

    def test_a_changed_name_is_updated_and_audited_never_deleted(self, db_session, tmp_path):
        data = tmp_path / "d"
        shutil.copytree(DATA_DIR, data)
        seed_territories(db_session, data)
        db_session.commit()
        text = (data / "districts.csv").read_text().replace("10,188,ARARIA", "10,188,ARARIA RENAMED")
        (data / "districts.csv").write_text(text)
        # Dropping a block from the file must not delete its row.
        lines = (data / "blocks.csv").read_text().splitlines()
        (data / "blocks.csv").write_text("\n".join(l for l in lines if not l.startswith("188,1625,")) + "\n")
        result = seed_territories(db_session, data)
        db_session.commit()
        assert result.updated["districts"] == 1
        assert db_session.execute(select(TerritoryDistrict).where(TerritoryDistrict.lgd_code == 188)).scalar_one().name == "ARARIA RENAMED"
        assert db_session.execute(select(TerritoryBlock).where(TerritoryBlock.lgd_code == 1625)).scalar_one()
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "seed_updated")).scalar_one()

    @pytest.mark.parametrize("file, bad_line, message", [
        ("blocks.csv", "99999,1,ORPHAN", "names a district not in"),
        ("districts.csv", "99,5000,NOWHERE", "names a state not in"),
        ("blocks.csv", "188,1624,DUPLICATE", "duplicate block_lgd_code"),
    ])
    def test_inconsistent_files_refused_before_any_write(self, db_session, tmp_path, file, bad_line, message):
        data = tmp_path / "d"
        shutil.copytree(DATA_DIR, data)
        with (data / file).open("a") as fh:
            fh.write(bad_line + "\n")
        with pytest.raises(TerritoryDataError, match=message):
            seed_territories(db_session, data)
        assert db_session.execute(select(func.count(TerritoryState.id))).scalar_one() == 0
