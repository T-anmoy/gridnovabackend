"""BE-20 §3 — associate loose ends FE-19 found.

  * Upload race: two overlapping uploads for one customer each appended to the same old
    JSON list (site-assessment documents / photo slots, and execution files), so the later
    commit dropped the other's file. Fixed by re-reading the row under a lock.
  * GET /dashboard/associates/{id}/customers — the customers staff can move.
  * duplicate_onboarding names the associate's own existing record; another associate's
    match is never refused nor named.
  * Document and photo-slot names served as {code, label}; the estimate's segments.
  * /public/verify keeps active|inactive only (no status_detail).
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    AssociateStatus, CustomerOnboarding, OnboardingStatus, Project, ProjectExecution, ProjectStage, ProposalInputs,
)
from tests.conftest import TEST_DATABASE_URL
from tests.execution_support import (  # noqa: F401 — fixtures
    BASE, DASH, associate_in_execution, headers, kyc_ready, make_associate, new_customer, onboarding_ready, territories,
)
from tests.onboarding_support import onboarding_body

ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
PG_ONLY = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")


def _attach_document(session, inputs_id: int, n: int) -> None:
    MemoryManager(session).attach_proposal_input_upload(
        inputs_id=inputs_id, target="document", filename=f"doc-{n}.pdf", storage_key=f"k/doc-{n}",
        performed_by="associate:test", document_type="id_proof")


def _attach_photo(session, inputs_id: int, slot: str) -> None:
    MemoryManager(session).attach_proposal_input_upload(
        inputs_id=inputs_id, target="installation_photo", filename=f"{slot}.jpg", storage_key=f"k/{slot}",
        performed_by="associate:test", photo_slot=slot)


# ── Upload race ──────────────────────────────────────────────────────────────────

class TestUploadRace:
    def _inputs_id(self, client, db_session) -> int:
        oid = new_customer(client, make_associate(db_session))
        return db_session.get(CustomerOnboarding, oid).proposal_inputs_id

    def test_an_upload_that_read_the_row_earlier_keeps_the_other_upload(self, client, db_session, onboarding_ready,
                                                                        _isolated_db):
        """The interleaving behind the lost update, made deterministic: request A has the row
        loaded (as every upload route does before storing the file), request B's upload
        commits, then A's lands. Both must remain — documents and photo slots alike."""
        inputs_id = self._inputs_id(client, db_session)
        before = len(db_session.get(ProposalInputs, inputs_id).document_refs or [])
        a, b = _isolated_db(), _isolated_db()
        try:
            held = a.get(ProposalInputs, inputs_id)               # A read the row and holds it (the
            held.document_refs, held.installation_photo_refs      # route does, while the file uploads) …
            _attach_document(b, inputs_id, 1)                     # … B's upload commits …
            _attach_photo(b, inputs_id, "wiring_path")
            b.commit()
            _attach_document(a, inputs_id, 2)                     # … then A's
            _attach_photo(a, inputs_id, "earthing_location")
            a.commit()
        finally:
            a.close()
            b.close()
        db_session.expire_all()
        row = db_session.get(ProposalInputs, inputs_id)
        names = [r["filename"] for r in row.document_refs]
        assert len(names) == before + 2 and {"doc-1.pdf", "doc-2.pdf"} <= set(names)
        assert {"wiring_path", "earthing_location"} <= set(row.installation_photo_refs)

    def test_execution_files_too(self, client, db_session, onboarding_ready, _isolated_db):
        from app.execution import service as execution
        _, oid = associate_in_execution(client, db_session)
        a, b = _isolated_db(), _isolated_db()
        try:
            stale = execution.require(a, a.get(CustomerOnboarding, oid))
            stale.files                                            # A read the row …
            fresh = execution.require(b, b.get(CustomerOnboarding, oid))
            execution.attach_file(b, fresh, kind="photo", name="earthing", storage_key="k/e", filename="e.jpg",
                                  content_type="image/jpeg", uploaded_by="associate:test", extra={})
            b.commit()                                             # … B's upload commits …
            execution.attach_file(a, stale, kind="document", name="dispatch_challan", storage_key="k/d",
                                  filename="d.pdf", content_type="application/pdf", uploaded_by="associate:test",
                                  extra={})
            a.commit()
        finally:
            a.close()
            b.close()
        db_session.expire_all()
        files = db_session.execute(select(ProjectExecution).where(ProjectExecution.onboarding_id == oid)).scalar_one().files
        assert "earthing" in files["photos"] and "dispatch_challan" in files["documents"]

    @PG_ONLY
    def test_simultaneous_uploads_lose_nothing(self, client, db_session, onboarding_ready, _isolated_db):
        inputs_id = self._inputs_id(client, db_session)
        before = len(db_session.get(ProposalInputs, inputs_id).document_refs or [])
        n, barrier, errors = 6, threading.Barrier(6), []

        def run(i):
            session = _isolated_db()
            try:
                held = session.get(ProposalInputs, inputs_id)
                held.document_refs
                barrier.wait()
                _attach_document(session, inputs_id, i)
                session.commit()
            except Exception as exc:  # collected and asserted below
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        db_session.expire_all()
        assert len(db_session.get(ProposalInputs, inputs_id).document_refs) == before + n


# ── An associate's customers ─────────────────────────────────────────────────────

class TestAssociateCustomers:
    def test_lists_open_customers_with_stage(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        first, second = new_customer(client, gna), new_customer(client, gna)
        closed = db_session.get(CustomerOnboarding, second)
        closed.status = OnboardingStatus.closed
        db_session.commit()
        body = client.get(f"{DASH}/associates/{gna}/customers", headers=ADMIN).json()
        o = db_session.get(CustomerOnboarding, first)
        assert body["associate_id"] == gna and body["open_customers"] == 1
        assert body["items"] == [{"onboarding_id": first,
                                  "client_code": db_session.get(Project, o.project_id).reference_id,
                                  "customer_name": body["items"][0]["customer_name"], "status": "draft",
                                  "project_stage": "registered", "open": True}]
        everything = client.get(f"{DASH}/associates/{gna}/customers", params={"include_closed": True},
                                headers=ADMIN).json()
        assert [(i["onboarding_id"], i["open"]) for i in everything["items"]] == [(first, True), (second, False)]

    def test_permission_and_region(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        for role in ("sales", "accounts", "operations", "engineer", "management"):
            r = client.get(f"{DASH}/associates/{gna}/customers",
                           headers={"X-Employee-Id": f"emp-{role}-9", "X-Employee-Role": role})
            assert r.status_code == 403, role
        nm = {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"}
        assert client.get(f"{DASH}/associates/{gna}/customers", headers=nm).status_code == 200
        from app.memory.models import Employee
        emp = db_session.get(Employee, "emp-nm-1")
        emp.regions = ["BIHAR"]
        db_session.commit()
        assert client.get(f"{DASH}/associates/{gna}/customers", headers=nm).status_code == 403


# ── duplicate_onboarding ─────────────────────────────────────────────────────────

class TestDuplicateOnboarding:
    def test_the_owner_is_told_which_record(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        first = new_customer(client, gna, customer_mobile="9830000001")
        r = client.post(BASE, json=onboarding_body(n=99, customer_mobile="9830000001"), headers=headers(gna))
        detail = r.json()["detail"]
        o = db_session.get(CustomerOnboarding, first)
        assert r.status_code == 409 and detail["code"] == "duplicate_onboarding"
        assert detail["existing_onboarding_id"] == first
        assert detail["client_code"] == db_session.get(Project, o.project_id).reference_id

    def test_another_associate_learns_nothing(self, client, db_session, onboarding_ready):
        a, b = make_associate(db_session), make_associate(db_session)
        first = new_customer(client, a, customer_mobile="9830000002")
        r = client.post(BASE, json=onboarding_body(n=98, customer_mobile="9830000002"), headers=headers(b))
        assert r.status_code == 201          # flagged for attribution review, never refused
        o = db_session.get(CustomerOnboarding, first)
        text = r.text
        assert db_session.get(Project, o.project_id).reference_id not in text and a not in text
        assert '"existing_onboarding_id"' not in text


# ── Names as {code, label} ───────────────────────────────────────────────────────

class TestNames:
    def test_onboarding_options(self, client, db_session, onboarding_ready):
        from app.memory.models import INSTALLATION_PHOTO_SLOTS
        from app.pricing.engine import KNOWN_SEGMENTS
        body = client.get(f"{BASE}/options", headers=headers(make_associate(db_session))).json()
        assert all(d["code"] == d["key"] and d["label"] for d in body["documents"])
        assert body["photo_slots"] == list(INSTALLATION_PHOTO_SLOTS)                 # unchanged
        assert [s["code"] for s in body["photo_slot_items"]] == list(INSTALLATION_PHOTO_SLOTS)
        assert body["photo_slot_items"][1] == {"code": "acdb_dcdb_placement", "label": "ACDB / DCDB placement"}
        assert body["estimate_segments"] == list(KNOWN_SEGMENTS) == ["residential", "commercial", "industrial",
                                                                      "government"]

    def test_execution_status(self, client, db_session, onboarding_ready):
        from app.execution import config
        gna, oid = associate_in_execution(client, db_session)
        body = client.get(f"/api/v1/associate/onboardings/{oid}/execution", headers=headers(gna)).json()
        assert body["photo_slots"] == config.photo_slots()                            # unchanged
        assert [i["code"] for i in body["photo_slot_items"]] == config.photo_slots()
        assert [i["code"] for i in body["document_type_items"]] == config.document_types()
        assert all(i["label"] for i in body["photo_slot_items"] + body["document_type_items"])

    def test_a_slot_without_a_label_stops_the_app(self, monkeypatch):
        from app.onboarding import config
        data = {**config.load(), "photo_slot_labels": {"wiring_path": "Wiring path"}}
        with pytest.raises(config.OnboardingConfigError, match="photo_slot_labels"):
            config._validate(data)


# ── Public verify ────────────────────────────────────────────────────────────────

def test_public_verify_still_says_only_active_or_inactive(client, db_session, onboarding_ready):
    """Suspended and deactivated stay "inactive": telling them apart would publish a
    disciplinary state the public flag deliberately does not."""
    from app.memory.models import Associate
    for status in (AssociateStatus.suspended, AssociateStatus.deactivated):
        gna = make_associate(db_session, status=status)
        body = client.get(f"/api/v1/public/verify/{gna}").json()
        assert body["status"] == "inactive" and "status_detail" not in body
        assert set(body) == {"name", "photo_url", "type", "level", "status"}
    assert db_session.execute(select(Associate)).first() is not None
