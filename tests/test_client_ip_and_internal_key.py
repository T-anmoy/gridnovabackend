"""Proxy-aware client IP (app/security/client_ip.py) and the server-to-server key
(app/security/internal_key.py) on the two routes that honour it."""
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

import app.api.routes.chat as chat
from app.config import settings
from app.security.client_ip import client_ip
from tests.conftest import admin_headers, engineer_headers, sales_headers

CHAT = "/api/v1/chat/message"
ESTIMATE = "/api/v1/public/estimate-request"
STALE = "/api/v1/dashboard/internal/check-stale-leads"
KEY = "test-internal-key-5f2c"


def _request(headers: dict, peer: str = "169.254.1.1") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 12345),
    })


def _estimate(email: str) -> dict:
    return {"name": "Visitor", "email": email}


@pytest.fixture
def hops(monkeypatch):
    def _set(n: int):
        monkeypatch.setattr(settings, "trusted_proxy_hops", n)
    return _set


@pytest.fixture
def internal_key(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", KEY)
    return KEY


# ── client_ip ──────────────────────────────────────────────────────────────────

class TestClientIp:
    def test_default_ignores_forwarded_for_entirely(self, hops):
        hops(0)
        assert client_ip(_request({"X-Forwarded-For": "203.0.113.9"})) == "169.254.1.1"

    def test_one_hop_takes_the_entry_the_front_end_appended(self, hops):
        hops(1)
        assert client_ip(_request({"X-Forwarded-For": "203.0.113.9"})) == "203.0.113.9"

    def test_client_supplied_entries_on_the_left_are_never_used(self, hops):
        hops(1)
        spoofed = _request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 203.0.113.9"})
        assert client_ip(spoofed) == "203.0.113.9"

    def test_two_hops_for_a_load_balancer_in_front(self, hops):
        hops(2)
        r = _request({"X-Forwarded-For": "1.2.3.4, 203.0.113.9, 130.211.0.1"})
        assert client_ip(r) == "203.0.113.9"

    def test_missing_or_short_header_falls_back_to_the_peer(self, hops):
        hops(2)
        assert client_ip(_request({})) == "169.254.1.1"
        assert client_ip(_request({"X-Forwarded-For": "203.0.113.9"})) == "169.254.1.1"

    def test_non_ip_entry_falls_back_to_the_peer(self, hops):
        hops(1)
        assert client_ip(_request({"X-Forwarded-For": "not-an-ip"})) == "169.254.1.1"

    def test_ipv6_is_normalised(self, hops):
        hops(1)
        assert client_ip(_request({"X-Forwarded-For": "2001:DB8::1"})) == "2001:db8::1"

    def test_rate_limiters_use_it(self, client, hops):
        """Two visitors behind the same front end get separate buckets."""
        hops(1)
        chat._request_log["203.0.113.9"] = [datetime.now(timezone.utc)] * chat.RATE_LIMIT
        r = client.post(CHAT, json={"message": "hi"}, headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 429
        r = client.post(CHAT, json={"message": "hi"}, headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 200


# ── estimate-request ───────────────────────────────────────────────────────────

class TestEstimateRequestInternalKey:
    def test_without_key_per_ip_limit_applies(self, client):
        for i in range(10):
            assert client.post(ESTIMATE, json=_estimate(f"a{i}@example.com")).status_code == 200
        assert client.post(ESTIMATE, json=_estimate("a-over@example.com")).status_code == 429

    def test_valid_key_skips_the_per_ip_limit(self, client, internal_key):
        for i in range(15):
            r = client.post(ESTIMATE, json=_estimate(f"b{i}@example.com"),
                            headers={"X-Internal-Key": internal_key})
            assert r.status_code == 200, (i, r.text)

    def test_valid_key_keeps_the_email_cooldown(self, client, internal_key):
        h = {"X-Internal-Key": internal_key}
        assert client.post(ESTIMATE, json=_estimate("same@example.com"), headers=h).status_code == 200
        assert client.post(ESTIMATE, json=_estimate("same@example.com"), headers=h).status_code == 429

    def test_customer_ip_becomes_the_rate_limit_identity(self, client, internal_key):
        a = {"X-Internal-Key": internal_key, "X-Customer-IP": "203.0.113.9"}
        b = {"X-Internal-Key": internal_key, "X-Customer-IP": "198.51.100.7"}
        for i in range(10):
            assert client.post(ESTIMATE, json=_estimate(f"c{i}@example.com"), headers=a).status_code == 200
        assert client.post(ESTIMATE, json=_estimate("c-over@example.com"), headers=a).status_code == 429
        # A different visitor through the same server is unaffected.
        assert client.post(ESTIMATE, json=_estimate("d0@example.com"), headers=b).status_code == 200

    def test_wrong_key_is_treated_as_absent(self, client, internal_key):
        h = {"X-Internal-Key": "wrong", "X-Customer-IP": "203.0.113.9"}
        for i in range(10):
            assert client.post(ESTIMATE, json=_estimate(f"e{i}@example.com"), headers=h).status_code == 200
        # Per-IP limit on the real peer applies, and X-Customer-IP was not honoured:
        # a fresh customer IP does not get a fresh bucket.
        h2 = {"X-Internal-Key": "wrong", "X-Customer-IP": "198.51.100.7"}
        assert client.post(ESTIMATE, json=_estimate("e-over@example.com"), headers=h2).status_code == 429

    def test_blank_key_setting_disables_the_feature(self, client, monkeypatch):
        monkeypatch.setattr(settings, "internal_api_key", "")
        h = {"X-Internal-Key": "", "X-Customer-IP": "203.0.113.9"}
        for i in range(10):
            assert client.post(ESTIMATE, json=_estimate(f"f{i}@example.com"), headers=h).status_code == 200
        assert client.post(ESTIMATE, json=_estimate("f-over@example.com"), headers=h).status_code == 429

    def test_non_ascii_key_never_500s(self, client, internal_key):
        r = client.post(ESTIMATE, json=_estimate("g@example.com"),
                        headers={"X-Internal-Key": "kéy".encode("latin-1")})
        assert r.status_code == 200


# ── check-stale-leads ──────────────────────────────────────────────────────────

class TestStaleLeadsInternalKey:
    def test_valid_key_without_jwt_is_accepted(self, client, internal_key):
        r = client.post(STALE, headers={"X-Internal-Key": internal_key})
        assert r.status_code == 200
        assert set(r.json()) >= {"lead_stale_48h", "lead_stale_5d", "checked"}

    def test_wrong_key_is_treated_as_absent(self, client, internal_key):
        assert client.post(STALE, headers={"X-Internal-Key": "wrong"}).status_code == 401

    def test_blank_key_setting_ignores_the_header(self, client, monkeypatch):
        monkeypatch.setattr(settings, "internal_api_key", "")
        assert client.post(STALE, headers={"X-Internal-Key": ""}).status_code == 401
        assert client.post(STALE, headers={"X-Internal-Key": "anything"}).status_code == 401

    def test_admin_path_unchanged(self, client, internal_key):
        assert client.post(STALE, headers=admin_headers()).status_code == 200
        assert client.post(STALE, headers={**admin_headers(), "X-Internal-Key": "wrong"}).status_code == 200

    @pytest.mark.parametrize("headers", [sales_headers(), engineer_headers()])
    def test_non_admin_still_forbidden(self, client, internal_key, headers):
        assert client.post(STALE, headers=headers).status_code == 403
        assert client.post(STALE, headers={**headers, "X-Internal-Key": "wrong"}).status_code == 403

    def test_non_ascii_key_never_500s(self, client, internal_key):
        r = client.post(STALE, headers={"X-Internal-Key": "kéy".encode("latin-1")})
        assert r.status_code == 401
