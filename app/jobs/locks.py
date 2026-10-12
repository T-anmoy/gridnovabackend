"""One run at a time for each scheduled job (POST /api/v1/dashboard/internal/check-*).

Cloud Scheduler retries a run it thinks failed, and an Admin can press the same button
while the scheduled run is still going, so two runs of one job can overlap. Each job is
idempotent run after run (it marks what it has alerted), but two runs reading the same
unmarked rows at the same moment could both alert them. The job-level lock closes that:
the second run finds the lock taken and returns at once with
{"status": "skipped", "reason": "already_running"}.

PostgreSQL: a session-level advisory lock (pg_try_advisory_lock), taken on a connection
of its own and held for the whole run. Not the job's Session connection: the jobs
commit part-way through, and a pooled Session may continue on a different connection
after a commit, which would leave the lock (or the unlock) on the wrong one.
SQLite (development and tests: one process): a process-local lock does the same.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

SKIPPED = {"status": "skipped", "reason": "already_running"}

_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


def lock_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key for a job name."""
    return int.from_bytes(hashlib.sha256(f"gridnova-job:{name}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def job_lock(db: Session, name: str) -> Iterator[bool]:
    """Yields True when this run holds the job's lock, False when another run does."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        key = lock_key(name)
        with bind.connect() as conn:
            acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
            conn.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                    conn.commit()
        return
    with _local_guard:
        lock = _local_locks.setdefault(name, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def run_exclusive(db: Session, name: str, job: Callable[[], dict]) -> dict:
    """Run `job` under the job's lock, or answer SKIPPED when another run holds it."""
    with job_lock(db, name) as acquired:
        if not acquired:
            return dict(SKIPPED)
        return job()
