"""Keep KYC values out of Sentry.

The associate registration forms carry an Aadhaar number, a PAN, a bank account
number and an IFSC. With Sentry's defaults, an unhandled error during one of those
requests sends the parsed form in `request.data` — the full Aadhaar number included
— and the stack frames' local variables as well. Three layers stop that:

1. max_request_body_size="never": no request body is attached to any event, on any
   endpoint.
2. Frames from the registration code (app.api.routes.public_associates and
   app.associates.*) and the onboarding code (app.onboarding.*,
   app.api.routes.associate_onboarding, app.api.routes.onboarding_admin — which
   handle customer phone, address, consumer number and GPS) lose their local
   variables — the raw form/record lives there.
3. before_send scrubs whatever still reaches an event: the sensitive keys wherever
   they appear, and any Aadhaar-shaped (12 digits passing the Verhoeff check) or
   PAN-shaped value, Google Maps link, or precise "lat, lon" pair inside any string
   (a customer's site location identifies their home).

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
    # Payments (Phase 4A)
    "cheque_number", "utr", "payment_reference", "payer_account_number", "reference", "reference_normalized",
    # Site location (Phase 3C) — where the customer lives.
    "site_latitude", "site_longitude", "latitude", "longitude", "site_location_raw_input",
    # Site survey (S1) — the customer's connection, signature images and declared name.
    "consumer_number", "customer_signature_ref", "associate_signature_ref", "declaration_name",
    "customer_declaration_name", "signature",
})
_NO_LOCALS_MODULES = ("app.api.routes.public_associates", "app.associates", "app.onboarding",
                      "app.api.routes.associate_onboarding", "app.api.routes.onboarding_admin",
                      "app.execution", "app.api.routes.associate_execution", "app.api.routes.execution_admin",
                      "app.api.routes.execution_register_admin", "app.payments", "app.jobs.payment_alerts",
                      "app.api.routes.associate_payments", "app.api.routes.payments_admin",
                      "app.api.routes.public_receipts", "app.geo", "app.onboarding.maps_link",
                      "app.onboarding.site_location", "app.api.routes.associate_equipment", "app.survey",
                      "app.api.routes.associate_survey", "app.api.routes.survey_admin")

# 12 digits, optionally grouped 4-4-4 by spaces or hyphens as printed on the card.
_AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
_PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}\d{4}[A-Za-z](?![A-Za-z0-9])")
# Any Google Maps link (short or full), up to the next whitespace or quote.
_MAPS_URL_RE = re.compile(
    r"(?i)(?:https?://)?(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.|maps\.)?google\.[a-z.]{2,6}/maps)[^\s\"'<>]*")
# A precise coordinate pair (3+ decimals each, e.g. "22.5726, 88.3639"); money and
# measurements in this codebase carry at most two decimals.
_LATLON_RE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}(?![\d.])")


def _scrub_text(value: str) -> str:
    from app.associates.validation import verhoeff_valid

    def _aadhaar(m: re.Match) -> str:
        digits = "".join(m.groups())
        return REDACTED if verhoeff_valid(digits) else m.group(0)

    value = _LATLON_RE.sub(REDACTED, _MAPS_URL_RE.sub(REDACTED, value))
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
