"""BE-5 Phase 3 — service requests, work-queue counts, the overdue job, the home feed."""
import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.handover import config
from app.memory.models import Associate, AuditLog, CustomerOnboarding, Employee, EmployeeRole, ServiceRequest
from app.storage import r2
from tests.handover_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, ENG, MGMT, OPS, SALES, catalogue, hand_url, headers, kyc_ready, notifications, onboarding_ready,
    png, prepare, real_handover_text, record_handover, stub_pdf, territories,
)

SR = "/api/v1/dashboard/service-requests"
PUBLIC = "/api/v1/public/handover"
FORM = {"category": "no_generation", "description": "Nothing is generating since Monday"}


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    store = {}
    monkeypatch.setattr(r2, "upload_bytes", lambda key, body, ct: store.__setitem__(key, body) or key)
    monkeypatch.setattr(r2, "download_bytes", lambda key: store[key])
    monkeypatch.setattr(r2, "signed_get_url", lambda key, filename=None, as_attachment=False: (
        "https://signed.example/x", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)))
    from app.api.routes import public_handover
    public_handover._read_log._hits.clear()
    public_handover._write_log._hits.clear()
    return store


def handed(client, db, catalogue):
    oid = prepare(client, db, catalogue)
    assert record_handover(client, oid).status_code == 200
    return oid


def staff_raise(client, oid, who=OPS, **extra):
    return client.post(f"/api/v1/dashboard/onboardings/{oid}/service-requests", data={**FORM, **extra}, headers=who)


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (26.0, 29.0, 0.0), 3: "E", 4: (89.0, 31.0, 0.0)})
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


class TestRaise:
    def test_needs_a_handed_over_project(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue)
        r = staff_raise(client, oid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_handed_over"

    def test_staff_raise_computes_warranty_and_notifies(self, client, db_session, onboarding_ready, catalogue,
                                                        notifications):
        oid = handed(client, db_session, catalogue)
        r = staff_raise(client, oid, ENG, contact_phone="9000000000")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "open" and body["within_warranty"] is True and body["raised_via"] == "staff"
        assert body["sla_due_at"] is None and body["overdue"] is False
        assert [h["kind"] for h in body["history"]] == ["raised"]
        assert notifications.of("service_request_raised")[0]["within_warranty"] is True

    def test_outside_warranty(self, client, db_session, onboarding_ready, catalogue):
        from app.memory.models import ProjectWarranty
        oid = handed(client, db_session, catalogue)
        w = db_session.execute(select(ProjectWarranty)).scalar_one()
        w.system_warranty_end = w.handover_date - timedelta(days=1)
        db_session.commit()
        assert staff_raise(client, oid).json()["within_warranty"] is False

    def test_validation(self, client, db_session, onboarding_ready, catalogue):
        oid = handed(client, db_session, catalogue)
        assert staff_raise(client, oid, description="   ").json()["detail"]["code"] == "invalid_description"
        assert staff_raise(client, oid, category="bogus").status_code == 422
        for who in (MGMT, SALES, ACCOUNTS):
            assert staff_raise(client, oid, who).status_code == 403

    def test_photos_are_typed_by_bytes_and_limited(self, client, db_session, onboarding_ready, catalogue):
        oid = handed(client, db_session, catalogue)
        url = f"/api/v1/dashboard/onboardings/{oid}/service-requests"
        bad = client.post(url, data=FORM, files=[("files", ("a.jpg", b"not an image", "image/jpeg"))], headers=OPS)
        assert bad.status_code == 422
        many = [("files", (f"{i}.png", png(), "image/png")) for i in range(config.service()["max_photos"] + 1)]
        assert client.post(url, data=FORM, files=many, headers=OPS).json()["detail"]["code"] == "too_many_photos"
        ok = client.post(url, data=FORM, files=[("files", ("a.png", png(), "image/png"))], headers=OPS)
        assert ok.status_code == 201 and ok.json()["photo_count"] == 1
        ref = ok.json()["photos"][0]["file_ref"]
        assert client.get(f"{SR}/{ok.json()['request_id']}/photos/{ref}", headers=OPS).status_code == 200

    def test_associate_owner_raises_and_reads(self, client, db_session, onboarding_ready, catalogue):
        oid = handed(client, db_session, catalogue)
        owner = db_session.execute(select(Associate)).scalars().first().associate_id
        url = f"/api/v1/associate/onboardings/{oid}/service-requests"
        r = client.post(url, data=FORM, headers=headers(owner))
        assert r.status_code == 201 and r.json()["raised_via"] == "associate" and "history" not in r.json()
        listing = client.get(url, headers=headers(owner)).json()
        assert len(listing["items"]) == 1 and listing["can_raise"] is True
        pack = client.get(f"/api/v1/associate/onboardings/{oid}/handover", headers=headers(owner)).json()
        assert pack["handover_recorded"] and "link" not in pack and pack["can_raise_service_request"] is True
        assert client.post(url, data=FORM, headers={"X-Associate-Id": "GNA-NOPE"}).status_code in (401, 403)


class TestCustomerLink:
    def _token(self, client, db, catalogue, monkeypatch):
        monkeypatch.setattr("app.config.settings.public_api_base_url", "https://api.example")
        oid = handed(client, db, catalogue)
        token = client.post(hand_url(oid, "/link"), json={"send_email": False}, headers=OPS)
        return oid, token

    def _link(self, client, db, catalogue, monkeypatch):
        from app.handover import link
        from app.memory.models import HandoverPack
        oid = handed(client, db, catalogue)
        pack = db.execute(select(HandoverPack)).scalar_one()
        token = link.issue(db, pack, "t")
        db.commit()
        return oid, token

    def test_customer_raises_with_gps_stripped(self, client, db_session, onboarding_ready, catalogue, storage,
                                               monkeypatch):
        _, token = self._link(client, db_session, catalogue, monkeypatch)
        r = client.post(f"{PUBLIC}/{token}/service-requests", data=FORM,
                        files=[("files", ("p.jpg", jpeg_with_gps(), "image/jpeg"))])
        assert r.status_code == 201 and "noindex" in r.headers["x-robots-tag"]
        body = r.json()
        assert body["status"] == "open" and "raised_by" not in body and "photos" not in body
        req = db_session.execute(select(ServiceRequest)).scalar_one()
        assert req.raised_via == "customer"
        stored = storage[req.photos[0]["storage_key"]]
        assert not Image.open(io.BytesIO(stored)).getexif().get_ifd(0x8825)
        listing = client.get(f"{PUBLIC}/{token}/service-requests").json()["items"]
        assert [i["request_id"] for i in listing] == [body["request_id"]]

    def test_customer_upload_rejects_non_images_and_bad_tokens(self, client, db_session, onboarding_ready, catalogue,
                                                               monkeypatch):
        _, token = self._link(client, db_session, catalogue, monkeypatch)
        r = client.post(f"{PUBLIC}/{token}/service-requests", data=FORM,
                        files=[("files", ("p.jpg", b"%PDF-1.4 hello", "image/jpeg"))])
        assert r.status_code == 422
        assert client.post(f"{PUBLIC}/{'z' * 43}/service-requests", data=FORM).status_code == 404

    def test_customer_writes_are_rate_limited(self, client, db_session, onboarding_ready, catalogue, monkeypatch):
        _, token = self._link(client, db_session, catalogue, monkeypatch)
        codes = [client.post(f"{PUBLIC}/{token}/service-requests", data=FORM).status_code for _ in range(6)]
        assert codes == [201] * 5 + [429]


class TestHandling:
    def _req(self, client, db, catalogue):
        oid = handed(client, db, catalogue)
        return oid, staff_raise(client, oid).json()["request_id"]

    def test_status_machine_and_history(self, client, db_session, onboarding_ready, catalogue):
        oid, rid = self._req(client, db_session, catalogue)
        assert client.post(f"{SR}/{rid}/status", json={"status": "resolved"}, headers=OPS).json()["detail"]["code"] == \
            "note_required"
        assert client.post(f"{SR}/{rid}/status", json={"status": "in_progress"}, headers=ENG).status_code == 200
        r = client.post(f"{SR}/{rid}/status", json={"status": "resolved", "note": "Replaced fuse"}, headers=ENG)
        assert r.status_code == 200 and r.json()["closed_at"] and r.json()["resolution_note"] == "Replaced fuse"
        again = client.post(f"{SR}/{rid}/status", json={"status": "in_progress"}, headers=OPS)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "invalid_transition"
        assert [h["kind"] for h in r.json()["history"]] == ["raised", "status", "status"]
        assert client.post(f"{SR}/{rid}/status", json={"status": "resolved", "note": "x"}, headers=MGMT).status_code == 403

    def test_assign_and_notes(self, client, db_session, onboarding_ready, catalogue):
        oid, rid = self._req(client, db_session, catalogue)
        db_session.add(Employee(id="emp-eng-9", name="E", email="e9@example.com", role=EmployeeRole.engineer))
        db_session.add(Employee(id="emp-acc-9", name="A", email="a9@example.com", role=EmployeeRole.accounts))
        db_session.commit()
        assert client.post(f"{SR}/{rid}/assign", json={"assignee_id": "emp-acc-9"}, headers=OPS).json()["detail"]["code"] \
            == "invalid_assignee"
        assert client.post(f"{SR}/{rid}/assign", json={"assignee_id": "emp-eng-9"}, headers=OPS).json()["assignee_id"] \
            == "emp-eng-9"
        r = client.post(f"{SR}/{rid}/notes", json={"note": "Called the customer"}, headers=ENG)
        assert r.json()["history"][-1]["note"] == "Called the customer"
        mine = client.get(SR, params={"assignee": "unassigned"}, headers=OPS).json()
        assert mine["total"] == 0

    def test_history_is_append_only(self, client, db_session, onboarding_ready, catalogue):
        oid, rid = self._req(client, db_session, catalogue)
        for sql in ("UPDATE service_request_events SET note = 'x'", "DELETE FROM service_request_events"):
            with pytest.raises(Exception, match="append-only"):
                db_session.execute(text(sql))
            db_session.rollback()

    def test_queue_filters_and_scoping(self, client, db_session, onboarding_ready, catalogue):
        oid, rid = self._req(client, db_session, catalogue)
        q = client.get(SR, params={"open": "true", "within_warranty": "true"}, headers=MGMT).json()
        assert q["total"] == 1 and q["items"][0]["customer_name"] and q["items"][0]["project_reference_id"]
        assert client.get(SR, params={"overdue": "true"}, headers=OPS).json()["total"] == 0
        assert client.get(SR, params={"status": "resolved"}, headers=OPS).json()["total"] == 0
        assert client.get(SR, headers=ACCOUNTS).status_code == 403
        assert client.get(f"{SR}/{rid}", headers=ACCOUNTS).status_code == 403
        assert client.get(f"{SR}/9999", headers=OPS).status_code == 404


class TestSlaJobAndQueues:
    def test_overdue_job_alerts_once(self, client, db_session, onboarding_ready, catalogue, notifications, monkeypatch):
        monkeypatch.setitem(config.service(), "sla_hours", 4)
        oid = handed(client, db_session, catalogue)
        rid = staff_raise(client, oid).json()["request_id"]
        job = "/api/v1/dashboard/internal/check-service-requests"
        assert client.post(job, headers=ADMIN).json() == {"overdue": 0, "newly_alerted": 0}
        req = db_session.get(ServiceRequest, rid)
        req.sla_due_at = req.sla_due_at - timedelta(hours=10)
        db_session.commit()
        assert client.get(SR, params={"overdue": "true"}, headers=OPS).json()["total"] == 1
        assert client.post(job, headers=ADMIN).json() == {"overdue": 1, "newly_alerted": 1}
        assert client.post(job, headers=ADMIN).json() == {"overdue": 1, "newly_alerted": 0}
        assert len(notifications.of("service_requests_overdue")) == 1

    def test_no_sla_means_never_overdue(self, client, db_session, onboarding_ready, catalogue):
        oid = handed(client, db_session, catalogue)
        staff_raise(client, oid)
        r = client.post("/api/v1/dashboard/internal/check-service-requests", headers=ADMIN)
        assert r.json() == {"overdue": 0, "newly_alerted": 0}

    def test_work_queue_counts(self, client, db_session, onboarding_ready, catalogue):
        wait = prepare(client, db_session, catalogue, ticks=False)
        done = prepare(client, db_session, catalogue)
        record_handover(client, done)
        staff_raise(client, done)
        q = client.get("/api/v1/dashboard/work-queues", headers=OPS).json()
        assert q["handover"] == {"awaiting_handover": 1, "packs_incomplete": 1}
        assert q["service_requests"] == {"open": 1, "overdue": 0}
        assert "handover" not in client.get("/api/v1/dashboard/work-queues", headers=ACCOUNTS).json()

    def test_work_queue_query_budget(self, client, db_session, onboarding_ready, catalogue):
        from tests.test_query_budgets import count_queries
        from app.handover import queues
        from app.auth.dependencies import AuthenticatedEmployee
        staff_ids = []
        for _ in range(3):
            oid = prepare(client, db_session, catalogue)
            record_handover(client, oid)
            staff_raise(client, oid)
        emp = db_session.get(Employee, "emp-ops-1") or type("E", (), {"role": EmployeeRole.operations})()
        emp = type("E", (), {"role": EmployeeRole.operations, "employee_id": "emp-ops-1"})()
        with count_queries() as small:
            queues.counts(db_session, emp)
        for _ in range(3):
            oid = prepare(client, db_session, catalogue)
            record_handover(client, oid)
            staff_raise(client, oid)
        with count_queries() as big:
            queues.counts(db_session, emp)
        assert len(small) == len(big) == 2


class TestHomeFeed:
    def test_associate_sees_open_then_closed_requests(self, client, db_session, onboarding_ready, catalogue):
        oid = prepare(client, db_session, catalogue)
        owner = db_session.execute(select(Associate)).scalars().first().associate_id
        kinds = lambda: {i["kind"] for i in client.get("/api/v1/associate/home", headers=headers(owner)).json()["items"]}   # noqa: E731
        assert "handover_pending" in kinds()
        record_handover(client, oid)
        assert "handover_pending" not in kinds()
        rid = staff_raise(client, oid).json()["request_id"]
        item = [i for i in client.get("/api/v1/associate/home", headers=headers(owner)).json()["items"]
                if i["kind"] == "service_request_open"][0]
        assert item["message_key"] == "todo.service_request_open" and item["params"]["request_id"] == rid
        client.post(f"{SR}/{rid}/status", json={"status": "resolved", "note": "Done"}, headers=OPS)
        assert "service_request_closed" in kinds()
        req = db_session.get(ServiceRequest, rid)
        req.closed_at = req.closed_at - timedelta(days=30)
        db_session.commit()
        assert "service_request_closed" not in kinds()
