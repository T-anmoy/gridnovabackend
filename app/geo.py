"""Pure geographic helpers — no network, no database.

  * haversine_m           great-circle distance in metres
  * check_point           finite, in range, not the degenerate (0,0)-ish point, inside the
                          India sanity box (config location.india_bbox) — or GeoError
  * parse_coordinate_text "22.5726, 88.3639" and friends (see the function)
  * parse_maps_url        coordinates from a Google Maps URL (see SUPPORTED_URL_FORMATS)

The India box is a sanity check only (it also covers parts of neighbouring countries);
it is not a border test.
"""
from __future__ import annotations

import math
import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

EARTH_RADIUS_M = 6_371_008.8
DEGENERATE_DEG = 0.001


class GeoError(ValueError):
    """`code`: invalid_coordinates | outside_india. `likely_swapped`: the pair would be
    inside India with latitude and longitude exchanged."""

    def __init__(self, code: str, message: str, *, likely_swapped: bool = False):
        super().__init__(message)
        self.code = code
        self.likely_swapped = likely_swapped


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def in_bbox(lat: float, lon: float, bbox: dict) -> bool:
    return bbox["lat_min"] <= lat <= bbox["lat_max"] and bbox["lon_min"] <= lon <= bbox["lon_max"]


def check_point(lat, lon, bbox: dict) -> tuple[float, float]:
    """Return (lat, lon) as floats, or raise GeoError."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise GeoError("invalid_coordinates", "The coordinates are not numbers.")
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise GeoError("invalid_coordinates", "The coordinates are not finite numbers.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeoError("invalid_coordinates", "Latitude must be -90..90 and longitude -180..180.")
    if abs(lat) < DEGENERATE_DEG or abs(lon) < DEGENERATE_DEG:
        raise GeoError("invalid_coordinates", "The location is (0, 0) or on the equator/prime meridian — "
                                              "the device did not get a real fix.")
    if not in_bbox(lat, lon, bbox):
        swapped = in_bbox(lon, lat, bbox)
        raise GeoError("outside_india",
                       "These coordinates are outside India." + (
                           " Latitude and longitude look swapped — latitude comes first." if swapped else ""),
                       likely_swapped=swapped)
    return lat, lon


# ── Pasted text ──────────────────────────────────────────────────────────────────

_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
_TEXT = re.compile(
    rf"^\s*(?P<a>{_NUM})\s*°?\s*(?P<ah>[NSns])?\s*[,;\s]\s*(?P<b>{_NUM})\s*°?\s*(?P<bh>[EWew])?\s*$")


def parse_coordinate_text(text: str) -> Optional[tuple[float, float]]:
    """Decimal degrees, latitude first: "22.5726, 88.3639", "22.5726 88.3639",
    "22.5726°N, 88.3639°E", "22.5726N 88.3639E", "-33.9, 151.2". A hemisphere letter
    S or W negates. Degrees-minutes-seconds is NOT supported. None when unparseable
    (range/India checks are check_point's job)."""
    if not isinstance(text, str) or len(text) > 100:
        return None
    m = _TEXT.match(text.replace(" ", " "))
    if not m:
        return None
    a, b = float(m["a"]), float(m["b"])
    if m["ah"] and m["ah"].upper() == "S":
        a = -abs(a)
    if m["bh"] and m["bh"].upper() == "W":
        b = -abs(b)
    return a, b


# ── Google Maps URLs ─────────────────────────────────────────────────────────────

SUPPORTED_URL_FORMATS = (
    ("pin data   …/data=…!3d<lat>!4d<lon>…", "exact (the last !3d!4d pair in the URL)"),
    ("q / query  ?q=<lat>,<lon>  ?q=loc:<lat>,<lon>  ?query=<lat>,<lon>", "exact"),
    ("place      /maps/place/<lat>,<lon>", "exact"),
    ("search     /maps/search/<lat>,<lon>", "exact"),
    ("ll         ?ll=<lat>,<lon>", "viewport_center (ll is the map centre)"),
    ("viewport   /@<lat>,<lon>,<zoom>z", "viewport_center"),
)

_PAIR = re.compile(rf"^\s*(?:loc:)?\s*({_NUM})\s*,\s*({_NUM})\s*$")
_PIN = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
_AT = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:[,/]|$)")


def _pair(value: str) -> Optional[tuple[float, float]]:
    m = _PAIR.match(unquote(value).replace("+", " "))
    return (float(m[1]), float(m[2])) if m else None


def parse_maps_url(url: str) -> Optional[tuple[float, float, str]]:
    """(lat, lon, precision) from a Google Maps URL, or None. Priority: the place pin
    (!3d!4d) → q= / query= → /place/<lat>,<lon> or /search/<lat>,<lon> → ll= → @lat,lon.
    The host is NOT checked here (callers check it). See SUPPORTED_URL_FORMATS."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = unquote(parts.path)
    pins = _PIN.findall(url) or _PIN.findall(path)
    if pins:
        lat, lon = pins[-1]
        return float(lat), float(lon), "exact"
    query = parse_qs(parts.query)
    for key in ("q", "query"):
        for value in query.get(key, []):
            found = _pair(value)
            if found:
                return found[0], found[1], "exact"
    segments = [s for s in path.split("/") if s]
    for marker in ("place", "search"):
        if marker in segments:
            i = segments.index(marker)
            if i + 1 < len(segments):
                found = _pair(segments[i + 1])
                if found:
                    return found[0], found[1], "exact"
    for value in query.get("ll", []):
        found = _pair(value)
        if found:
            return found[0], found[1], "viewport_center"
    m = _AT.search(path)
    if m:
        return float(m[1]), float(m[2]), "viewport_center"
    return None
