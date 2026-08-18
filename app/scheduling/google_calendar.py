"""
Thin Google Calendar wrapper — the only module that talks to Google's APIs.

Everything else (availability engine, booking) depends on the small, mockable
functions here: `get_busy_intervals`, `insert_event`, and the OAuth helpers. Google
client libraries are imported lazily inside function bodies (the repo pattern for
openai/boto3) so the rest of the app — and the test suite — never require them to be
installed or configured. When Google OAuth settings are absent the module is inert and
raises a clear error rather than half-working.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from app.config import settings

# Scopes: read/write events + free/busy availability lookups.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleCalendarNotConfiguredError(RuntimeError):
    """Raised when a Google Calendar operation is attempted without OAuth settings."""


def _require_configured() -> None:
    if not settings.google_calendar_enabled:
        raise GoogleCalendarNotConfiguredError(
            "Google Calendar is not configured — set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET, and GOOGLE_OAUTH_REDIRECT_URI."
        )


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": _TOKEN_URI,
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


# ── OAuth ────────────────────────────────────────────────────────────────────────

def build_authorization_url(state: str) -> str:
    """Return the Google consent URL for the current employee (state ties it back)."""
    _require_configured()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_oauth_redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # force a refresh_token even on re-consent
        state=state,
    )
    return auth_url


def exchange_code(code: str) -> str:
    """Exchange an OAuth authorization code for a long-lived refresh token."""
    _require_configured()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_oauth_redirect_uri
    flow.fetch_token(code=code)
    refresh_token = getattr(flow.credentials, "refresh_token", None)
    if not refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token — the employee may need to revoke "
            "prior access and reconnect with consent."
        )
    return refresh_token


def _credentials(refresh_token: str):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=SCOPES,
    )


def _service(refresh_token: str):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_credentials(refresh_token), cache_discovery=False)


# ── Availability ───────────────────────────────────────────────────────────────

def get_busy_intervals(
    refresh_token: str,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[tuple[datetime, datetime]]:
    """Return this calendar's busy intervals in [time_min, time_max] as UTC datetimes."""
    _require_configured()
    service = _service(refresh_token)
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "items": [{"id": calendar_id}],
    }
    response = service.freebusy().query(body=body).execute()
    busy = response.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return [(_parse_iso(b["start"]), _parse_iso(b["end"])) for b in busy]


# ── Booking ────────────────────────────────────────────────────────────────────

def insert_event(
    refresh_token: str,
    calendar_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    attendee_emails: list[str],
    description: Optional[str] = None,
) -> dict:
    """Create a Calendar event with a Google Meet link and email both parties.

    Returns {"google_event_id", "meet_link"}. `sendUpdates="all"` is what makes
    Google email the invite (with the Meet link) to every attendee.
    """
    _require_configured()
    service = _service(refresh_token)
    event_body = {
        "summary": summary,
        "description": description or "",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "attendees": [{"email": e} for e in attendee_emails if e],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    created = service.events().insert(
        calendarId=calendar_id,
        body=event_body,
        conferenceDataVersion=1,
        sendUpdates="all",
    ).execute()
    return {
        "google_event_id": created.get("id"),
        "meet_link": created.get("hangoutLink"),
    }


def _parse_iso(value: str) -> datetime:
    # Google returns RFC3339 timestamps, e.g. "2026-08-12T10:00:00Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
