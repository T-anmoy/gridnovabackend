"""Reject an oversized upload while it is arriving, not after — site-assessment files
and the associate registration forms (four documents each).

FastAPI parses a whole multipart body before the endpoint runs, so a size check in
the endpoint only happens once every byte has been received and spooled. This
middleware stops the request earlier: 413 straight away when Content-Length is
already too large, and otherwise as soon as the bytes actually received pass the
limit. The endpoint still checks the file itself in chunks for the exact per-file
limit (app/api/routes/proposal_inputs.py) — this is the outer bound on the body.
"""
from __future__ import annotations

import json
import re

_UPLOAD_PATH = re.compile(r"^/api/v1/proposal-inputs/\d+/upload/?$")
_REGISTRATION_PATH = re.compile(r"^/api/v1/public/(associates|preferred)/apply/?$")
_ONBOARDING_FILE_PATH = re.compile(
    r"^/api/v1/(associate/onboardings/\d+/(files|agreement/signed)|dashboard/onboardings/\d+/agreement/signed)/?$")
# Multipart framing (boundaries, part headers, the small form fields) on top of the
# file itself. Generous: this bound exists to stop a flood, not to measure the file.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _limit() -> int:
    from app.api.routes import proposal_inputs  # read at call time: tests shrink it
    return proposal_inputs.MAX_UPLOAD_SIZE_BYTES


def _registration_limit() -> int:
    from app.associates import uploads  # read at call time: tests shrink it
    from app.memory.models import APPLICATION_DOCUMENTS
    return uploads.MAX_DOCUMENT_BYTES * len(APPLICATION_DOCUMENTS)


def too_large_detail() -> str:
    return f"File exceeds the {_limit() // (1024 * 1024)}MB limit."


def _registration_too_large_detail() -> str:
    from app.associates import uploads
    return f"Upload exceeds the limit ({uploads.MAX_DOCUMENT_BYTES // (1024 * 1024)}MB per document)."


class _BodyTooLarge(Exception):
    pass


async def _send_413(send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class UploadSizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        if _UPLOAD_PATH.match(path):
            allowed, detail = _limit() + MULTIPART_OVERHEAD_BYTES, too_large_detail()
        elif _REGISTRATION_PATH.match(path):
            allowed, detail = _registration_limit() + MULTIPART_OVERHEAD_BYTES, _registration_too_large_detail()
        elif _ONBOARDING_FILE_PATH.match(path):
            from app.api.routes import associate_onboarding  # read at call time: tests shrink it
            limit = associate_onboarding.MAX_FILE_BYTES
            allowed, detail = limit + MULTIPART_OVERHEAD_BYTES, f"File exceeds the {limit // (1024 * 1024)}MB limit."
        else:
            return await self.app(scope, receive, send)

        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > allowed:
                    return await _send_413(send, detail)
            except ValueError:
                pass

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > allowed:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_413(send, detail)
