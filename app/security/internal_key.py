"""Server-to-server authentication with a shared key (INTERNAL_API_KEY).

Two callers are not people and cannot present a Clerk JWT: the website's server-side
lead mirror (every request comes from the hosting provider's IP) and Cloud Scheduler
(the stale-lead cron). They send the key as X-Internal-Key instead.

A blank INTERNAL_API_KEY switches the feature off entirely. A missing, wrong or
malformed key is treated exactly as if the header were absent. It never raises.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Request

from app.config import settings
from app.security.client_ip import parse_ip

INTERNAL_KEY_HEADER = "X-Internal-Key"
CUSTOMER_IP_HEADER = "X-Customer-IP"


def has_valid_internal_key(request: Request) -> bool:
    expected = settings.internal_api_key
    if not expected:
        return False
    supplied = request.headers.get(INTERNAL_KEY_HEADER)
    if not supplied:
        return False
    # Compare bytes: hmac.compare_digest rejects non-ASCII str, and a header value
    # is attacker-controlled.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def forwarded_customer_ip(request: Request) -> Optional[str]:
    """The visitor IP an internal caller vouches for via X-Customer-IP. Only call this
    after has_valid_internal_key(); a value that is not an IP address counts as absent."""
    value = request.headers.get(CUSTOMER_IP_HEADER)
    return parse_ip(value) if value else None
