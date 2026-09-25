"""The client IP every public rate limiter keys on.

Behind Cloud Run the TCP peer is Google's front end, not the visitor, so the socket
address alone would put every visitor in one bucket. The visitor's address is in
X-Forwarded-For, but that header is only partly trustworthy: a client can send its
own X-Forwarded-For, and each proxy only APPENDS to it. So the entries a client
wrote come first, and the entries our own proxies wrote are the last
TRUSTED_PROXY_HOPS. The entry the outermost trusted proxy appended, counting
TRUSTED_PROXY_HOPS from the right, is the address that proxy actually saw
connecting. Anything to the left of it is client-supplied and never used.

(uvicorn's --forwarded-allow-ips='*' is deliberately not used for this: with '*' it
takes the LEFTMOST entry, which is exactly the client-supplied one.)
"""
from __future__ import annotations

import ipaddress
from typing import Optional

from fastapi import Request

from app.config import settings

_FALLBACK = "127.0.0.1"


def parse_ip(value: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def forwarded_client_ip(request: Request, hops: int) -> Optional[str]:
    """The X-Forwarded-For entry appended by the outermost of `hops` trusted proxies,
    or None if the header is absent, too short, or that entry is not an IP address."""
    if hops <= 0:
        return None
    entries = [
        entry.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for entry in header.split(",")
        if entry.strip()
    ]
    if len(entries) < hops:
        return None
    return parse_ip(entries[-hops])


def client_ip(request: Request) -> str:
    forwarded = forwarded_client_ip(request, settings.trusted_proxy_hops)
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return _FALLBACK
