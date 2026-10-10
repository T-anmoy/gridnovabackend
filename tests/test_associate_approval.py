"""Admin review and approval, the associate lifecycle, seats, the public verification
endpoint, and territory administration."""
import re
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import app.notifications.notifier as notifier_module
from app.associates import service
from app.memory.models import (
    ApplicationStatus, Associate, AssociateApplication, AuditLog, ClerkInviteStatus, TerritoryDistrict,
    TerritoryLevel, TerritorySeat, TerritoryState,
)
from app.notifications.notifier import LogNotifier
from tests.associate_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, ALIPURDUAR_I, ARARIA, ASSOCIATE_APPLY, BR, DASH, KOLKATA, PREFERRED_APPLY, WB,
    approve_flow, associate_form, kyc_ready, preferred_form, real_jpeg, submit, territories,
)
from tests.conftest import seed_employees

SALES = {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"}
MGMT = {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"}


class Capture(LogNotifier):
    def __init__(self):
        self.events = []

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))


@pytest.fixture
def ready(kyc_ready, db_session, monkeypatch):
    seed_employees(db_session, "emp-admin-1")
    capture = Capture()
    monkeypatch.setattr(notifier_module, "notifier", capture)
    with patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub"):
        yield capture


def _apply(client, n=1, url=ASSOCIATE_APPLY, **kw):
    form = associate_form(n, **kw) if url == ASSOCIATE_APPLY else preferred_form(n, **kw)
    r = submit(client, url, form)
    assert r.status_code == 201, r.text
    return r.json()["application_id"]


def _base(kind, app_id):
    return f"{DASH}/associate-applications/{kind}/{app_id}"


class TestReviewStateMachine:
    def test_full_path_with_needs_info_round_trip(self, client, ready):
        app_id = _apply(client)
        base = _base("associate", app_id)
        for status, note in (("under_review", None), ("needs_info", "Cheque is unreadable"), ("under_review", None)):
            r = client.post(f"{base}/status", json={"status": status, "note": note}, headers=ADMIN)
            assert r.status_code == 200 and r.json()["status"] == status
        assert client.get(base, headers=ADMIN).json()["needs_info_note"] == "Cheque is unreadable"

    @pytest.mark.parametrize("path, target", [
        (["under_review", "rejected"], "under_review"),
        ([], "needs_info"),
        ([], "rejected"),
        (["under_review"], "under_review"),
    ])
    def test_illegal_moves_refused(self, client, ready, path, target):
        app_id = _apply(client)
        for status in path:
            note = "reason" if status == "rejected" else None
            client.post(f"{_base('associate', app_id)}/status", json={"status": status, "note": note}, headers=ADMIN)
        r = client.post(f"{_base('associate', app_id)}/status", json={"status": target, "note": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition"

    def test_rejection_requires_a_reason(self, client, ready, db_session):
        app_id = _apply(client)
        base = _base("associate", app_id)
        client.post(f"{base}/status", json={"status": "under_review"}, headers=ADMIN)
        r = client.post(f"{base}/status", json={"status": "rejected", "note": "  "}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "reason_required"
        r = client.post(f"{base}/status", json={"status": "rejected", "note": "PAN name mismatch"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["rejection_reason"] == "PAN name mismatch"

    def test_database_refuses_rejection_without_reason(self, client, ready, db_session):
        app_id = _apply(client)
        row = db_session.get(AssociateApplication, app_id)
        row.status = ApplicationStatus.rejected
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_database_refuses_approval_without_verification_call(self, client, ready, db_session):
        app_id = _apply(client)
        row = db_session.get(AssociateApplication, app_id)
        row.status = ApplicationStatus.approved
        row.decided_by_employee_id = "emp-admin-1"
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_every_transition_is_audited(self, client, ready, db_session):
        app_id = _apply(client)
        approve_flow(client, "associate", app_id)
        actions = [e.action for e in db_session.execute(
            select(AuditLog).where(AuditLog.entity_type == "associate_application",
                                   AuditLog.entity_id == str(app_id)).order_by(AuditLog.id)).scalars()]
        assert actions == ["submitted", "status_under_review", "status_approved"]

    @pytest.mark.parametrize("headers", [SALES, MGMT, {"X-Employee-Id": "e-ops", "X-Employee-Role": "operations"},
                                         {"X-Employee-Id": "e-acc", "X-Employee-Role": "accounts"}])
    def test_admin_only(self, client, ready, headers):
        app_id = _apply(client)
        assert client.get(f"{DASH}/associate-applications", headers=headers).status_code == 403
        assert client.post(f"{_base('associate', app_id)}/status", json={"status": "under_review"},
                           headers=headers).status_code == 403
        assert client.post(f"{_base('associate', app_id)}/approve",
                           json={"verification_call_completed": True, "verification_call_note": "x"},
                           headers=headers).status_code == 403

    def test_list_and_detail_never_expose_secrets(self, client, ready):
        form = associate_form()
        app_id = _apply(client)
        listing = client.get(f"{DASH}/associate-applications", headers=ADMIN).json()
        assert listing["total"] == 1 and listing["items"][0]["application_id"] == app_id
        detail = client.get(_base("associate", app_id), headers=ADMIN)
        assert detail.json()["bank_account_last4"] == form["bank_account_number"][-4:]
        for secret in (form["bank_account_number"], form["aadhaar_number"], "storage_key", "encrypted"):
            assert secret not in detail.text

    def test_document_link(self, client, ready):
        app_id = _apply(client)
        r = client.get(f"{_base('associate', app_id)}/documents/pan_card", headers=ADMIN)
        assert r.status_code == 200 and r.json()["url"].startswith("https://signed.example/")
        assert client.get(f"{_base('associate', app_id)}/documents/nope", headers=ADMIN).status_code == 404


class TestApproval:
    def test_requires_the_verification_call(self, client, ready):
        app_id = _apply(client)
        base = _base("associate", app_id)
        client.post(f"{base}/status", json={"status": "under_review"}, headers=ADMIN)
        r = client.post(f"{base}/approve", json={"verification_call_completed": False,
                                                 "verification_call_note": "x"}, headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "verification_call_required"
        r = client.post(f"{base}/approve", json={"verification_call_completed": True,
                                                 "verification_call_note": "   "}, headers=ADMIN)
        assert r.status_code == 422

    def test_only_from_under_review(self, client, ready):
        app_id = _apply(client)
        r = client.post(f"{_base('associate', app_id)}/approve",
                        json={"verification_call_completed": True, "verification_call_note": "ok"}, headers=ADMIN)
        assert r.status_code == 409

    def test_issues_id_creates_associate_and_notifies(self, client, ready, db_session):
        app_id = _apply(client)
        r = approve_flow(client, "associate", app_id)
        assert r.status_code == 200, r.text
        body = r.json()
        yy = f"{service.today_ist().year % 100:02d}"
        assert body["associate"]["associate_id"] == f"GNA-WB-{yy}-00001"
        assert body["application"]["status"] == "approved"
        assert body["application"]["verification_call_completed"] is True
        assert body["associate"]["capability"] == "sales" and body["associate"]["level"] is None
        assert body["associate"]["clerk_invite_status"] == "skipped"  # no CLERK_SECRET_KEY in tests
        assert body["associate"]["documents"]["id_card"] and body["associate"]["documents"]["welcome_letter"]
        assert ready.events == [("associate_approved", {
            "associate_id": f"GNA-WB-{yy}-00001", "associate_type": "associate", "name": "Test Applicant 1",
            "level": None, "approved_by": "emp-admin-1"})]

    def test_ids_are_sequential_per_state_and_year(self, client, ready):
        yy = f"{service.today_ist().year % 100:02d}"
        ids = []
        for n, state, district, block in ((1, WB, ALIPURDUAR, ALIPURDUAR_I), (2, WB, KOLKATA, None),
                                          (3, BR, ARARIA, 1624)):
            app_id = _apply(client, n, state_lgd_code=str(state), district_lgd_code=str(district),
                            block_lgd_code=str(block) if block else None)
            ids.append(approve_flow(client, "associate", app_id).json()["associate"]["associate_id"])
        assert ids == [f"GNA-WB-{yy}-00001", f"GNA-WB-{yy}-00002", f"GNA-BR-{yy}-00001"]

    def test_atomic_sequence(self, db_session, territories):
        assert [service.next_associate_sequence(db_session, "GNA-WB-26") for _ in range(3)] == [1, 2, 3]
        assert service.next_associate_sequence(db_session, "GNA-BR-26") == 1

    def test_id_format(self):
        assert service.ASSOCIATE_ID_RE.fullmatch("GNA-WB-26-00001")
        assert not service.ASSOCIATE_ID_RE.fullmatch("GNA-W-26-00001")

    def test_associate_id_is_immutable_in_the_orm(self, client, ready, db_session):
        approve_flow(client, "associate", _apply(client))
        associate = db_session.execute(select(Associate)).scalar_one()
        with pytest.raises(ValueError, match="immutable"):
            associate.associate_id = "GNA-WB-26-99999"

    def test_associate_id_is_immutable_in_the_database(self, client, ready, db_session):
        approve_flow(client, "associate", _apply(client))
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(text("UPDATE associates SET associate_id = 'GNA-WB-26-99999'"))
        db_session.rollback()
        # Other columns can still change.
        db_session.execute(text("UPDATE associates SET mobile = '9000000000'"))
        db_session.commit()

    def test_never_an_employee(self, client, ready, db_session):
        from app.memory.models import Employee
        approve_flow(client, "associate", _apply(client))
        assert {e.id for e in db_session.execute(select(Employee)).scalars()} == {"emp-admin-1"}

    def test_clerk_invite_carries_the_associate_claims(self, client, ready, monkeypatch):
        calls = []
        monkeypatch.setattr("app.auth.clerk_client.create_associate_invitation",
                            lambda email, meta, redirect="": calls.append((email, meta)) or (True, "inv_123"))
        body = approve_flow(client, "associate", _apply(client)).json()["associate"]
        assert body["clerk_invite_status"] == "sent"
        assert calls == [("applicant1@example.test",
                          {"principal": "associate", "associate_id": body["associate_id"]})]

    def test_clerk_failure_keeps_the_approval_and_can_be_retried(self, client, ready, monkeypatch):
        from app.auth.clerk_client import ClerkSyncError

        def _fail(*a, **k):
            raise ClerkSyncError("Clerk invitation failed (HTTP 422)")
        monkeypatch.setattr("app.auth.clerk_client.create_associate_invitation", _fail)
        body = approve_flow(client, "associate", _apply(client)).json()
        assert body["application"]["status"] == "approved"
        assert body["associate"]["clerk_invite_status"] == "failed"
        monkeypatch.setattr("app.auth.clerk_client.create_associate_invitation",
                            lambda *a, **k: (True, "inv_retry"))
        r = client.post(f"{DASH}/associates/{body['associate']['associate_id']}/clerk-invite", headers=ADMIN)
        assert r.status_code == 200 and r.json()["clerk_invite_status"] == "sent"
        r = client.post(f"{DASH}/associates/{body['associate']['associate_id']}/clerk-invite", headers=ADMIN)
        assert r.status_code == 409  # already sent

    def test_clerk_api_request_shape(self, monkeypatch):
        """The real client posts to Clerk's invitations endpoint with public_metadata."""
        import httpx
        from app.auth import clerk_client
        from app.config import settings
        sent = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "inv_1"}

        def _post(url, headers, json, timeout):
            sent.update(url=url, auth=headers["Authorization"], body=json)
            return _Resp()
        monkeypatch.setattr(settings, "clerk_secret_key", "sk_test_x")
        monkeypatch.setattr(httpx, "post", _post)
        assert clerk_client.create_associate_invitation(
            "a@x.test", {"principal": "associate", "associate_id": "GNA-WB-26-00001"}, "https://portal/x",
        ) == (True, "inv_1")
        assert sent["url"] == "https://api.clerk.com/v1/invitations"
        assert sent["body"] == {"email_address": "a@x.test", "notify": True, "redirect_url": "https://portal/x",
                                "public_metadata": {"principal": "associate", "associate_id": "GNA-WB-26-00001"}}

    def test_document_failure_keeps_the_approval_and_can_be_regenerated(self, client, ready, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "public_api_base_url", "")
        body = approve_flow(client, "associate", _apply(client)).json()["associate"]
        assert body["documents"]["id_card"] is False and "PUBLIC_API_BASE_URL" in body["documents"]["error"]
        monkeypatch.setattr(settings, "public_api_base_url", "https://api.gridnova.test")
        r = client.post(f"{DASH}/associates/{body['associate_id']}/documents/regenerate", headers=ADMIN)
        assert r.json()["documents"]["id_card"] is True and r.json()["documents"]["error"] is None
        link = client.get(f"{DASH}/associates/{body['associate_id']}/documents/id_card", headers=ADMIN)
        assert link.status_code == 200 and link.json()["filename"] == f"{body['associate_id']}-id-card.pdf"


class TestDocumentsContent:
    def test_id_card_html(self, client, ready, db_session, kyc_ready):
        from app.associates import documents, issuance
        app_id = submit(client, PREFERRED_APPLY, preferred_form(1, level="state_head"),
                        photo=real_jpeg()).json()["application_id"]
        approve_flow(client, "preferred_associate", app_id)
        associate = db_session.execute(select(Associate)).scalar_one()
        details = issuance.card_details(db_session, associate, real_jpeg(), date(2026, 9, 27))
        html = documents.id_card_html(details)
        assert associate.associate_id in html and "Test Applicant 1" in html
        assert "Preferred Associate" in html and "State Head" in html and "WEST BENGAL" in html
        assert "data:image/jpeg;base64," in html and "<svg" in html

    def test_qr_code_encodes_the_verification_url(self, monkeypatch):
        from app.associates import documents
        from app.config import settings
        monkeypatch.setattr(settings, "public_api_base_url", "https://api.gridnova.test/")
        url = documents.verify_url("GNA-WB-26-00001")
        assert url == "https://api.gridnova.test/api/v1/public/verify/GNA-WB-26-00001"
        details = documents.CardDetails("GNA-WB-26-00001", "X", "associate", None, None, None, None,
                                        date(2026, 9, 27))
        # The card embeds exactly the QR symbol for that URL.
        assert documents.qr_svg(url) in documents.id_card_html(details)

    def test_welcome_letter_uses_the_placeholder_not_invented_copy(self, client, ready, db_session):
        from app.associates import documents, issuance, texts
        approve_flow(client, "associate", _apply(client))
        associate = db_session.execute(select(Associate)).scalar_one()
        html = documents.welcome_letter_html(issuance.card_details(db_session, associate, None, date(2026, 9, 27)),
                                             texts.welcome_letter_body())
        assert "[PLACEHOLDER — the welcome letter text is to be supplied by Grid Nova.]" in html

    def test_values_are_html_escaped(self):
        from app.associates import documents
        details = documents.CardDetails("GNA-WB-26-00001", "<script>x</script>", "associate", None, None,
                                        None, None, date(2026, 9, 27))
        html = documents.welcome_letter_html(details, "<b>body</b>")
        assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html

    def test_real_pdf_rendering(self, monkeypatch):
        """Runs only where WeasyPrint's native libraries load (see the report)."""
        try:
            import weasyprint  # noqa: F401
        except OSError:
            pytest.skip("WeasyPrint native libraries (pango/gobject) not loadable here")
        from app.associates import documents
        from app.config import settings
        monkeypatch.setattr(settings, "public_api_base_url", "https://api.gridnova.test")
        details = documents.CardDetails("GNA-WB-26-00001", "Test Person", "preferred_associate", "district_head",
                                        "Alipurduar, WEST BENGAL", real_jpeg(), "image/jpeg", date(2026, 9, 27))
        card = documents.render_pdf(documents.id_card_html(details))
        letter = documents.render_pdf(documents.welcome_letter_html(details, "[PLACEHOLDER]"))
        assert card.startswith(b"%PDF-") and letter.startswith(b"%PDF-")


def _assert_violated(error: IntegrityError, name: str) -> None:
    """Which constraint fired. PostgreSQL names it; SQLite names a CHECK but reports a
    unique index by its columns."""
    message = str(error.orig)
    if "UNIQUE constraint failed" in message:  # SQLite
        columns = {"uq_territory_seats_state_head": "territory_seats.state_id, territory_seats.seat_no",
                   "uq_territory_seats_district_head": "territory_seats.district_id, territory_seats.seat_no"}
        assert columns[name] in message, message
    else:
        assert name in message, message


class TestSeats:
    def test_second_district_head_refused_at_approval(self, client, ready, db_session):
        first = _apply(client, 1, url=PREFERRED_APPLY)
        second = _apply(client, 2, url=PREFERRED_APPLY)  # applied while the seat was free
        assert approve_flow(client, "preferred_associate", first).status_code == 200
        r = approve_flow(client, "preferred_associate", second)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "seat_full"
        assert len(db_session.execute(select(Associate)).scalars().all()) == 1  # nothing half-created
        assert client.get(_base("preferred_associate", second), headers=ADMIN).json()["status"] == "under_review"

    def test_at_most_two_state_heads(self, client, ready):
        ids = [_apply(client, n, url=PREFERRED_APPLY, level="state_head") for n in (1, 2, 3)]
        assert approve_flow(client, "preferred_associate", ids[0]).status_code == 200
        assert approve_flow(client, "preferred_associate", ids[1]).status_code == 200
        r = approve_flow(client, "preferred_associate", ids[2])
        assert r.status_code == 409 and "Both State Head seats" in r.json()["detail"]["message"]
        r = client.get("/api/v1/public/preferred/seat-availability", params={"level": "state_head",
                                                                            "state_lgd_code": WB})
        assert r.json()["available"] is False and r.json()["held"] == 2

    def _seat_parts(self, db_session):
        state = db_session.execute(select(TerritoryState).where(TerritoryState.lgd_code == WB)).scalar_one()
        district = db_session.execute(select(TerritoryDistrict).where(TerritoryDistrict.lgd_code == ALIPURDUAR)).scalar_one()
        return state, district

    def _approve_n(self, client, n, level):
        for i in range(1, n + 1):
            approve_flow(client, "preferred_associate", _apply(client, i, url=PREFERRED_APPLY, level=level))

    def _seatless_holder(self, client, db_session) -> int:
        """A real associate holding no seat (a plain Associate), so a raw seat insert
        can only fail on the seat limit — never on the foreign key."""
        aid = approve_flow(client, "associate", _apply(client, 50)).json()["associate"]["associate_id"]
        return service.get_associate(db_session, aid).id

    def test_database_refuses_a_third_state_head_seat(self, client, ready, db_session):
        """Bypasses the application code entirely: a raw insert of a third held seat
        (seat_no 3) violates the CHECK, and a duplicate seat_no the unique index."""
        self._approve_n(client, 2, "state_head")
        holder_id = self._seatless_holder(client, db_session)
        state, _ = self._seat_parts(db_session)
        for seat_no, violated in ((3, "ck_territory_seats_shape"), (1, "uq_territory_seats_state_head")):
            db_session.add(TerritorySeat(level=TerritoryLevel.state_head, state_id=state.id, seat_no=seat_no,
                                         holder_id=holder_id, claimed_at=datetime.now(timezone.utc)))
            with pytest.raises(IntegrityError) as exc:
                db_session.flush()
            db_session.rollback()
            _assert_violated(exc.value, violated)

    def test_database_refuses_a_second_district_head_seat(self, client, ready, db_session):
        self._approve_n(client, 1, "district_head")
        holder_id = self._seatless_holder(client, db_session)
        state, district = self._seat_parts(db_session)
        for seat_no, violated in ((1, "uq_territory_seats_district_head"), (2, "ck_territory_seats_shape")):
            db_session.add(TerritorySeat(level=TerritoryLevel.district_head, state_id=state.id,
                                         district_id=district.id, seat_no=seat_no, holder_id=holder_id,
                                         claimed_at=datetime.now(timezone.utc)))
            with pytest.raises(IntegrityError) as exc:
                db_session.flush()
            db_session.rollback()
            _assert_violated(exc.value, violated)

    def test_block_seats_are_not_limited(self, client, ready):
        for n in (1, 2, 3):
            app_id = _apply(client, n, url=PREFERRED_APPLY, level="block")
            assert approve_flow(client, "preferred_associate", app_id).status_code == 200

    def test_deactivation_releases_the_seat(self, client, ready):
        first = approve_flow(client, "preferred_associate", _apply(client, 1, url=PREFERRED_APPLY)).json()
        aid = first["associate"]["associate_id"]
        assert first["associate"]["seat"]["seat_no"] == 1
        r = client.post(f"{DASH}/associates/{aid}/status", json={"status": "deactivated", "reason": "Left"},
                        headers=ADMIN)
        assert r.status_code == 200 and r.json()["seat"] is None
        # The seat is free again, and the released seat is kept as history.
        assert approve_flow(client, "preferred_associate",
                            _apply(client, 2, url=PREFERRED_APPLY)).status_code == 200
        seats = client.get(f"{DASH}/territory-seats", params={"include_released": True}, headers=ADMIN).json()["items"]
        assert len(seats) == 2 and seats[0]["released_at"] and "Left" in seats[0]["release_reason"]

    def test_seat_release_needs_a_suspended_holder_and_reinstatement_retakes_it(self, client, ready):
        aid = approve_flow(client, "preferred_associate",
                           _apply(client, 1, url=PREFERRED_APPLY)).json()["associate"]["associate_id"]
        seat_id = client.get(f"{DASH}/territory-seats", headers=ADMIN).json()["items"][0]["seat_id"]
        r = client.post(f"{DASH}/territory-seats/{seat_id}/release", json={"reason": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "holder_active"
        client.post(f"{DASH}/associates/{aid}/status", json={"status": "suspended", "reason": "Audit"}, headers=ADMIN)
        assert client.post(f"{DASH}/territory-seats/{seat_id}/release", json={"reason": "Reassigning"},
                           headers=ADMIN).status_code == 200
        r = client.post(f"{DASH}/associates/{aid}/status", json={"status": "active"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["seat"]["seat_no"] == 1

    def test_reinstatement_refused_when_the_seat_was_filled(self, client, ready):
        aid = approve_flow(client, "preferred_associate",
                           _apply(client, 1, url=PREFERRED_APPLY)).json()["associate"]["associate_id"]
        client.post(f"{DASH}/associates/{aid}/status", json={"status": "suspended", "reason": "Audit"}, headers=ADMIN)
        seat_id = client.get(f"{DASH}/territory-seats", headers=ADMIN).json()["items"][0]["seat_id"]
        client.post(f"{DASH}/territory-seats/{seat_id}/release", json={"reason": "Reassigning"}, headers=ADMIN)
        assert approve_flow(client, "preferred_associate", _apply(client, 2, url=PREFERRED_APPLY)).status_code == 200
        r = client.post(f"{DASH}/associates/{aid}/status", json={"status": "active"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "seat_full"

    def test_seat_admin_only(self, client, ready):
        assert client.get(f"{DASH}/territory-seats", headers=MGMT).status_code == 403


class TestLifecycle:
    def _associate(self, client):
        return approve_flow(client, "associate", _apply(client)).json()["associate"]["associate_id"]

    def test_active_suspended_active_deactivated(self, client, ready, db_session):
        aid = self._associate(client)
        for status, reason in (("suspended", "Complaint"), ("active", None), ("deactivated", "Exit")):
            r = client.post(f"{DASH}/associates/{aid}/status", json={"status": status, "reason": reason},
                            headers=ADMIN)
            assert r.status_code == 200 and r.json()["status"] == status
        actions = [e.action for e in db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "associate", AuditLog.entity_id == aid).order_by(AuditLog.id)).scalars()]
        assert actions[-3:] == ["status_suspended", "status_active", "status_deactivated"]

    def test_deactivation_is_final(self, client, ready):
        aid = self._associate(client)
        client.post(f"{DASH}/associates/{aid}/status", json={"status": "deactivated", "reason": "Exit"}, headers=ADMIN)
        r = client.post(f"{DASH}/associates/{aid}/status", json={"status": "active"}, headers=ADMIN)
        assert r.status_code == 409

    def test_suspension_needs_a_reason(self, client, ready):
        aid = self._associate(client)
        r = client.post(f"{DASH}/associates/{aid}/status", json={"status": "suspended"}, headers=ADMIN)
        assert r.status_code == 422

    def test_associate_portal_and_own_documents(self, client, ready):
        aid = self._associate(client)
        me = client.get("/api/v1/associate/me", headers={"X-Associate-Id": aid})
        assert me.status_code == 200 and me.json()["associate_id"] == aid
        doc = client.get("/api/v1/associate/me/documents/welcome_letter", headers={"X-Associate-Id": aid})
        assert doc.status_code == 200 and doc.json()["url"].startswith("https://signed.example/associates/")
        client.post(f"{DASH}/associates/{aid}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        assert client.get("/api/v1/associate/me", headers={"X-Associate-Id": aid}).status_code == 403


class TestVerifyEndpoint:
    def test_returns_only_the_public_fields(self, client, ready):
        aid = approve_flow(client, "preferred_associate",
                           _apply(client, 1, url=PREFERRED_APPLY)).json()["associate"]["associate_id"]
        r = client.get(f"/api/v1/public/verify/{aid}")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"name", "photo_url", "type", "level", "status"}
        assert body["name"] == "Test Applicant 1" and body["type"] == "preferred_associate"
        assert body["level"] == "district_head" and body["status"] == "active"
        assert body["photo_url"].startswith("https://signed.example/associate-applications/")

    def test_inactive_after_suspension_or_deactivation(self, client, ready):
        aid = approve_flow(client, "associate", _apply(client)).json()["associate"]["associate_id"]
        client.post(f"{DASH}/associates/{aid}/status", json={"status": "suspended", "reason": "x"}, headers=ADMIN)
        assert client.get(f"/api/v1/public/verify/{aid}").json()["status"] == "inactive"

    @pytest.mark.parametrize("bad", ["GNA-WB-26-00001", "nonsense", "GNA-WB-26-1"])
    def test_unknown(self, client, territories, bad):
        assert client.get(f"/api/v1/public/verify/{bad}").status_code == 404

    def test_rate_limited(self, client, territories):
        from app.api.routes.public_associates import _VERIFY_LIMIT
        assert _VERIFY_LIMIT == 10
        codes = [client.get("/api/v1/public/verify/GNA-WB-26-00001").status_code for _ in range(11)]
        assert codes[-1] == 429 and set(codes[:10]) == {404}


class TestTerritoryAdmin:
    def test_add_rename_and_switch_off(self, client, ready, db_session):
        r = client.post(f"{DASH}/territories/blocks", json={"district_lgd_code": KOLKATA, "lgd_code": 999001,
                                                             "name": "Test Block"}, headers=ADMIN)
        assert r.status_code == 201 and r.json()["district_lgd_code"] == KOLKATA
        dup = client.post(f"{DASH}/territories/blocks", json={"district_lgd_code": KOLKATA, "lgd_code": 999001,
                                                               "name": "Again"}, headers=ADMIN)
        assert dup.status_code == 409
        r = client.patch(f"{DASH}/territories/blocks/{ALIPURDUAR_I}", json={"is_active": False}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["is_active"] is False
        public = client.get(f"/api/v1/public/territories/districts/{ALIPURDUAR}/blocks").json()["items"]
        assert ALIPURDUAR_I not in {b["lgd_code"] for b in public}
        admin = client.get(f"{DASH}/territories/blocks", params={"parent_lgd_code": ALIPURDUAR},
                           headers=ADMIN).json()["items"]
        assert ALIPURDUAR_I in {b["lgd_code"] for b in admin}
        # A switched-off block can no longer be chosen on a registration form.
        r = submit(client, ASSOCIATE_APPLY, associate_form(9))
        assert r.status_code == 422
        actions = {e.action for e in db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "territory_block")).scalars()}
        assert {"created", "updated"} <= actions

    def test_admin_only(self, client, ready):
        r = client.post(f"{DASH}/territories/districts", json={"state_lgd_code": WB, "lgd_code": 5, "name": "x"},
                        headers=SALES)
        assert r.status_code == 403
