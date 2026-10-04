"""The customer's site location — required, and confirmed by the associate, before an
onboarding can be submitted.

Sources (CustomerOnboarding.site_location_source):
  device_gps          one fix from the phone, taken when the associate presses a button
                      (never background or continuous tracking)
  photo_gps           copied from an in-app CAMERA photo's stored GPS fix (gallery photos
                      and photos without GPS are refused)
  google_maps_link    parsed from a pasted link (app/onboarding/maps_link.py)
  pasted_coordinates  parsed from pasted "lat, lon" text
  staff_entered       corrected by staff (admin/sales) during the verification call
  unknown_legacy      a raw site_latitude/site_longitude pair on POST/PATCH (old clients,
                      and rows from before Phase 3C)

A device/photo fix at or under location.accuracy_good_m is `verified`; anything else is
accepted but unverified, and says why in `flags` (computed on read, never blocking):
low_accuracy, accuracy_unknown, approximate_location, unverified_source,
far_from_photos, location_missing.

Setting a location — any source, even the same coordinates — leaves it UNCONFIRMED;
the associate must then confirm "this is the customer's house". Any change to the
coordinates clears an earlier confirmation (audited as
site_location_confirmation_cleared). Staff corrections count as confirmed by that
staff member. Every change is audited and put on the lead's timeline.

Who sees what (serialize): the owner and staff get the full object with exact
coordinates; a Preferred Associate viewing a downline customer gets only
{present, confirmed}.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import geo
from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    INSTALLATION_PHOTO_SLOTS, CustomerOnboarding, OnboardingStatus, ProposalInputs, SiteLocationPrecision,
    SiteLocationSource, TimelineEventType,
)
from app.onboarding import config

ASSOCIATE_SOURCES = ("device_gps", "photo_gps", "google_maps_link", "pasted_coordinates")
STAFF_SOURCES = ("google_maps_link", "pasted_coordinates")
GPS_SOURCES = (SiteLocationSource.device_gps, SiteLocationSource.photo_gps)
UNVERIFIED_SOURCES = (SiteLocationSource.google_maps_link, SiteLocationSource.pasted_coordinates,
                      SiteLocationSource.staff_entered, SiteLocationSource.unknown_legacy)
FLAGS = ("low_accuracy", "accuracy_unknown", "approximate_location", "unverified_source", "far_from_photos",
         "location_missing")
# Flags that should make staff confirm the address with the customer on the call.
CHECK_WITH_CUSTOMER = frozenset(FLAGS)
_LABELS = {SiteLocationSource.device_gps: "the phone's GPS", SiteLocationSource.photo_gps: "a site photo's GPS",
           SiteLocationSource.google_maps_link: "a Google Maps link",
           SiteLocationSource.pasted_coordinates: "pasted coordinates", SiteLocationSource.staff_entered: "staff",
           SiteLocationSource.unknown_legacy: "coordinates entered directly"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _audit(db: Session, onboarding_id: int, action: str, performed_by: str, before=None, after=None) -> None:
    from app.onboarding.service import _audit as audit
    audit(db, onboarding_id, action, performed_by, before=before, after=after)


def _geo_error(exc: geo.GeoError) -> AssociateError:
    return AssociateError(str(exc), code=exc.code, status_code=422, field="location",
                          extra={"likely_swapped": True} if exc.likely_swapped else None)


def check(lat, lon) -> tuple[float, float]:
    try:
        return geo.check_point(lat, lon, config.location()["india_bbox"])
    except geo.GeoError as exc:
        raise _geo_error(exc)


def _snapshot(o: CustomerOnboarding) -> dict:
    return {"latitude": o.site_latitude, "longitude": o.site_longitude,
            "source": o.site_location_source.value if o.site_location_source else None,
            "accuracy_m": o.site_location_accuracy_m, "verified": o.site_location_verified,
            "precision": o.site_location_precision.value if o.site_location_precision else None,
            "confirmed_at": _iso(o.site_location_confirmed_at), "confirmed_by": o.site_location_confirmed_by}


def is_present(o: CustomerOnboarding) -> bool:
    return o.site_latitude is not None and o.site_longitude is not None


def is_confirmed(o: CustomerOnboarding) -> bool:
    return is_present(o) and o.site_location_confirmed_at is not None


# ── Writing ──────────────────────────────────────────────────────────────────────

def _store(db: Session, o: CustomerOnboarding, *, lat: Optional[float], lon: Optional[float],
           source: Optional[SiteLocationSource], accuracy_m: Optional[float] = None,
           captured_at: Optional[datetime] = None, verified: bool = False, raw_input: Optional[str] = None,
           precision: Optional[SiteLocationPrecision] = None, confirmed_by: Optional[str] = None) -> tuple[dict, dict]:
    """Write the location fields; returns (before, after) snapshots. Leaves the
    location unconfirmed unless `confirmed_by` is given."""
    before = _snapshot(o)
    o.site_latitude, o.site_longitude = lat, lon
    o.site_location_source = source if lat is not None else None
    o.site_location_accuracy_m = accuracy_m
    o.site_location_captured_at = captured_at
    o.site_location_verified = bool(verified) and lat is not None
    o.site_location_raw_input = raw_input[:500] if raw_input else None
    o.site_location_precision = precision
    o.site_location_confirmed_at = _now() if confirmed_by else None
    o.site_location_confirmed_by = confirmed_by
    return before, _snapshot(o)


def _after_change(db: Session, o: CustomerOnboarding, before: dict, performed_by: str, description: str) -> None:
    if before["confirmed_at"] and not o.site_location_confirmed_at:
        _audit(db, o.id, "site_location_confirmation_cleared", performed_by,
               before={"confirmed_at": before["confirmed_at"], "confirmed_by": before["confirmed_by"]},
               after={"confirmed_at": None, "reason": "site location changed"})
    MemoryManager(db).add_timeline_event(lead_id=o.lead_id, event_type=TimelineEventType.site_location_updated,
                                         description=description, created_by=performed_by)


def _require_editable(o: CustomerOnboarding) -> None:
    from app.onboarding.service import EDITABLE_STATUSES
    if o.status not in EDITABLE_STATUSES:
        raise AssociateError(f"This customer is {o.status.value} and can no longer be edited here.",
                             code="not_editable", status_code=409)


def _back_to_draft(db: Session, o: CustomerOnboarding, performed_by: str) -> Optional[dict]:
    if o.status != OnboardingStatus.submitted:
        return None
    o.status = OnboardingStatus.draft
    o.submitted_at = None
    return {"before": "submitted", "after": "draft"}


def _photo_fix(inputs: ProposalInputs, slot: str) -> dict:
    if slot not in INSTALLATION_PHOTO_SLOTS:
        raise AssociateError(f"photo_slot must be one of {list(INSTALLATION_PHOTO_SLOTS)}.",
                             code="unknown_photo_slot", status_code=422, field="photo_slot")
    ref = (inputs.installation_photo_refs or {}).get(slot) or {}
    if not ref.get("storage_key"):
        raise AssociateError("No photo has been uploaded for that slot.", code="no_gps_on_photo",
                             status_code=422, field="photo_slot", extra={"reason": "no_photo"})
    if ref.get("capture_method") != "camera":
        raise AssociateError("Only a photo taken with the in-app camera can set the site location — this one "
                             "came from the gallery.", code="no_gps_on_photo", status_code=422, field="photo_slot",
                             extra={"reason": "not_camera"})
    if ref.get("latitude") is None or ref.get("longitude") is None:
        raise AssociateError("That photo has no GPS position.", code="no_gps_on_photo", status_code=422,
                             field="photo_slot", extra={"reason": "no_gps"})
    return ref


def _parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def set_location(db: Session, o: CustomerOnboarding, performed_by: str, body: dict, *,
                 resolve_link=None) -> list[dict]:
    """The associate sets the site location (owner check is the route's). Returns
    location warnings. Caller commits. `resolve_link(link) -> (lat, lon, precision)`
    is injected by the route (so tests can stub the network layer)."""
    _require_editable(o)
    loc = config.location()
    source = SiteLocationSource(body["source"])
    accuracy = captured = raw = None
    precision = SiteLocationPrecision.exact
    if source == SiteLocationSource.device_gps:
        lat, lon = check(body["latitude"], body["longitude"])
        accuracy, captured = float(body["accuracy_m"]), _parse_time(body.get("captured_at"))
    elif source == SiteLocationSource.photo_gps:
        ref = _photo_fix(db.get(ProposalInputs, o.proposal_inputs_id), body["photo_slot"])
        lat, lon = check(ref["latitude"], ref["longitude"])
        accuracy = ref.get("accuracy_m")
        captured = _parse_time(ref.get("client_captured_at")) or _parse_time(ref.get("received_at"))
        raw = f"photo:{body['photo_slot']}"
    elif source == SiteLocationSource.google_maps_link:
        raw = body["link"].strip()
        lat, lon, prec = resolve_link(raw)
        lat, lon = check(lat, lon)
        precision = SiteLocationPrecision(prec)
    else:
        raw = body["text"].strip()
        parsed = geo.parse_coordinate_text(raw)
        if parsed is None:
            raise AssociateError('Paste the coordinates as "latitude, longitude", e.g. "22.5726, 88.3639".',
                                 code="invalid_coordinates", status_code=422, field="text")
        lat, lon = check(*parsed)
    if source in GPS_SOURCES and accuracy is not None and accuracy > loc["accuracy_reject_m"]:
        raise AssociateError(f"The GPS fix is only accurate to about {accuracy:.0f} m. Step outside, wait a few "
                             "seconds and try again.", code="accuracy_too_low", status_code=422, field="accuracy_m",
                             extra={"accuracy_m": accuracy, "accuracy_reject_m": loc["accuracy_reject_m"]})
    verified = source in GPS_SOURCES and accuracy is not None and accuracy <= loc["accuracy_good_m"]

    before, after = _store(db, o, lat=lat, lon=lon, source=source, accuracy_m=accuracy, captured_at=captured,
                           verified=verified, raw_input=raw, precision=precision)
    status = _back_to_draft(db, o, performed_by)
    if status:
        before["status"], after["status"] = status["before"], status["after"]
    db.flush()
    _audit(db, o.id, "site_location_set", performed_by, before=before, after={**after, "raw_input": raw})
    _after_change(db, o, before, performed_by,
                  f"Site location set from {_LABELS[source]} ({'verified' if verified else 'unverified'}) — "
                  "awaiting the associate's confirmation")
    _rescore(db, o, performed_by)
    return location_warnings(db, o)


def confirm(db: Session, o: CustomerOnboarding, performed_by: str) -> None:
    """The associate confirms "this is the customer's house"."""
    _require_editable(o)
    if not is_present(o):
        raise AssociateError("Set the site location first.", code="site_location_not_set", status_code=409)
    check(o.site_latitude, o.site_longitude)
    if o.site_location_confirmed_at is not None:
        return
    o.site_location_confirmed_at = _now()
    o.site_location_confirmed_by = performed_by
    db.flush()
    _audit(db, o.id, "site_location_confirmed", performed_by, before={"confirmed_at": None},
           after={"confirmed_at": _iso(o.site_location_confirmed_at), "confirmed_by": performed_by,
                  "source": o.site_location_source.value if o.site_location_source else None,
                  "verified": o.site_location_verified})
    MemoryManager(db).add_timeline_event(lead_id=o.lead_id, event_type=TimelineEventType.site_location_updated,
                                         description="Site location confirmed by the associate as the "
                                                     "customer's house", created_by=performed_by)
    _rescore(db, o, performed_by)


def staff_correct(db: Session, o: CustomerOnboarding, employee_id: str, body: dict, *, resolve_link) -> list[dict]:
    """Staff correct the location during the verification call. Stored as staff_entered,
    unverified, confirmed by that staff member. Does not change the status."""
    if o.status == OnboardingStatus.closed:
        raise AssociateError("This customer record is closed.", code="not_editable", status_code=409)
    reason = (body.get("reason") or "").strip()
    if len(reason) < 5:
        raise AssociateError("Say why the location is being corrected (at least 5 characters).",
                             code="reason_required", status_code=422, field="reason")
    precision = SiteLocationPrecision.exact
    if body["source"] == "google_maps_link":
        raw = body["link"].strip()
        lat, lon, prec = resolve_link(raw)
        precision = SiteLocationPrecision(prec)
    else:
        raw = body["text"].strip()
        parsed = geo.parse_coordinate_text(raw)
        if parsed is None:
            raise AssociateError('Paste the coordinates as "latitude, longitude", e.g. "22.5726, 88.3639".',
                                 code="invalid_coordinates", status_code=422, field="text")
        lat, lon = parsed
    lat, lon = check(lat, lon)
    before, after = _store(db, o, lat=lat, lon=lon, source=SiteLocationSource.staff_entered, raw_input=raw,
                           precision=precision, confirmed_by=employee_id)
    db.flush()
    _audit(db, o.id, "site_location_corrected", employee_id, before=before,
           after={**after, "raw_input": raw, "reason": reason, "input_source": body["source"]})
    MemoryManager(db).add_timeline_event(lead_id=o.lead_id, event_type=TimelineEventType.site_location_updated,
                                         description=f"Site location corrected by staff: {reason}",
                                         created_by=employee_id)
    _rescore(db, o, employee_id)
    return location_warnings(db, o)


def apply_legacy(db: Session, o: CustomerOnboarding, lat: Optional[float], lon: Optional[float],
                 performed_by: str) -> tuple[dict, dict]:
    """A raw site_latitude/site_longitude pair on POST/PATCH: stored as unknown_legacy,
    unverified, unconfirmed (so the submit gate still applies). Caller audits the
    returned (before, after) with its own update and calls _after_change."""
    return _store(db, o, lat=lat, lon=lon,
                  source=SiteLocationSource.unknown_legacy if lat is not None else None)


def _rescore(db: Session, o: CustomerOnboarding, performed_by: str) -> None:
    from app.onboarding import scoring
    scoring.rescore(db, o, performed_by)


# ── Reading ──────────────────────────────────────────────────────────────────────

def camera_fixes(inputs: ProposalInputs) -> list[tuple[float, float]]:
    out = []
    for slot in INSTALLATION_PHOTO_SLOTS:
        ref = (inputs.installation_photo_refs or {}).get(slot) or {}
        if ref.get("storage_key") and ref.get("capture_method") == "camera" \
                and ref.get("latitude") is not None and ref.get("longitude") is not None:
            out.append((float(ref["latitude"]), float(ref["longitude"])))
    return out


def photo_distance_m(o: CustomerOnboarding, inputs: ProposalInputs) -> Optional[float]:
    """Distance from the site pin to the median (per axis) of the camera photos' fixes."""
    fixes = camera_fixes(inputs)
    if not is_present(o) or not fixes:
        return None
    mlat = statistics.median(f[0] for f in fixes)
    mlon = statistics.median(f[1] for f in fixes)
    return round(geo.haversine_m(o.site_latitude, o.site_longitude, mlat, mlon), 1)


def flags(o: CustomerOnboarding, inputs: ProposalInputs, distance: Optional[float] = None) -> list[str]:
    if not is_present(o):
        return ["location_missing"]
    loc = config.location()
    out = []
    if o.site_location_accuracy_m is not None and o.site_location_accuracy_m > loc["accuracy_good_m"]:
        out.append("low_accuracy")
    if o.site_location_accuracy_m is None and o.site_location_source in GPS_SOURCES:
        out.append("accuracy_unknown")
    if o.site_location_precision == SiteLocationPrecision.viewport_center:
        out.append("approximate_location")
    if o.site_location_source in UNVERIFIED_SOURCES or o.site_location_source is None:
        out.append("unverified_source")
    if distance is not None and distance > loc["photo_mismatch_m"]:
        out.append("far_from_photos")
    return out


_WARNING_TEXT = {
    "low_accuracy": "The GPS fix is not precise. If you can, take it again outdoors.",
    "accuracy_unknown": "The photo did not record how precise its GPS fix was.",
    "approximate_location": "This link points at the map view, not a pin — the location is approximate. Drop a "
                            "pin on the house and share that instead.",
    "unverified_source": "This location was not taken by the phone's GPS at the site.",
    "far_from_photos": "The site location is far from where the site photos were taken. Check it is the "
                       "customer's house.",
}


def location_warnings(db: Session, o: CustomerOnboarding) -> list[dict]:
    inputs = db.get(ProposalInputs, o.proposal_inputs_id)
    out = [{"code": f, "message_key": f"site_location.warning.{f}", "message": _WARNING_TEXT[f]}
           for f in flags(o, inputs, photo_distance_m(o, inputs)) if f in _WARNING_TEXT]
    acc = o.site_location_accuracy_m
    if acc is not None and acc > config.location()["accuracy_warn_m"]:
        out.append({"code": "accuracy_poor", "message_key": "site_location.warning.accuracy_poor",
                    "message": f"The GPS fix is only accurate to about {acc:.0f} m. Try again outdoors."})
    return out


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


def summary(o: CustomerOnboarding, inputs: ProposalInputs, *, full: bool, staff: bool = False) -> dict:
    """The `site_location` object. `full=False` (a Preferred Associate looking at a
    downline customer): only whether it is there and confirmed."""
    present, confirmed = is_present(o), is_confirmed(o)
    if not full:
        return {"present": present, "confirmed": confirmed}
    distance = photo_distance_m(o, inputs)
    found = flags(o, inputs, distance)
    data = {"present": present, "latitude": o.site_latitude, "longitude": o.site_longitude,
            "source": o.site_location_source.value if o.site_location_source and present else None,
            "accuracy_m": o.site_location_accuracy_m, "captured_at": _iso(o.site_location_captured_at),
            "verified": bool(o.site_location_verified) and present, "confirmed": confirmed,
            "confirmed_at": _iso(o.site_location_confirmed_at), "confirmed_by": o.site_location_confirmed_by,
            "precision": o.site_location_precision.value if o.site_location_precision else None,
            "flags": found, "photo_distance_m": distance,
            "maps_url": maps_url(o.site_latitude, o.site_longitude) if present else None}
    if staff:
        # The verification call should confirm the address with the customer.
        data["check_with_customer"] = bool(set(found) & CHECK_WITH_CUSTOMER) or not confirmed
    return data


def submit_gaps(o: CustomerOnboarding) -> dict:
    """{missing_site_location, site_location_unconfirmed} for the submit gate."""
    if not config.location()["required_to_submit"]:
        return {"missing_site_location": False, "site_location_unconfirmed": False}
    present = is_present(o)
    return {"missing_site_location": not present, "site_location_unconfirmed": present and not is_confirmed(o)}


def options() -> dict:
    loc = config.location()
    return {"accuracy_good_m": loc["accuracy_good_m"], "accuracy_warn_m": loc["accuracy_warn_m"],
            "accuracy_reject_m": loc["accuracy_reject_m"], "sample_seconds": loc["sample_seconds"],
            "required_to_submit": loc["required_to_submit"], "sources": list(ASSOCIATE_SOURCES),
            "recommended_source": "photo_gps"}
