"""Sentry events never carry KYC values (Aadhaar, PAN, bank account, IFSC).

Sentry is initialised for real here — with the same options app.main uses — but
with a recording transport instead of a DSN's network one, then torn down so the
rest of the suite still runs with Sentry off (tests/test_sentry_isolation.py)."""
import json
from contextlib import contextmanager

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from app.security.sentry_scrub import REDACTED, sentry_init_options
from tests.associate_support import (  # noqa: F401 — fixtures
    ADMIN, ASSOCIATE_APPLY, PREFERRED_APPLY, approve_flow, associate_form, files, kyc_ready,
    preferred_form, territories,
)


class RecordingTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        event = envelope.get_event() or envelope.get_transaction_event()
        if event is not None:
            self.events.append(event)


@contextmanager
def sentry_recording():
    transport = RecordingTransport()
    options = sentry_init_options(dsn="https://public@o0.ingest.sentry.io/0", environment="test")
    sentry_sdk.init(**options, transport=transport)
    try:
        yield transport.events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)
        assert not sentry_sdk.is_initialized()


def _serialized(events) -> str:
    return json.dumps(events, default=str)


@pytest.fixture
def failing_storage(monkeypatch):
    """Storage that fails mid-registration — an unhandled exception inside the
    request, after the whole KYC form has been read and parsed."""
    from app.storage import r2

    def _boom(key, body, content_type):
        raise RuntimeError("R2 exploded")
    monkeypatch.setattr(r2, "upload_bytes", _boom)


class TestFailedRegistrationEvent:
    @pytest.mark.parametrize("url, build", [(ASSOCIATE_APPLY, associate_form), (PREFERRED_APPLY, preferred_form)])
    def test_event_has_no_kyc_values(self, kyc_ready, failing_storage, url, build):
        from fastapi.testclient import TestClient
        from app.main import app

        form = build(7)
        with sentry_recording() as events:
            r = TestClient(app, raise_server_exceptions=False).post(url, data=form, files=files())
        assert r.status_code == 500
        errors = [e for e in events if e.get("exception")]
        assert errors, "the failed registration should have produced a Sentry error event"
        assert any("R2 exploded" in json.dumps(e["exception"]) for e in errors)

        dumped = _serialized(events)
        assert form["aadhaar_number"] not in dumped
        assert form["aadhaar_number"][-8:] not in dumped  # not even a long fragment
        assert form["pan"] not in dumped
        assert form["bank_account_number"] not in dumped
        for event in errors:
            request = event.get("request", {})
            assert "data" not in request or request["data"] in (None, "", REDACTED, {})

    def test_failed_approval_event_has_no_kyc_values(self, client, kyc_ready, db_session, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        from tests.conftest import seed_employees

        seed_employees(db_session, "emp-admin-1")
        form = associate_form(8)
        app_id = client.post(ASSOCIATE_APPLY, data=form, files=files()).json()["application_id"]

        def _boom(*a, **k):
            raise RuntimeError("issuance exploded")
        monkeypatch.setattr("app.associates.service.issue_associate_id", _boom)
        with sentry_recording() as events:
            approve_flow(TestClient(app, raise_server_exceptions=False), "associate", app_id)
        assert any(e.get("exception") for e in events)
        dumped = _serialized(events)
        for secret in (form["aadhaar_number"], form["pan"], form["bank_account_number"]):
            assert secret not in dumped


class TestScrubber:
    """The before_send hook on hand-built events: every place a value could sit."""

    def test_request_data_keys_redacted(self):
        from app.security.sentry_scrub import scrub_event
        from tests.associate_support import aadhaar

        event = {"request": {"data": {"aadhaar_number": aadhaar(), "pan": "ABCPE1234F",
                                      "bank_account_number": "123456789012", "bank_ifsc": "SBIN0001234",
                                      "full_name": "Kept"}},
                 "extra": {"nested": [{"bank_account": "123456789012"}]}}
        out = scrub_event(event, {})
        data = out["request"]["data"]
        assert {data[k] for k in ("aadhaar_number", "pan", "bank_account_number", "bank_ifsc")} == {REDACTED}
        assert data["full_name"] == "Kept"
        assert out["extra"]["nested"][0]["bank_account"] == REDACTED

    def test_values_inside_free_text_redacted(self):
        from app.security.sentry_scrub import scrub_event
        from tests.associate_support import aadhaar

        number = aadhaar()
        spaced = f"{number[:4]} {number[4:8]} {number[8:]}"
        event = {"message": f"failed for {spaced} / ABCPE1234F",
                 "breadcrumbs": {"values": [{"message": f"form: [('aadhaar_number', '{number}')]"}]}}
        dumped = json.dumps(scrub_event(event, {}))
        assert number not in dumped and spaced not in dumped and "ABCPE1234F" not in dumped

    def test_ordinary_numbers_left_alone(self):
        from app.security.sentry_scrub import scrub_event

        # 12 digits that fail the Verhoeff check are not an Aadhaar number.
        event = {"message": "order 123456789012 took 250 ms"}
        assert scrub_event(event, {})["message"] == "order 123456789012 took 250 ms"

    def test_registration_frames_lose_their_local_variables(self):
        from app.security.sentry_scrub import scrub_event

        event = {"exception": {"values": [{"stacktrace": {"frames": [
            {"module": "app.api.routes.public_associates", "vars": {"form": "FormData(...)"}},
            {"module": "app.associates.validation", "vars": {"value": "x"}},
            {"module": "app.api.routes.dashboard", "vars": {"lead_id": "7"}},
        ]}}]}}
        frames = scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"]
        assert "vars" not in frames[0] and "vars" not in frames[1]
        assert frames[2]["vars"] == {"lead_id": "7"}

    def test_onboarding_frames_lose_their_local_variables(self):
        """app.onboarding and its staff/associate route modules also handle customer
        phone, address, consumer number and GPS — same treatment as registration."""
        from app.security.sentry_scrub import scrub_event

        event = {"exception": {"values": [{"stacktrace": {"frames": [
            {"module": "app.onboarding.service", "vars": {"onboarding": "CustomerOnboarding(...)"}},
            {"module": "app.api.routes.associate_onboarding", "vars": {"body": "..."}},
            {"module": "app.api.routes.onboarding_admin", "vars": {"body": "..."}},
            {"module": "app.api.routes.dashboard", "vars": {"lead_id": "7"}},
        ]}}]}}
        frames = scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"]
        assert "vars" not in frames[0] and "vars" not in frames[1] and "vars" not in frames[2]
        assert frames[3]["vars"] == {"lead_id": "7"}

    def test_init_options(self):
        options = sentry_init_options(dsn="https://x@o0.ingest.sentry.io/0", environment="production")
        assert options["max_request_body_size"] == "never"
        assert options["send_default_pii"] is False
        assert options["before_send"] is not None
