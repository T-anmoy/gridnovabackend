"""Coordinates from a pasted Google Maps link — the one place this backend makes an
outbound request on a user-supplied URL, so it is deliberately narrow.

A link that already carries coordinates (app/geo.parse_maps_url) is parsed with no
network at all. Otherwise — a mobile "Share → Copy link" short link such as
https://maps.app.goo.gl/… — the redirects are followed by hand:

  * PRIMARY CONTROL: the host must EXACTLY equal one in location.link_hosts_allowlist
    (config/associate_onboarding.yaml) — no suffix/substring matching — on the pasted
    URL and on every redirect target. https only; no user-info; no port but 443;
    bounded URL length.
  * At most MAX_REDIRECTS redirects, each re-validated; TOTAL_TIMEOUT_S for the whole
    resolution; no cookies sent (each request is built fresh, never from the client's
    jar); no proxy from the environment; only the `Location` header is read — a
    response body is never downloaded or parsed (the response is opened streaming and
    closed unread).
  * Defence in depth: the host is resolved first and refused if ANY address is not a
    public unicast address (private, loopback, link-local, multicast, reserved…).
    httpx resolves again when it connects, so this is not proof against DNS
    rebinding — the exact-host allowlist is what actually confines the requests.
  * Per-associate rate limit (location.link_resolve_rate_limit per hour) on links that
    need a request. Process-local, like the chat limiter: each worker counts its own.

Every failure is a LinkError (422, or 429 for the rate limit) — never a 500.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import deque
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from app import geo
from app.onboarding import config

MAX_REDIRECTS = 3
TOTAL_TIMEOUT_S = 5.0
MAX_INPUT_LENGTH = 500          # what an associate may paste (also the raw_input column)
MAX_HOP_LENGTH = 4096           # a redirect target (expanded place URLs are long)
RATE_WINDOW_S = 3600
_HEADERS = {"User-Agent": "GridNova-LinkResolver/1.0", "Accept": "*/*"}
GUIDANCE_KEYS = ["site_location.guidance.drop_pin_and_share", "site_location.guidance.use_device_gps",
                 "site_location.guidance.paste_coordinates"]


class LinkError(Exception):
    """code: invalid_link | link_unresolvable | no_coordinates_in_link | link_rate_limited."""

    def __init__(self, code: str, message: str, *, status_code: int = 422, reason: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.status_code, self.reason = code, message, status_code, reason

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message, "message_key": f"site_location.{self.code}",
               "field": "link"}
        if self.reason:
            out["reason"] = self.reason
        if self.code in ("no_coordinates_in_link", "link_unresolvable"):
            out["guidance_keys"] = list(GUIDANCE_KEYS)
        return out


# ── URL validation ───────────────────────────────────────────────────────────────

def validate_url(url: str, *, max_length: int = MAX_HOP_LENGTH) -> str:
    """The URL if it may be requested/parsed, else LinkError(invalid_link)."""
    if not isinstance(url, str) or not url.strip():
        raise LinkError("invalid_link", "Paste a Google Maps link.", reason="empty")
    url = url.strip()
    if len(url) > max_length:
        raise LinkError("invalid_link", "That link is too long.", reason="too_long")
    if any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise LinkError("invalid_link", "That does not look like a single link.", reason="malformed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise LinkError("invalid_link", "That does not look like a link.", reason="malformed")
    if parts.scheme.lower() != "https":
        raise LinkError("invalid_link", "Only https Google Maps links are accepted.", reason="scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise LinkError("invalid_link", "That link is not a Google Maps link.", reason="userinfo")
    if port not in (None, 443):
        raise LinkError("invalid_link", "That link is not a Google Maps link.", reason="port")
    host = (parts.hostname or "").lower()
    if host not in set(config.location()["link_hosts_allowlist"]):
        raise LinkError("invalid_link", "Only Google Maps links are accepted.", reason="host_not_allowed")
    return url


# ── DNS (defence in depth) ───────────────────────────────────────────────────────

def _getaddrinfo(host: str) -> list[str]:
    """Seam for tests."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def _require_public(host: str) -> None:
    try:
        addresses = _getaddrinfo(host)
    except (OSError, UnicodeError):
        raise LinkError("link_unresolvable", "Could not open that link.", reason="dns")
    if not addresses:
        raise LinkError("link_unresolvable", "Could not open that link.", reason="dns")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise LinkError("link_unresolvable", "Could not open that link.", reason="blocked_address")
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local \
                or ip.is_private or ip.is_unspecified:
            raise LinkError("link_unresolvable", "Could not open that link.", reason="blocked_address")


# ── Rate limit ───────────────────────────────────────────────────────────────────

_rate: dict[str, deque] = {}
_rate_lock = threading.Lock()


def reset_rate_limits() -> None:
    with _rate_lock:
        _rate.clear()


def _take_rate_token(who: str) -> None:
    limit = config.location()["link_resolve_rate_limit"]
    now = time.monotonic()
    with _rate_lock:
        q = _rate.setdefault(who, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            raise LinkError("link_rate_limited", "Too many links opened in the last hour. Paste the coordinates, "
                                                 "or use the phone's GPS, instead.", status_code=429)
        q.append(now)


# ── Resolution ───────────────────────────────────────────────────────────────────

def coordinates_from_link(link: str, *, who: str,
                          transport: Optional[httpx.BaseTransport] = None) -> tuple[float, float, str]:
    """(lat, lon, precision) from a pasted link. Raises LinkError."""
    url = validate_url(link, max_length=MAX_INPUT_LENGTH)
    found = geo.parse_maps_url(url)
    if found:
        return found
    _take_rate_token(who)
    final = resolve_maps_link(url, transport=transport)
    found = geo.parse_maps_url(final)
    if not found:
        raise LinkError("no_coordinates_in_link",
                        "That link does not contain the location's coordinates. In Google Maps, drop a pin on the "
                        "house and share that pin — or use the phone's GPS.")
    return found


def resolve_maps_link(url: str, *, transport: Optional[httpx.BaseTransport] = None) -> str:
    """Follow up to MAX_REDIRECTS redirects from `url`, stopping as soon as a URL carries
    coordinates. Returns the last URL reached (the caller parses it)."""
    current = validate_url(url)
    if geo.parse_maps_url(current):
        return current
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    redirects = 0
    try:
        with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LinkError("link_unresolvable", "Opening that link took too long.", reason="timeout")
                _require_public(urlsplit(current).hostname)
                client.cookies.clear()
                request = httpx.Request("GET", current, headers=_HEADERS,
                                        extensions={"timeout": httpx.Timeout(remaining).as_dict()})
                response = client.send(request, stream=True)
                try:
                    location = response.headers.get("location") if response.is_redirect else None
                finally:
                    response.close()   # the body is never read
                if not location:
                    return current
                redirects += 1
                current = validate_url(urljoin(current, location))
                if geo.parse_maps_url(current):
                    return current
                if redirects >= MAX_REDIRECTS:
                    raise LinkError("link_unresolvable", "That link redirects too many times.",
                                    reason="too_many_redirects")
    except LinkError:
        raise
    except httpx.TimeoutException:
        raise LinkError("link_unresolvable", "Opening that link took too long.", reason="timeout")
    except Exception:
        raise LinkError("link_unresolvable", "Could not open that link.", reason="network")
