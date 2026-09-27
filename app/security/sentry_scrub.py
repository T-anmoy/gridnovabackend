"""Keep KYC values out of Sentry.

The associate registration forms carry an Aadhaar number, a PAN, a bank account
number and an IFSC. With Sentry's defaults, an unhandled error during one of those
requests sends the parsed form in `request.data` — the full Aadhaar number included
— and the stack frames' local variables as well. Three layers stop that:

1. max_request_body_size="never": no request body is attached to any event, on any
   endpoint.
2. Frames from the registration code (app.api.routes.public_associates and
   app.associates.*) lose their local variables — the raw form lives there.
3. before_send scrubs whatever still reaches an event: the sensitive keys wherever
   they appear, and any Aadhaar-shaped (12 digits passing the Verhoeff check) or
   PAN-shaped value inside any string.

Each layer is independently sufficient for request data; together they also cover a
value that ends up somewhere unexpected (an exception message, a breadcrumb).
"""
from __future__ import annotations

import re
from typing import Any, Optional

REDACTED = "[Filtered]"

# Form field names on the registration endpoints, plus the names the same values
# carry inside the service code.
SENSITIVE_KEYS = frozenset({
    "aadhaar_number", "aadhaar", "pan", "bank_account_number", "bank_account", "bank_ifsc", "ifsc",
    "bank_account_encrypted",
})
_NO_LOCALS_MODULES = ("app.api.routes.public_associates", "app.associates")

# 12 digits, optionally grouped 4-4-4 by spaces or hyphens as printed on the card.
_AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
_PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}\d{4}[A-Za-z](?![A-Za-z0-9])")


def _scrub_text(value: str) -> str:
    from app.associates.validation import verhoeff_valid

    def _aadhaar(m: re.Match) -> str:
        digits = "".join(m.groups())
        return REDACTED if verhoeff_valid(digits) else m.group(0)

    return _PAN_RE.sub(REDACTED, _AADHAAR_RE.sub(_aadhaar, value))


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: (REDACTED if isinstance(k, str) and k.lower() in SENSITIVE_KEYS else _scrub(v))
                for k, v in node.items()}
    if isinstance(node, list):
        # Form data can also arrive as [key, value] pairs.
        if len(node) == 2 and isinstance(node[0], str) and node[0].lower() in SENSITIVE_KEYS:
            return [node[0], REDACTED]
        return [_scrub(v) for v in node]
    if isinstance(node, tuple):
        return tuple(_scrub(list(node)))
    if isinstance(node, str):
        return _scrub_text(node)
    return node


def _drop_registration_locals(event: dict) -> None:
    for exc in (event.get("exception") or {}).get("values") or []:
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            if str(frame.get("module") or "").startswith(_NO_LOCALS_MODULES):
                frame.pop("vars", None)
    for thread in (event.get("threads") or {}).get("values") or []:
        for frame in (thread.get("stacktrace") or {}).get("frames") or []:
            if str(frame.get("module") or "").startswith(_NO_LOCALS_MODULES):
                frame.pop("vars", None)


def scrub_event(event: dict, hint: Optional[dict]) -> dict:
    """Sentry before_send / before_send_transaction hook. Never drops the event."""
    try:
        _drop_registration_locals(event)
        return _scrub(event)
    except Exception:  # a scrubbing bug must not leak the unscrubbed event
        return {"message": "Sentry event withheld: scrubbing failed", "level": "error"}


def sentry_init_options(dsn: str, environment: str) -> dict:
    """Everything app.main passes to sentry_sdk.init()."""
    return {
        "dsn": dsn,
        "environment": environment,
        "send_default_pii": False,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }
