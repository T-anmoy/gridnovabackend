"""Photo provenance for associate uploads — onboarding site photos and execution photos.

An associate may take a photo in the app (`camera`) or choose one from the phone's
gallery (`gallery`). What the server records is honest about which:

  * camera  — the device's GPS fix and capture time are required (422 location_required
              otherwise); `location_verified` is true.
  * gallery — location optional; nothing about the file's own place or time is ever
              treated as verified. EXIF inside the file (Pillow) is kept as informational,
              untrusted fields, and so is a client-declared EXIF time when the app had to
              convert or compress the file.

Non-blocking flags (never a rejection): gallery_upload, no_metadata (gallery file with no
EXIF), old_photo (EXIF older than execution.photo_policy.exif_max_age_days), and
duplicate_photo (the same bytes already on another project — photo_fingerprints).

`capture_method` omitted: camera when latitude+longitude came with it, else gallery —
so clients that predate this field keep working unchanged.
"""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.execution import config
from app.memory.models import PhotoFingerprint

CAPTURE_METHODS = ("camera", "gallery")
FLAGS = ("gallery_upload", "no_metadata", "old_photo", "duplicate_photo")
_EXIF_DATETIME_ORIGINAL, _EXIF_DATETIME = 36867, 306


def _rational(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        num, den = value
        return float(num) / float(den)


def _degrees(dms, ref) -> Optional[float]:
    try:
        d, m, s = (_rational(v) for v in dms)
    except Exception:
        return None
    deg = d + m / 60 + s / 3600
    return -deg if str(ref).upper() in ("S", "W") else deg


def read_exif(content: bytes) -> dict:
    """{"taken_at": "YYYY-MM-DDTHH:MM:SS" (device-local, no zone), "lat", "lng"} — only
    what is present and parseable. Never raises: a file with no or broken EXIF is {}."""
    try:
        from PIL import ExifTags, Image

        with Image.open(io.BytesIO(content)) as img:
            exif = img.getexif()
            out: dict = {}
            raw = exif.get_ifd(ExifTags.IFD.Exif).get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME)
            if raw:
                out["taken_at"] = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            if gps and 2 in gps and 4 in gps:
                lat, lng = _degrees(gps[2], gps.get(1, "N")), _degrees(gps[4], gps.get(3, "E"))
                if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
                    out.update(lat=round(lat, 6), lng=round(lng, 6))
            return out
    except Exception:
        return {}


def provenance(*, content: bytes, capture_method: Optional[str], latitude: Optional[float],
               longitude: Optional[float], captured_at: Optional[str],
               exif_taken_at_client: Optional[str] = None) -> dict:
    """The provenance fields for one photo (everything except duplicate_photo, which
    needs the database — see flag_duplicate). Raises AssociateError (422)."""
    method = capture_method or ("camera" if latitude is not None and longitude is not None else "gallery")
    if method not in CAPTURE_METHODS:
        raise AssociateError(f"capture_method must be one of {list(CAPTURE_METHODS)}.", code="invalid_capture_method",
                             status_code=422, field="capture_method")
    policy = config.photo_policy()
    # An explicit camera claim needs the fix AND the capture time; an inferred one was
    # inferred from the fix, so an older client that never sent captured_at still works.
    if method == "camera" and (latitude is None or longitude is None or (capture_method and not captured_at)):
        raise AssociateError("A photo taken in the app needs the device's location and the time it was taken.",
                             code="location_required", status_code=422, field="latitude")
    if method == "gallery" and not policy["allow_gallery"]:
        raise AssociateError("Photos must be taken in the app.", code="gallery_not_allowed", status_code=422,
                             field="capture_method")
    exif = read_exif(content)
    meta = {"sha256": hashlib.sha256(content).hexdigest(), "capture_method": method,
            "location_verified": method == "camera"}
    if exif.get("taken_at"):
        meta["exif_taken_at"] = exif["taken_at"]
    if "lat" in exif:
        meta["exif_lat"], meta["exif_lng"] = exif["lat"], exif["lng"]
    if exif_taken_at_client:
        meta["exif_taken_at_client"] = exif_taken_at_client
    flags = []
    if method == "gallery":
        flags.append("gallery_upload")
        if not exif:
            flags.append("no_metadata")
        taken = meta.get("exif_taken_at") or exif_taken_at_client
        if taken and _older_than(taken, policy["exif_max_age_days"]):
            flags.append("old_photo")
    meta["flags"] = flags
    return meta


def _older_than(taken_at: str, days: int) -> bool:
    try:
        taken = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - taken > timedelta(days=days)


def flag_duplicate_and_record(db: Session, meta: dict, *, project_id: int, source: str, slot: str,
                              storage_key: str) -> dict:
    """Add duplicate_photo when these bytes are already on ANOTHER project, then record
    this photo's fingerprint. Returns meta (updated in place)."""
    elsewhere = db.execute(select(PhotoFingerprint.id).where(
        PhotoFingerprint.sha256 == meta["sha256"], PhotoFingerprint.project_id != project_id)).first()
    if elsewhere and "duplicate_photo" not in meta["flags"]:
        meta["flags"] = [*meta["flags"], "duplicate_photo"]
    db.add(PhotoFingerprint(sha256=meta["sha256"], project_id=project_id, source=source, slot=slot,
                            storage_key=storage_key, created_at=datetime.now(timezone.utc)))
    return meta


def public_fields(ref: dict) -> dict:
    """The provenance fields serialized on a photo. A photo uploaded before provenance
    was recorded has capture_method null — never a guess."""
    return {"capture_method": ref.get("capture_method"), "location_verified": bool(ref.get("location_verified")),
            "flags": list(ref.get("flags") or [])}


def counts_as_evidence(ref: dict) -> bool:
    """Whether a photo counts toward readiness/evidence under the photo policy (E9)."""
    if ref.get("capture_method") == "gallery" and not config.photo_policy()["gallery_counts_fully"]:
        return False
    return bool(ref.get("storage_key"))
