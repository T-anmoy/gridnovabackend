"""Phase 3C Step 2 — pure geo helpers (app/geo.py). No network, no database."""
import math

import pytest

from app import geo
from app.onboarding import config

BOX = config.location()["india_bbox"]


class TestHaversine:
    def test_known_distances(self):
        assert geo.haversine_m(22.5726, 88.3639, 22.5726, 88.3639) == 0
        # One degree of latitude is about 111.2 km everywhere.
        assert geo.haversine_m(22.0, 88.0, 23.0, 88.0) == pytest.approx(111_195, rel=1e-3)
        # Kolkata ↔ Delhi, roughly 1,300 km great-circle.
        assert 1_280_000 < geo.haversine_m(22.5726, 88.3639, 28.6139, 77.2090) < 1_320_000


class TestCheckPoint:
    def test_valid_point(self):
        assert geo.check_point("22.5726", 88.3639, BOX) == (22.5726, 88.3639)

    @pytest.mark.parametrize("lat,lon", [(0, 0), (0.0005, 88.3), (22.5, 0.0009), (float("nan"), 88.0),
                                         (math.inf, 88.0), (91, 88), (22, 181), ("x", 88)])
    def test_invalid(self, lat, lon):
        with pytest.raises(geo.GeoError) as exc:
            geo.check_point(lat, lon, BOX)
        assert exc.value.code == "invalid_coordinates"

    def test_outside_india(self):
        with pytest.raises(geo.GeoError) as exc:
            geo.check_point(51.5074, -0.1278, BOX)   # London
        assert exc.value.code == "outside_india" and exc.value.likely_swapped is False

    def test_swapped_inside_india_is_detected(self):
        with pytest.raises(geo.GeoError) as exc:
            geo.check_point(88.3639, 22.5726, BOX)
        assert exc.value.code == "outside_india" and exc.value.likely_swapped is True
        assert "swapped" in str(exc.value)


class TestCoordinateText:
    @pytest.mark.parametrize("text,expected", [
        ("22.5726, 88.3639", (22.5726, 88.3639)),
        ("22.5726,88.3639", (22.5726, 88.3639)),
        ("22.5726 88.3639", (22.5726, 88.3639)),
        (" 22.5726 ,  88.3639 ", (22.5726, 88.3639)),
        ("22.5726°N, 88.3639°E", (22.5726, 88.3639)),
        ("22.5726° N 88.3639° E", (22.5726, 88.3639)),
        ("22.5726N 88.3639E", (22.5726, 88.3639)),
        ("33.9S, 151.2E", (-33.9, 151.2)),
        ("40.7, 74.0W", (40.7, -74.0)),
        ("-33.9; 151.2", (-33.9, 151.2)),
        ("26 , 89", (26.0, 89.0)),
    ])
    def test_valid(self, text, expected):
        assert geo.parse_coordinate_text(text) == expected

    @pytest.mark.parametrize("text", ["", "hello", "22.5726", "22.5726, 88.3639, 10", "22°34'21\"N 88°21'50\"E",
                                      "22.5726, abc", "1234.5, 88.3", "x" * 200, "22.5 N, 88.3 N"])
    def test_invalid(self, text):
        assert geo.parse_coordinate_text(text) is None


class TestMapsUrl:
    @pytest.mark.parametrize("url,expected", [
        # Place pin data — the LAST !3d!4d pair wins (earlier ones can be other entities).
        ("https://www.google.com/maps/place/Victoria+Memorial/@22.5448,88.3404,17z/data=!3m1!4b1!4m6!3m5"
         "!1s0x3a02771346ae0ce7:0xd0b5a4bad8c3ec7b!8m2!3d22.5448082!4d88.3425578!16zL20vMGY5eHo",
         (22.5448082, 88.3425578, "exact")),
        ("https://www.google.com/maps?q=22.5726,88.3639", (22.5726, 88.3639, "exact")),
        ("https://maps.google.com/?q=22.5726%2C88.3639", (22.5726, 88.3639, "exact")),
        ("https://www.google.com/maps?q=loc:22.5726,88.3639", (22.5726, 88.3639, "exact")),
        ("https://www.google.com/maps/search/?api=1&query=22.5726,88.3639", (22.5726, 88.3639, "exact")),
        ("https://www.google.com/maps/place/22.5726,88.3639", (22.5726, 88.3639, "exact")),
        ("https://www.google.com/maps/place/22.5726,88.3639/@22.5,88.3,15z", (22.5726, 88.3639, "exact")),
        ("https://www.google.co.in/maps/search/22.5726,+88.3639", (22.5726, 88.3639, "exact")),
        ("https://maps.google.com/maps?ll=22.5726,88.3639&z=16", (22.5726, 88.3639, "viewport_center")),
        ("https://www.google.com/maps/@22.5726,88.3639,15z", (22.5726, 88.3639, "viewport_center")),
        ("https://www.google.com/maps/place/Some+Shop/@22.5726,88.3639,17z", (22.5726, 88.3639, "viewport_center")),
    ])
    def test_supported_forms(self, url, expected):
        assert geo.parse_maps_url(url) == expected

    def test_priority_pin_beats_query_and_viewport(self):
        url = "https://www.google.com/maps/place/@1.0,2.0,15z/data=!3d22.1!4d88.1?q=23.0,89.0"
        assert geo.parse_maps_url(url) == (22.1, 88.1, "exact")
        assert geo.parse_maps_url("https://www.google.com/maps/@1.5,2.5,15z?q=22.5,88.5") == (22.5, 88.5, "exact")

    @pytest.mark.parametrize("url", [
        "https://maps.app.goo.gl/AbCdEf123",                     # a short link: needs resolving
        "https://www.google.com/maps/place/Victoria+Memorial",   # a name, no coordinates
        "https://www.google.com/maps?q=Victoria+Memorial+Kolkata",
        "https://www.google.com/maps/dir/22.5,88.3/22.6,88.4",   # directions: not supported
        "not a url", "",
    ])
    def test_no_coordinates(self, url):
        assert geo.parse_maps_url(url) is None
