"""Phase 3C Step 3 — safe Google Maps short-link resolution (app/onboarding/maps_link.py).

Nothing here touches the network: HTTP goes through httpx.MockTransport and DNS through
the module's _getaddrinfo seam. A guard fixture makes any real socket lookup fail the
test.
"""
import socket

import httpx
import pytest

from app.onboarding import maps_link
from app.onboarding.maps_link import LinkError

SHORT = "https://maps.app.goo.gl/AbCdEf123"
PLACE = "https://www.google.com/maps/place/22.5726,88.3639"
PUBLIC = ["142.250.183.110"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to resolve a real host")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(maps_link, "_getaddrinfo", lambda host: PUBLIC)
    maps_link.reset_rate_limits()
    yield
    maps_link.reset_rate_limits()


class Recorder:
    """A MockTransport handler: `routes` maps a URL to (status, headers, body)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        status, headers, body = self.routes.get(str(request.url), (404, {}, b""))
        return httpx.Response(status, headers=headers, content=body)


def _transport(routes) -> tuple[httpx.MockTransport, Recorder]:
    rec = Recorder(routes)
    return httpx.MockTransport(rec), rec


def _code(exc_info) -> tuple[str, str]:
    return exc_info.value.code, exc_info.value.reason


class TestValidation:
    @pytest.mark.parametrize("url,reason", [
        ("http://maps.app.goo.gl/AbC", "scheme"),
        ("ftp://maps.app.goo.gl/AbC", "scheme"),
        ("https://google.com.evil.tld/maps?q=22.5,88.3", "host_not_allowed"),
        ("https://evil-google.com/maps", "host_not_allowed"),
        ("https://maps.app.goo.gl.evil.tld/x", "host_not_allowed"),
        ("https://www.google.com./maps?q=22.5,88.3", "host_not_allowed"),
        ("https://user@maps.app.goo.gl/AbC", "userinfo"),
        ("https://maps.app.goo.gl@evil.tld/AbC", "userinfo"),
        ("https://maps.app.goo.gl:8443/AbC", "port"),
        ("https://maps.app.goo.gl/" + "a" * 600, "too_long"),
        ("https://maps.app.goo.gl/a b", "malformed"),
        ("", "empty"),
    ])
    def test_refused_without_any_request(self, url, reason):
        transport, rec = _transport({})
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(url, who="a", transport=transport)
        assert _code(exc) == ("invalid_link", reason) and exc.value.status_code == 422
        assert rec.calls == []

    def test_explicit_443_is_fine(self):
        assert maps_link.validate_url("https://www.google.com:443/maps?q=22.5,88.3")

    def test_link_with_coordinates_never_touches_the_network(self):
        transport, rec = _transport({})
        assert maps_link.coordinates_from_link(PLACE, who="a", transport=transport) == (22.5726, 88.3639, "exact")
        assert rec.calls == []


class TestResolution:
    def test_coordinates_after_one_hop(self):
        transport, rec = _transport({SHORT: (302, {"location": PLACE}, b"")})
        assert maps_link.coordinates_from_link(SHORT, who="a", transport=transport) == (22.5726, 88.3639, "exact")
        assert [str(c.url) for c in rec.calls] == [SHORT]

    def test_coordinates_after_two_hops(self):
        mid = "https://www.google.com/maps/place/Some+Name"
        transport, rec = _transport({SHORT: (301, {"location": mid}, b""),
                                     mid: (302, {"location": PLACE}, b"")})
        assert maps_link.coordinates_from_link(SHORT, who="a", transport=transport)[:2] == (22.5726, 88.3639)
        assert [str(c.url) for c in rec.calls] == [SHORT, mid]

    def test_relative_location_is_resolved_against_the_current_url(self):
        transport, _ = _transport({SHORT: (302, {"location": "/maps/place/22.5,88.3"}, b"")})
        # maps.app.goo.gl + a relative path stays on an allowlisted host.
        assert maps_link.coordinates_from_link(SHORT, who="a", transport=transport)[:2] == (22.5, 88.3)

    def test_redirect_chain_longer_than_three_is_refused(self):
        hops = [f"https://www.google.com/maps/place/hop{i}" for i in range(5)]
        routes = {SHORT: (302, {"location": hops[0]}, b"")}
        routes.update({hops[i]: (302, {"location": hops[i + 1]}, b"") for i in range(4)})
        transport, rec = _transport(routes)
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert _code(exc) == ("link_unresolvable", "too_many_redirects")
        assert len(rec.calls) == 3

    def test_redirect_to_a_non_allowlisted_host_is_not_followed(self):
        transport, rec = _transport({SHORT: (302, {"location": "https://evil.tld/maps?q=22.5,88.3"}, b"")})
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert _code(exc) == ("invalid_link", "host_not_allowed")
        assert len(rec.calls) == 1

    def test_redirect_to_http_is_not_followed(self):
        transport, rec = _transport({SHORT: (302, {"location": "http://www.google.com/maps/place/x"}, b"")})
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert _code(exc) == ("invalid_link", "scheme") and len(rec.calls) == 1

    def test_redirect_to_a_host_resolving_to_a_private_ip_is_refused(self, monkeypatch):
        mid = "https://www.google.com/maps/place/Some+Name"
        transport, rec = _transport({SHORT: (302, {"location": mid}, b"")})
        monkeypatch.setattr(maps_link, "_getaddrinfo",
                            lambda host: PUBLIC if host == "maps.app.goo.gl" else ["10.0.0.7"])
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert _code(exc) == ("link_unresolvable", "blocked_address")
        assert [str(c.url) for c in rec.calls] == [SHORT]   # the private host was never requested

    @pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fe80::1",
                                         "224.0.0.1", "0.0.0.0", "100.64.0.1", "fc00::1"])
    def test_non_public_addresses_are_refused(self, monkeypatch, address):
        monkeypatch.setattr(maps_link, "_getaddrinfo", lambda host: PUBLIC + [address])
        transport, rec = _transport({})
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert _code(exc) == ("link_unresolvable", "blocked_address") and rec.calls == []

    def test_dns_failure(self, monkeypatch):
        def fail(host):
            raise socket.gaierror("nope")
        monkeypatch.setattr(maps_link, "_getaddrinfo", fail)
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=_transport({})[0])
        assert _code(exc) == ("link_unresolvable", "dns")

    def test_timeout(self):
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=httpx.MockTransport(slow))
        assert _code(exc) == ("link_unresolvable", "timeout") and exc.value.status_code == 422

    def test_total_budget_is_enforced(self, monkeypatch):
        mid = "https://www.google.com/maps/place/Some+Name"
        transport, rec = _transport({SHORT: (302, {"location": mid}, b"")})
        clock = iter([0.0, 0.1, 99.0, 99.0])
        monkeypatch.setattr(maps_link.time, "monotonic", lambda: next(clock))
        with pytest.raises(LinkError) as exc:
            maps_link.resolve_maps_link(SHORT, transport=transport)
        assert _code(exc) == ("link_unresolvable", "timeout") and len(rec.calls) == 1

    def test_each_request_carries_the_remaining_budget_and_no_cookies(self):
        mid = "https://www.google.com/maps/place/Some+Name"
        transport, rec = _transport({SHORT: (302, {"location": mid, "set-cookie": "NID=abc; Path=/"}, b""),
                                     mid: (302, {"location": PLACE}, b"")})
        maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert all("cookie" not in c.headers for c in rec.calls)
        timeouts = [c.extensions["timeout"]["read"] for c in rec.calls]
        assert all(0 < t <= maps_link.TOTAL_TIMEOUT_S for t in timeouts)

    def test_network_error_is_a_422_not_a_500(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=httpx.MockTransport(boom))
        assert _code(exc) == ("link_unresolvable", "network")


class TestBodyIsNeverRead:
    def test_body_with_coordinates_is_ignored(self):
        class Exploding(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError("the response body was read")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=Exploding())
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=httpx.MockTransport(handler))
        assert exc.value.code == "no_coordinates_in_link"
        body = exc.value.body()
        assert body["message_key"] == "site_location.no_coordinates_in_link" and body["guidance_keys"]

    def test_final_url_without_coordinates(self):
        transport, _ = _transport({SHORT: (302, {"location": "https://www.google.com/maps/place/Some+Name"}, b""),
                                   "https://www.google.com/maps/place/Some+Name": (200, {}, b"!3d22.5!4d88.3")})
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert exc.value.code == "no_coordinates_in_link"


class TestRateLimit:
    def test_per_associate_hourly_limit(self, monkeypatch):
        from app.onboarding import config
        monkeypatch.setitem(config.location(), "link_resolve_rate_limit", 2)
        transport, _ = _transport({SHORT: (302, {"location": PLACE}, b"")})
        for _ in range(2):
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        with pytest.raises(LinkError) as exc:
            maps_link.coordinates_from_link(SHORT, who="a", transport=transport)
        assert exc.value.code == "link_rate_limited" and exc.value.status_code == 429
        # Another associate has their own allowance; a link with coordinates costs nothing.
        maps_link.coordinates_from_link(SHORT, who="b", transport=transport)
        maps_link.coordinates_from_link(PLACE, who="a", transport=transport)
