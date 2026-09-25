"""Notifications that describe a database change are sent only once that change is
committed.

notify_after_commit(session, send) queues `send` on the session. The SQLAlchemy
after_commit hook runs the queue; when the root transaction ends any other way
(rollback, or the session closing without a commit) the queue is thrown away. So a
request that fails and rolls back never emails anyone about a lead, meeting or
proposal that does not exist.

A queued send runs outside the request's control flow, after the data is safe: it
must never raise into anything, so each one is isolated and failures are logged.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

logger = logging.getLogger(__name__)

_PENDING = "gridnova_pending_notifications"


def notify_after_commit(session: Session, send: Callable[[], None], description: str) -> None:
    session.info.setdefault(_PENDING, []).append((description, send))


def pending_count(session: Session) -> int:
    return len(session.info.get(_PENDING, []))


@event.listens_for(Session, "after_commit")
def _send_pending(session: Session) -> None:
    pending = session.info.pop(_PENDING, [])
    for description, send in pending:
        try:
            send()
        except Exception:
            logger.exception("Notification %r failed after commit", description)


@event.listens_for(Session, "after_transaction_end")
def _discard_uncommitted(session: Session, transaction: SessionTransaction) -> None:
    # after_commit has already emptied the queue on a commit; anything left when the
    # outermost transaction ends was never committed.
    if transaction.parent is None and session.info.get(_PENDING):
        dropped = session.info.pop(_PENDING)
        logger.info("Discarded %d notification(s) for changes that were not committed: %s",
                    len(dropped), ", ".join(d for d, _ in dropped))
