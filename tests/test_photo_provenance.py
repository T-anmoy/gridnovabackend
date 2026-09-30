"""Phase 4A Step 1B — photos taken in the app (camera) or chosen from the phone's gallery,
with honest provenance, on onboarding site photos and on execution photos."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.execution import config as execution_config
from app.execution import photos
from app.memory.models import PhotoFingerprint
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, CAMERA, DASH, associate_in_execution, exec_url, headers, jpeg, kyc_ready, make_associate,
    new_customer, onboarding_ready, territories, upload,
)


def _photo(client, gna, oid, slot="panel_installation_area", content=None, **form):
    """Upload an onboarding site photo; returns (response, the photo from photos[])."""
    r = upload(client, headers(gna), f"{BASE}/{oid}", photo_slot=slot,
               content=content if content is not None else jpeg(seed=oid), **form)
    photo = next((p for p in r.json().get("photos", []) if p["photo_slot"] == slot), None) if r.is_success else None
    return r, photo


class TestCaptureMethod:
    def test_camera_without_location_is_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        r, _ = _photo(client, gna, oid, capture_method="camera")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "location_required"
        r, _ = _photo(client, gna, oid, capture_method="camera", latitude="26.4", longitude="89.5")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "location_required"   # no captured_at

    def test_camera_with_fix(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        r, photo = _photo(client, gna, oid, **CAMERA)
        assert r.status_code == 201
        assert (photo["capture_method"], photo["location_verified"], photo["flags"]) == ("camera", True, [])

    def test_gallery_without_location_is_accepted_and_flagged(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        r, photo = _photo(client, gna, oid, capture_method="gallery")
        assert r.status_code == 201
        assert photo["capture_method"] == "gallery" and photo["location_verified"] is False
        assert photo["flags"] == ["gallery_upload", "no_metadata"]

    def test_gallery_location_is_never_verified(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        _, photo = _photo(client, gna, oid, capture_method="gallery", latitude="26.4", longitude="89.5")
        assert photo["location_verified"] is False and photo["latitude"] == 26.4

    def test_inferred_when_omitted(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        _, photo = _photo(client, gna, oid, latitude="26.4", longitude="89.5")   # older client: no captured_at
        assert photo["capture_method"] == "camera" and photo["location_verified"] is True
        _, photo = _photo(client, gna, oid, slot="wiring_path")
        assert photo["capture_method"] == "gallery"

    def test_gallery_can_be_switched_off(self, client, db_session, onboarding_ready, monkeypatch):
        monkeypatch.setitem(execution_config.photo_policy(), "allow_gallery", False)
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        r, _ = _photo(client, gna, oid, capture_method="gallery")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "gallery_not_allowed"

    def test_heic_is_named_not_silently_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
        r, _ = _photo(client, gna, oid, content=heic, capture_method="gallery")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "heic_not_supported"
        assert "JPEG" in r.json()["detail"]["message"]


class TestExifAndFlags:
    def test_old_exif_on_a_gallery_photo(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        old = jpeg(taken_at=datetime.now() - timedelta(days=90), gps=(22.5726, 88.3639), seed=1)
        _, photo = _photo(client, gna, oid, content=old, capture_method="gallery")
        assert photo["flags"] == ["gallery_upload", "old_photo"]
        detail = client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()
        staff_photo = next(p for p in detail["photos"] if p["photo_slot"] == "panel_installation_area")
        assert staff_photo["flags"] == ["gallery_upload", "old_photo"]

    def test_recent_exif_is_kept_as_information(self, client, db_session, onboarding_ready):
        taken = datetime.now() - timedelta(days=2)
        meta = photos.provenance(content=jpeg(taken_at=taken, gps=(22.5726, 88.3639)), capture_method="gallery",
                                 latitude=None, longitude=None, captured_at=None, exif_taken_at_client=None)
        assert meta["flags"] == ["gallery_upload"]
        assert meta["exif_taken_at"] == taken.replace(microsecond=0).isoformat()
        assert abs(meta["exif_lat"] - 22.5726) < 0.001 and abs(meta["exif_lng"] - 88.3639) < 0.001
        assert meta["location_verified"] is False

    def test_client_declared_exif_time_is_recorded_as_such(self):
        meta = photos.provenance(content=jpeg(), capture_method="gallery", latitude=None, longitude=None,
                                 captured_at=None, exif_taken_at_client="2020-01-01T10:00:00")
        assert meta["exif_taken_at_client"] == "2020-01-01T10:00:00" and "exif_taken_at" not in meta
        assert "old_photo" in meta["flags"]

    def test_camera_photos_get_no_fabricated_flags(self):
        old = jpeg(taken_at=datetime.now() - timedelta(days=400))
        meta = photos.provenance(content=old, capture_method="camera", latitude=26.4, longitude=89.5,
                                 captured_at="2026-09-29T10:00:00+05:30")
        assert meta["flags"] == [] and meta["location_verified"] is True

    def test_broken_image_bytes_do_not_crash_exif(self):
        assert photos.read_exif(b"\xff\xd8\xff\xe0 not really a jpeg") == {}


class TestDuplicates:
    def test_same_bytes_on_another_customer_is_flagged(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        first, second = new_customer(client, gna), new_customer(client, gna)
        same = jpeg(seed=42)
        _, photo = _photo(client, gna, first, content=same, capture_method="gallery")
        assert "duplicate_photo" not in photo["flags"]
        _, photo = _photo(client, gna, first, slot="wiring_path", content=same, capture_method="gallery")
        assert "duplicate_photo" not in photo["flags"]      # same customer: not a duplicate
        _, photo = _photo(client, gna, second, content=same, **CAMERA)
        assert photo["flags"] == ["duplicate_photo"]
        rows = db_session.execute(select(PhotoFingerprint)).scalars().all()
        assert len(rows) == 3 and len({r.sha256 for r in rows}) == 1

    def test_across_onboarding_and_execution_photos(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        other = new_customer(client, gna)
        same = jpeg(seed=77)
        _photo(client, gna, other, content=same, capture_method="gallery")
        r = upload(client, headers(gna), exec_url(oid), photo_slot="earthing", content=same, **CAMERA)
        assert r.json()["files"]["photos"]["earthing"]["flags"] == ["duplicate_photo"]


class TestReadiness:
    def test_gallery_photos_count_unless_policy_says_otherwise(self, client, db_session, onboarding_ready,
                                                               monkeypatch):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        _photo(client, gna, oid, capture_method="gallery")

        def readiness():
            detail = client.get(f"{DASH}/onboardings/{oid}", headers=ADMIN).json()
            factors = detail["score"]["factors"]
            return next(f for f in factors if f["factor"] == "site_readiness")["detail"]
        assert readiness().startswith("1 of 7")
        monkeypatch.setitem(execution_config.photo_policy(), "gallery_counts_fully", False)
        _photo(client, gna, oid, slot="wiring_path", **CAMERA)   # triggers a re-score
        assert readiness().startswith("1 of 7")                  # the camera photo only
