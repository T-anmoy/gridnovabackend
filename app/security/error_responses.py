"""The generic 500, sent from inside CORSMiddleware.

Starlette puts ServerErrorMiddleware outside every user middleware, so a 500 built by
`@app.exception_handler(Exception)` leaves the app without passing CORSMiddleware: no
Access-Control-Allow-Origin, and the browser reports "network request failed" instead
of the server error. This middleware sits just inside CORSMiddleware, answers an
unhandled exception itself, and CORSMiddleware then decorates that response like any
other (allowed origins only, Vary: Origin, same credentials rule) — no CORS logic is
duplicated here.

The exception is re-raised after the response is sent, exactly as ServerErrorMiddleware
does, so Sentry and the server's own logging still see it.
"""
from __future__ import annotations

import json
import logging
import threading

from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import settings

logger = logging.getLogger(__name__)

GENERIC_500 = {"detail": "An internal error occurred."}
_SCHEMA_BEHIND_MESSAGE = "database schema is behind this code (run python scripts/dev_setup.py)"
# SQLite / PostgreSQL wording for a column or table the code expects and the database lacks.
_MISSING_MARKERS = ("no such column", "no such table", "undefinedcolumn", "undefinedtable")
_schema_log_lock = threading.Lock()
_schema_logged = False


def _is_missing_schema_object(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (OperationalError, ProgrammingError)):
            orig = getattr(exc, "orig", None)
            text = f"{type(orig).__name__} {orig} {exc}".lower()
            if any(marker in text for marker in _MISSING_MARKERS) or (
                    "does not exist" in text and ("column" in text or "relation" in text)):
                return True
        exc = exc.__cause__ or exc.__context__
    return False


def log_unhandled(exc: BaseException) -> None:
    """The ERROR log for an unhandled exception; development and test also get one
    plain line, once per process, when the cause is a database behind the code."""
    global _schema_logged
    logger.error("Unhandled exception: %s", exc, exc_info=(type(exc), exc, exc.__traceback__))
    if settings.environment in ("development", "test") and _is_missing_schema_object(exc):
        with _schema_log_lock:
            first, _schema_logged = not _schema_logged, True
        if first:
            logger.error(_SCHEMA_BEHIND_MESSAGE)


def reset_schema_log() -> None:
    global _schema_logged
    with _schema_log_lock:
        _schema_logged = False


class UnhandledErrorMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            if started:
                raise
            log_unhandled(exc)
            body = json.dumps(GENERIC_500, separators=(",", ":")).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            raise
