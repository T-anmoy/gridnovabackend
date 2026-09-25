"""Reject an oversized site-assessment upload while it is arriving, not after.

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
# Multipart framing (boundaries, part headers, the small form fields) on top of the
# file itself. Generous: this bound exists to stop a flood, not to measure the file.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _limit() -> int:
    from app.api.routes import proposal_inputs  # read at call time: tests shrink it
    return proposal_inputs.MAX_UPLOAD_SIZE_BYTES


def too_large_detail() -> str:
    return f"File exceeds the {_limit() // (1024 * 1024)}MB limit."


class _BodyTooLarge(Exception):
    pass


async def _send_413(send) -> None:
    body = json.dumps({"detail": too_large_detail()}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class UploadSizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or not _UPLOAD_PATH.match(scope.get("path", "")):
            return await self.app(scope, receive, send)

        allowed = _limit() + MULTIPART_OVERHEAD_BYTES
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > allowed:
                    return await _send_413(send)
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
                await _send_413(send)
