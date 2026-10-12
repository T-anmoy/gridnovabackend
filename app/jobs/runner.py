"""Work that must not hold up the response: emails and Slack posts after a commit,
receipt PDFs, proposal PDFs, associate ID cards and Clerk invitations, memory reflections.

    from app.jobs import runner
    runner.submit(fn, "what it is")

One bounded ThreadPoolExecutor per process (BACKGROUND_WORKERS threads). `fn` runs after
the response has been sent; it must open its own database session (the request's is
closed by then) and never raises into anything — a failure is logged with its description.

On Cloud Run this needs CPU outside requests: the service is deployed with
--no-cpu-throttling and --min-instances 1 (deploy/RUNBOOK.md, "Background work"), otherwise
the instance is throttled the moment the response is sent and this work stalls. On
SIGTERM the lifespan drains the pool for up to the graceful-shutdown window; anything
lost to a crash is picked up again by the sweepers in the scheduled jobs (proposals
without a PDF: check-stale-leads; receipts still pending: check-payment-alerts). Cloud
Tasks is the upgrade when this needs retries and durability of its own.

Tests run every submission inline (tests/conftest.py sets INLINE), so what a request
started has finished when the test client returns.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

INLINE = False

_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=settings.background_workers, thread_name_prefix="background")
        return _executor


def _run(fn: Callable[[], None], description: str) -> None:
    try:
        fn()
    except Exception:
        logger.exception("Background work %r failed", description)


def submit(fn: Callable[[], None], description: str) -> None:
    """Run `fn` off the request path (inline when INLINE is set)."""
    if INLINE:
        _run(fn, description)
        return
    _pool().submit(_run, fn, description)


def shutdown(wait: bool = True) -> None:
    """Stop taking work and (by default) wait for what is queued — the lifespan's last step."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=not wait)
