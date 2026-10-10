"""Phase 3C — required, confirmed site location: endpoints, submit gate, flags, visibility,
staff correction, to-do feed, photo accuracy and Sentry. (The migration: tests/test_associate_migrations.py.)"""
import json

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, CustomerOnboarding, OnboardingStatus, TimelineEvent, TimelineEventType
from app.onboarding import maps_link
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, BASE, DASH, ENG, KOLKATA, MGMT, SALES, complete_survey, confirm_site_location, headers, kyc_ready,
    make_associate, onboarding_body, onboarding_ready, territories, upload, upload_required_documents,
)

OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
GPS = {"source": "device_gps", "latitude": 26.4912, "longitude": 89.5231, "accuracy_m": 12.0,
       "captured_at": "2026-10-04T10:15:00+05:30"}
CAMERA = {"capture_method": "camera", "latitude": "26.4912", "longitude": "89.5231",
          "captured_at": "2026-10-04T10:00:00+05:30"}


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    maps_link.reset_rate_limits()
    yield
    maps_link.reset_rate_limits()


def _create(client, gna, **kw) -> int:
    r = client.post(BASE, json=onboarding_body(**kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()["onboarding_id"]


def _set(client, gna, oid, body):
    return client.post(f"{BASE}/{oid}/site-location", json=body, headers=headers(gna))


def _confirm(client, gna, oid):
    return client.post(f"{BASE}/{oid}/site-location/confirm", headers=headers(gna))


def _submit(client, gna, oid):
    return client.post(f"{BASE}/{oid}/submit", headers=headers(gna))


def _audits(db, oid, action=None):
    q = select(AuditLog).where(AuditLog.entity_type == "customer_onboarding", AuditLog.entity_id == str(oid))
    if action:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars())


class TestSetLocation:
    def test_precise_device_fix_is_verified_but_unconfirmed(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, GPS)
        assert r.status_code == 200, r.text
        loc = r.json()["site_location"]
        assert loc == {"present": True, "latitude": 26.4912, "longitude": 89.5231, "source": "device_gps",
                       "accuracy_m": 12.0, "captured_at": "2026-10-04T04:45:00+00:00", "verified": True,
                       "confirmed": False, "confirmed_at": None, "confirmed_by": None, "precision": "exact",
                       "flags": [], "photo_distance_m": None,
                       "maps_url": "https://www.google.com/maps?q=26.4912,89.5231"}
        assert (r.json()["site_latitude"], r.json()["site_longitude"]) == (26.4912, 89.5231)
        assert not [w for w in r.json()["warnings"] if w["code"].startswith("accuracy")]
        [audit] = _audits(db_session, oid, "site_location_set")
        assert audit.performed_by == f"associate:{gna}" and audit.after_value["source"] == "device_gps"
        o = db_session.get(CustomerOnboarding, oid)
        events = db_session.execute(select(TimelineEvent).where(
            TimelineEvent.lead_id == o.lead_id,
            TimelineEvent.event_type == TimelineEventType.site_location_updated)).scalars().all()
        assert len(events) == 1 and "26.49" not in events[0].description

    def test_accuracy_bands(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        body = _set(client, gna, oid, {**GPS, "accuracy_m": 100}).json()
        assert body["site_location"]["verified"] is False and body["site_location"]["flags"] == ["low_accuracy"]
        assert {"low_accuracy"} <= {w["code"] for w in body["warnings"]}
        body = _set(client, gna, oid, {**GPS, "accuracy_m": 200}).json()
        assert {"low_accuracy", "accuracy_poor"} <= {w["code"] for w in body["warnings"]}
        r = _set(client, gna, oid, {**GPS, "accuracy_m": 301})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "accuracy_too_low"
        assert r.json()["detail"]["accuracy_reject_m"] == 300
        # The refused fix did not replace the stored one.
        assert db_session.get(CustomerOnboarding, oid).site_location_accuracy_m == 200

    @pytest.mark.parametrize("body,code", [
        ({**GPS, "latitude": 51.5074, "longitude": -0.1278}, "outside_india"),
        ({**GPS, "latitude": 0, "longitude": 0}, "invalid_coordinates"),
        ({**GPS, "latitude": 0.0004, "longitude": 89.5}, "invalid_coordinates"),
        ({"source": "pasted_coordinates", "text": "51.5074, -0.1278"}, "outside_india"),
        ({"source": "pasted_coordinates", "text": "my house"}, "invalid_coordinates"),
        ({"source": "pasted_coordinates", "text": "0, 0"}, "invalid_coordinates"),
    ])
    def test_refusals(self, client, db_session, onboarding_ready, body, code):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code
        assert db_session.get(CustomerOnboarding, oid).site_latitude is None

    def test_swapped_pasted_coordinates_are_pointed_out(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, {"source": "pasted_coordinates", "text": "89.5231, 26.4912"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "outside_india"
        assert r.json()["detail"]["likely_swapped"] is True

    def test_unknown_source_and_missing_fields_are_422(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        assert _set(client, gna, oid, {"source": "magic"}).status_code == 422
        assert _set(client, gna, oid, {"source": "device_gps", "latitude": 26.4, "longitude": 89.5}).status_code == 422
        assert _set(client, gna, oid, {"source": "staff_entered", "text": "26.4, 89.5"}).status_code == 422

    def test_pasted_coordinates_are_unverified(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        loc = _set(client, gna, oid, {"source": "pasted_coordinates", "text": "26.4912° N, 89.5231° E"}).json()
        loc = loc["site_location"]
        assert (loc["source"], loc["verified"], loc["flags"]) == ("pasted_coordinates", False, ["unverified_source"])
        assert db_session.get(CustomerOnboarding, oid).site_location_raw_input == "26.4912° N, 89.5231° E"


class TestPhotoGps:
    def test_camera_photo_with_accuracy_sets_a_verified_location(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = upload(client, gna, oid, photo_slot="panel_installation_area", accuracy_m="8.5", **CAMERA)
        assert r.status_code == 201, r.text
        [photo] = r.json()["photos"]
        assert photo["accuracy_m"] == 8.5 and photo["capture_method"] == "camera"
        loc = _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "panel_installation_area"}).json()
        loc = loc["site_location"]
        assert (loc["source"], loc["latitude"], loc["longitude"], loc["accuracy_m"], loc["verified"]) == (
            "photo_gps", 26.4912, 89.5231, 8.5, True)
        assert loc["captured_at"] == "2026-10-04T04:30:00+00:00" and loc["photo_distance_m"] == 0.0
        assert loc["flags"] == [] and loc["confirmed"] is False

    def test_camera_photo_without_accuracy_is_unverified(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload(client, gna, oid, photo_slot="wiring_path", **CAMERA)
        loc = _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "wiring_path"}).json()["site_location"]
        assert loc["verified"] is False and loc["flags"] == ["accuracy_unknown"]

    def test_poor_photo_fix_is_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload(client, gna, oid, photo_slot="wiring_path", accuracy_m="500", **CAMERA)
        r = _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "wiring_path"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "accuracy_too_low"

    @pytest.mark.parametrize("setup,reason", [("none", "no_photo"), ("gallery", "not_camera"),
                                              ("gallery_gps", "not_camera")])
    def test_refused_without_a_camera_fix(self, client, db_session, onboarding_ready, setup, reason):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        if setup == "gallery":
            upload(client, gna, oid, photo_slot="wiring_path", capture_method="gallery")
        elif setup == "gallery_gps":
            upload(client, gna, oid, photo_slot="wiring_path", capture_method="gallery", latitude="26.49",
                   longitude="89.52")
        r = _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "wiring_path"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "no_gps_on_photo"
        assert r.json()["detail"]["reason"] == reason

    def test_unknown_slot(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "selfie"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_photo_slot"

    def test_accuracy_is_ignored_on_documents(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload(client, gna, oid, document_type="id_proof", accuracy_m="5")
        o = db_session.get(CustomerOnboarding, oid)
        from app.memory.models import ProposalInputs
        refs = db_session.get(ProposalInputs, o.proposal_inputs_id).document_refs
        assert all("accuracy_m" not in r for r in refs)

    def test_far_from_photos(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        for slot, lat in (("wiring_path", "26.4912"), ("earthing_location", "26.4914"),
                          ("acdb_dcdb_placement", "26.4990")):
            upload(client, gna, oid, photo_slot=slot, **{**CAMERA, "latitude": lat})
        upload(client, gna, oid, photo_slot="inverter_installation_area", capture_method="gallery",
               latitude="25.0", longitude="88.0")   # gallery fixes never count
        body = _set(client, gna, oid, {"source": "pasted_coordinates", "text": "26.5100, 89.5231"}).json()
        loc = body["site_location"]
        # Median of the camera fixes is 26.4914 → about 2.07 km from the pin.
        assert "far_from_photos" in loc["flags"] and 2000 < loc["photo_distance_m"] < 2150
        assert "far_from_photos" in {w["code"] for w in body["warnings"]}
        loc = _set(client, gna, oid, {"source": "pasted_coordinates", "text": "26.4920, 89.5231"}).json()
        assert "far_from_photos" not in loc["site_location"]["flags"]


class TestMapsLinkSource:
    def test_full_link_needs_no_network(self, client, db_session, onboarding_ready, monkeypatch):
        monkeypatch.setattr(maps_link, "resolve_maps_link", lambda *a, **k: pytest.fail("network used"))
        gna = make_associate(db_session)
        oid = _create(client, gna)
        link = "https://www.google.com/maps/place/Home/@26.48,89.51,17z/data=!3d26.4912!4d89.5231"
        loc = _set(client, gna, oid, {"source": "google_maps_link", "link": link}).json()["site_location"]
        assert (loc["source"], loc["latitude"], loc["precision"], loc["flags"]) == (
            "google_maps_link", 26.4912, "exact", ["unverified_source"])
        assert db_session.get(CustomerOnboarding, oid).site_location_raw_input == link

    def test_viewport_link_is_approximate(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        body = _set(client, gna, oid, {"source": "google_maps_link",
                                       "link": "https://www.google.com/maps/@26.4912,89.5231,15z"}).json()
        assert body["site_location"]["precision"] == "viewport_center"
        assert set(body["site_location"]["flags"]) == {"approximate_location", "unverified_source"}
        assert "approximate_location" in {w["code"] for w in body["warnings"]}

    def test_short_link_is_resolved(self, client, db_session, onboarding_ready, monkeypatch):
        seen = []
        monkeypatch.setattr(maps_link, "resolve_maps_link",
                            lambda url, transport=None: seen.append(url) or
                            "https://www.google.com/maps/place/26.4912,89.5231")
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, {"source": "google_maps_link", "link": "https://maps.app.goo.gl/AbC123?g_st=iw"})
        assert r.status_code == 200 and r.json()["site_location"]["latitude"] == 26.4912
        assert seen == ["https://maps.app.goo.gl/AbC123?g_st=iw"]

    def test_link_errors_are_422_with_guidance(self, client, db_session, onboarding_ready, monkeypatch):
        monkeypatch.setattr(maps_link, "resolve_maps_link",
                            lambda url, transport=None: "https://www.google.com/maps/place/Some+Shop")
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, {"source": "google_maps_link", "link": "https://maps.app.goo.gl/AbC"})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "no_coordinates_in_link" and detail["guidance_keys"]
        assert detail["message_key"] == "site_location.no_coordinates_in_link"
        r = _set(client, gna, oid, {"source": "google_maps_link", "link": "https://google.com.evil.tld/maps?q=1,2"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_link"
        assert r.json()["detail"]["reason"] == "host_not_allowed"

    def test_an_unexpected_resolver_failure_is_never_a_500(self, client, db_session, onboarding_ready, monkeypatch):
        import httpx

        def boom(request):
            raise RuntimeError("socket exploded")
        monkeypatch.setattr(maps_link, "_getaddrinfo", lambda host: ["142.250.183.110"])
        real = maps_link.resolve_maps_link
        monkeypatch.setattr(maps_link, "resolve_maps_link",
                            lambda url, transport=None: real(url, transport=httpx.MockTransport(boom)))
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _set(client, gna, oid, {"source": "google_maps_link", "link": "https://maps.app.goo.gl/AbC"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "link_unresolvable"

    def test_rate_limit_is_429(self, client, db_session, onboarding_ready, monkeypatch):
        from app.onboarding import config
        monkeypatch.setitem(config.location(), "link_resolve_rate_limit", 1)
        monkeypatch.setattr(maps_link, "resolve_maps_link",
                            lambda url, transport=None: "https://www.google.com/maps/place/26.4912,89.5231")
        gna = make_associate(db_session)
        oid = _create(client, gna)
        link = {"source": "google_maps_link", "link": "https://maps.app.goo.gl/AbC"}
        assert _set(client, gna, oid, link).status_code == 200
        r = _set(client, gna, oid, link)
        assert r.status_code == 429 and r.json()["detail"]["code"] == "link_rate_limited"


class TestConfirm:
    def test_confirm_requires_a_location(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = _confirm(client, gna, oid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "site_location_not_set"

    def test_confirm_then_change_clears_it(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        _set(client, gna, oid, GPS)
        loc = _confirm(client, gna, oid).json()["site_location"]
        assert loc["confirmed"] is True and loc["confirmed_by"] == f"associate:{gna}" and loc["confirmed_at"]
        assert len(_audits(db_session, oid, "site_location_confirmed")) == 1
        assert _confirm(client, gna, oid).status_code == 200            # idempotent
        assert len(_audits(db_session, oid, "site_location_confirmed")) == 1
        loc = _set(client, gna, oid, {**GPS, "latitude": 26.4950}).json()["site_location"]
        assert loc["confirmed"] is False and loc["confirmed_at"] is None
        [cleared] = _audits(db_session, oid, "site_location_confirmation_cleared")
        assert cleared.before_value["confirmed_by"] == f"associate:{gna}"

    def test_resending_the_same_fix_also_needs_confirming_again(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        _set(client, gna, oid, GPS)
        _confirm(client, gna, oid)
        assert _set(client, gna, oid, GPS).json()["site_location"]["confirmed"] is False

    def test_owner_only(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        preferred = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        stranger = make_associate(db_session)
        oid = _create(client, gna)
        for who in (preferred, stranger):
            assert _set(client, who, oid, GPS).status_code == 403
            assert _confirm(client, who, oid).status_code == 403
        assert client.post(f"{BASE}/{oid}/site-location", json=GPS, headers=ADMIN).status_code == 401


class TestLegacyPair:
    def test_create_with_a_raw_pair_is_unknown_legacy(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        r = client.post(BASE, json=onboarding_body(site_latitude=26.49, site_longitude=89.52), headers=headers(gna))
        loc = r.json()["site_location"]
        assert (loc["source"], loc["verified"], loc["confirmed"], loc["flags"]) == (
            "unknown_legacy", False, False, ["unverified_source"])
        assert len(_audits(db_session, r.json()["onboarding_id"], "site_location_set")) == 1

    def test_patch_with_a_raw_pair_clears_confirmation(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        same = {"site_latitude": 26.49, "site_longitude": 89.52}
        body = client.patch(f"{BASE}/{oid}", json=same, headers=headers(gna)).json()
        assert body["site_location"]["confirmed"] is True     # same coordinates: nothing changed
        body = client.patch(f"{BASE}/{oid}", json={"site_latitude": 26.50, "site_longitude": 89.52},
                            headers=headers(gna)).json()
        assert (body["site_location"]["source"], body["site_location"]["confirmed"]) == ("unknown_legacy", False)
        assert len(_audits(db_session, oid, "site_location_confirmation_cleared")) == 1
        updated = _audits(db_session, oid, "updated")[-1]
        assert updated.after_value["site_location"]["source"] == "unknown_legacy"

    def test_legacy_pair_outside_india_cannot_be_confirmed(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = client.post(BASE, json=onboarding_body(site_latitude=1.0, site_longitude=1.0),
                          headers=headers(gna)).json()["onboarding_id"]
        r = _confirm(client, gna, oid)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "outside_india"


class TestEditRules:
    def test_change_on_a_submitted_customer_returns_it_to_draft(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        assert _submit(client, gna, oid).json()["status"] == "submitted"
        body = _set(client, gna, oid, GPS).json()
        assert body["status"] == "draft" and body["submitted_at"] is None
        [audit] = _audits(db_session, oid, "site_location_set")[-1:]
        assert (audit.before_value["status"], audit.after_value["status"]) == ("submitted", "draft")
        assert _submit(client, gna, oid).json()["detail"]["site_location_unconfirmed"] is True

    def test_confirm_on_a_submitted_customer_keeps_it_submitted(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        _submit(client, gna, oid)
        assert _confirm(client, gna, oid).json()["status"] == "submitted"

    @pytest.mark.parametrize("status", [OnboardingStatus.verified, OnboardingStatus.closed])
    def test_verified_and_closed_are_not_editable(self, client, db_session, onboarding_ready, status):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        _set(client, gna, oid, GPS)
        o = db_session.get(CustomerOnboarding, oid)
        o.status = status
        if status == OnboardingStatus.verified:
            from datetime import datetime, timezone
            o.verification_call_completed, o.verification_call_note = True, "x"
            o.verified_by_employee_id, o.attribution_locked_until = "emp-admin-1", datetime.now(timezone.utc)
        db_session.commit()
        for r in (_set(client, gna, oid, GPS), _confirm(client, gna, oid)):
            assert r.status_code == 409 and r.json()["detail"]["code"] == "not_editable"


class TestSubmitGate:
    def test_draft_without_location_is_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        r = _submit(client, gna, oid)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert {k: detail[k] for k in ("code", "missing_documents", "missing_site_location",
                                       "site_location_unconfirmed")} == {
            "code": "incomplete", "missing_documents": [], "missing_site_location": True,
            "site_location_unconfirmed": False}
        assert "site location" in detail["message"]

    def test_unconfirmed_is_refused_confirmed_is_accepted(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        complete_survey(client, gna, oid)
        _set(client, gna, oid, GPS)
        detail = _submit(client, gna, oid).json()["detail"]
        assert (detail["missing_site_location"], detail["site_location_unconfirmed"]) == (False, True)
        _confirm(client, gna, oid)
        assert _submit(client, gna, oid).status_code == 200

    def test_missing_documents_and_location_together(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        detail = _submit(client, gna, oid).json()["detail"]
        assert detail["code"] == "incomplete" and detail["missing_documents"] and detail["missing_site_location"]

    def test_changing_coordinates_after_confirming_is_refused_again(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        _set(client, gna, oid, GPS)
        r = _submit(client, gna, oid)
        assert r.status_code == 422 and r.json()["detail"]["site_location_unconfirmed"] is True

    def test_grandfathered_records_are_not_gated(self, client, db_session, onboarding_ready):
        """A customer submitted before Phase 3C (no location) can still be verified."""
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        o = db_session.get(CustomerOnboarding, oid)
        from datetime import datetime, timezone
        o.status, o.submitted_at = OnboardingStatus.submitted, datetime.now(timezone.utc)
        db_session.commit()
        r = client.post(f"{DASH}/onboardings/{oid}/verification-call",
                        json={"verification_call_completed": True, "note": "Called."}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "verified"
        loc = r.json()["site_location"]
        assert loc["flags"] == ["location_missing"] and loc["check_with_customer"] is True

    def test_gate_can_be_switched_off_in_config(self, client, db_session, onboarding_ready, monkeypatch):
        from app.onboarding import config
        monkeypatch.setitem(config.location(), "required_to_submit", False)
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        complete_survey(client, gna, oid)
        assert _submit(client, gna, oid).status_code == 200


class TestVisibility:
    def _customer(self, client, db_session):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload(client, gna, oid, photo_slot="wiring_path", accuracy_m="9", **CAMERA)
        _set(client, gna, oid, {"source": "photo_gps", "photo_slot": "wiring_path"})
        _confirm(client, gna, oid)
        return gna, oid

    def test_preferred_associate_sees_presence_only(self, client, db_session, onboarding_ready):
        gna, oid = self._customer(client, db_session)
        preferred = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        body = client.get(f"{BASE}/{oid}", headers=headers(preferred)).json()
        assert body["site_location"] == {"present": True, "confirmed": True}
        assert body["site_latitude"] is None and body["site_longitude"] is None
        assert all(p["latitude"] is None and p["longitude"] is None and p["accuracy_m"] is None
                   for p in body["photos"])
        dumped = json.dumps(body)
        assert "26.4912" not in dumped and "89.5231" not in dumped
        [item] = client.get(BASE, headers=headers(preferred)).json()["items"]
        assert item["site_location"] == {"present": True, "confirmed": True} and item["site_latitude"] is None
        assert item["progress_summary"]["site_location"] == {"present": True, "confirmed": True}
        assert "26.4912" not in json.dumps(item)

    def test_owner_and_staff_see_coordinates(self, client, db_session, onboarding_ready):
        gna, oid = self._customer(client, db_session)
        owner = client.get(f"{BASE}/{oid}", headers=headers(gna)).json()
        assert owner["site_location"]["latitude"] == 26.4912 and owner["site_latitude"] == 26.4912
        assert "check_with_customer" not in owner["site_location"]
        for who in (ADMIN, MGMT, SALES):
            staff = client.get(f"{DASH}/onboardings/{oid}", headers=who).json()
            assert staff["site_location"]["latitude"] == 26.4912 and staff["site_location"]["verified"] is True
            assert staff["site_location"]["check_with_customer"] is False
        [item] = client.get(BASE, headers=headers(gna)).json()["items"]
        assert item["site_location"]["latitude"] == 26.4912

    def test_staff_detail_prompts_a_check_for_unverified_locations(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        staff = client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()["site_location"]
        assert staff["flags"] == ["unverified_source"] and staff["check_with_customer"] is True


class TestStaffCorrection:
    URL = DASH + "/onboardings/{}/site-location"

    def test_sales_correction(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        complete_survey(client, gna, oid)
        _set(client, gna, oid, GPS)
        _confirm(client, gna, oid)
        _submit(client, gna, oid)
        r = client.post(self.URL.format(oid), headers=SALES, json={
            "source": "pasted_coordinates", "text": "26.4950, 89.5200", "reason": "Customer gave the right gate."})
        assert r.status_code == 200, r.text
        body = r.json()
        loc = body["site_location"]
        assert (loc["source"], loc["verified"], loc["confirmed"], loc["confirmed_by"]) == (
            "staff_entered", False, True, "emp-sales-1")
        assert body["status"] == "submitted"   # staff corrections do not bounce the customer
        [audit] = _audits(db_session, oid, "site_location_corrected")
        assert audit.performed_by == "emp-sales-1" and audit.after_value["reason"] == "Customer gave the right gate."
        assert audit.before_value["latitude"] == 26.4912 and audit.after_value["latitude"] == 26.495
        assert audit.after_value["input_source"] == "pasted_coordinates"

    def test_link_correction(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = client.post(self.URL.format(oid), headers=ADMIN, json={
            "source": "google_maps_link", "link": "https://maps.google.com/?q=26.4950,89.5200",
            "reason": "Sent by the customer."})
        assert r.status_code == 200 and r.json()["site_location"]["latitude"] == 26.495

    @pytest.mark.parametrize("body,status,code", [
        ({"source": "pasted_coordinates", "text": "26.49, 89.52", "reason": "  ok  "}, 422, "reason_required"),
        ({"source": "pasted_coordinates", "text": "51.5, -0.12", "reason": "Called them."}, 422, "outside_india"),
        ({"source": "google_maps_link", "reason": "Called them."}, 422, None),
        ({"source": "device_gps", "latitude": 26.4, "longitude": 89.5, "reason": "Called them."}, 422, None),
    ])
    def test_refusals(self, client, db_session, onboarding_ready, body, status, code):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = client.post(self.URL.format(oid), headers=ADMIN, json=body)
        assert r.status_code == status
        if code:
            assert r.json()["detail"]["code"] == code

    def test_roles(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        body = {"source": "pasted_coordinates", "text": "26.49, 89.52", "reason": "Called them."}
        for who in (MGMT, ENG, OPS):
            assert client.post(self.URL.format(oid), headers=who, json=body).status_code == 403

    def test_closed_is_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        db_session.get(CustomerOnboarding, oid).status = OnboardingStatus.closed
        db_session.commit()
        r = client.post(self.URL.format(oid), headers=ADMIN,
                        json={"source": "pasted_coordinates", "text": "26.49, 89.52", "reason": "Called them."})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_editable"


class TestOptionsAndFeed:
    def test_options(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        opts = client.get(f"{BASE}/options", headers=headers(gna)).json()["site_location"]
        assert opts == {"accuracy_good_m": 50, "accuracy_warn_m": 150, "accuracy_reject_m": 300,
                        "sample_seconds": 10, "required_to_submit": True,
                        "sources": ["device_gps", "photo_gps", "google_maps_link", "pasted_coordinates"],
                        "recommended_source": "photo_gps"}

    def test_feed_walks_missing_unconfirmed_ready(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        complete_survey(client, gna, oid)

        def kinds():
            items = client.get("/api/v1/associate/home", headers=headers(gna)).json()["items"]
            return {(i["kind"], i["message_key"], i["priority"]) for i in items if i["onboarding_id"] == oid}
        assert kinds() == {("site_location_missing", "todo.site_location_missing", 1)}
        _set(client, gna, oid, GPS)
        assert kinds() == {("site_location_unconfirmed", "todo.site_location_unconfirmed", 1)}
        _confirm(client, gna, oid)
        assert kinds() == {("ready_to_submit", "todo.ready_to_submit", 1)}
        _submit(client, gna, oid)
        assert kinds() == set()
        [item] = client.get(BASE, headers=headers(gna)).json()["items"]
        assert item["progress_summary"]["site_location"] == {"present": True, "confirmed": True}


def test_sentry_event_from_a_failed_link_resolution_has_no_link_or_coordinates(
        client, db_session, onboarding_ready, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_sentry_scrubbing import _serialized, sentry_recording

    secret_link = "https://maps.app.goo.gl/SeCrEtLiNk42?g_st=iw"
    final = "https://www.google.com/maps/place/26.4912,89.5231"
    # An unexpected bug inside link handling, raised from the real maps_link frame (whose
    # locals hold the pasted link and the resolved URL): the short link parses to
    # nothing, the resolver returns `final`, and parsing `final` raises KeyError(final).
    monkeypatch.setattr(maps_link, "resolve_maps_link", lambda url, transport=None: final)
    from app import geo
    monkeypatch.setattr(geo, "parse_maps_url", {secret_link: None}.__getitem__)
    gna = make_associate(db_session)
    oid = _create(client, gna)
    with sentry_recording() as events:
        r = TestClient(app, raise_server_exceptions=False).post(
            f"{BASE}/{oid}/site-location", json={"source": "google_maps_link", "link": secret_link},
            headers=headers(gna))
    assert r.status_code == 500
    errors = [e for e in events if e.get("exception")]
    assert errors and any("KeyError" in json.dumps(e["exception"]) for e in errors)
    frames = [f for e in errors for x in e["exception"]["values"] for f in x["stacktrace"]["frames"]]
    assert any(f.get("module") == "app.onboarding.maps_link" for f in frames)
    dumped = _serialized(events)
    for leaked in ("SeCrEtLiNk42", "maps.app.goo.gl", "26.4912", "89.5231"):
        assert leaked not in dumped, leaked


def test_scrubber_redacts_location_text_and_keys():
    from app.security.sentry_scrub import REDACTED, scrub_event
    event = {"message": "pin https://maps.app.goo.gl/AbC?g_st=iw and www.google.co.in/maps/@22.57,88.36,15z "
                        "at 22.5726, 88.3639; paid 1500.00, 20.50 for 3 kW",
             "extra": {"site_latitude": 22.5726, "longitude": 88.3639, "site_location_raw_input": "x"}}
    out = scrub_event(event, None)
    assert "goo.gl" not in out["message"] and "google.co.in" not in out["message"] and "22.5726" not in out["message"]
    assert "1500.00, 20.50" in out["message"]
    assert set(out["extra"].values()) == {REDACTED}
