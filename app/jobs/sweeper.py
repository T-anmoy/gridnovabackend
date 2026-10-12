"""Retry background work that never finished (app/jobs/runner.py).

The runner's work lives in the instance's memory: an instance killed mid-render (a
crash, a deploy past the graceful-shutdown window) loses it. Two kinds of record show
it afterwards, and the scheduled jobs pick them up again:

  * an approved / sent / accepted proposal with no PDF       — check-stale-leads
  * a payment receipt still `pending` (never rendered/emailed) — check-payment-alerts

Only records older than SWEEPER_GRACE_MINUTES are retried (younger ones may still be in
flight on another instance), at most SWEEPER_BATCH_SIZE per run so a backlog cannot push
a job past its request timeout. Nothing is retried while storage is not configured —
the retry would fail the same way.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.models import PaymentReceipt, Proposal, ProposalStatus, ReceiptEmailStatus

logger = logging.getLogger(__name__)

PDF_STATUSES = (ProposalStatus.approved, ProposalStatus.sent, ProposalStatus.accepted)


def _cutoff(now: datetime) -> datetime:
    return now - timedelta(minutes=settings.sweeper_grace_minutes)


def retry_missing_proposal_pdfs(db: Session, now: datetime) -> int:
    from app.api.routes.dashboard import _trigger_pdf_generation
    from app.memory.memory_manager import MemoryManager
    from app.storage import r2

    if not r2.is_configured():
        return 0
    rows = list(db.execute(
        select(Proposal).where(Proposal.status.in_(PDF_STATUSES), Proposal.pdf_storage_key.is_(None),
                               Proposal.pdf_url.is_(None), Proposal.approved_at.is_not(None),
                               Proposal.approved_at < _cutoff(now))
        .order_by(Proposal.approved_at).limit(settings.sweeper_batch_size)).scalars())
    memory = MemoryManager(db)
    for proposal in rows:
        logger.info("Sweeper: retrying the PDF of proposal %s", proposal.id)
        _trigger_pdf_generation(proposal.id, proposal, db, memory)
    return len(rows)


def retry_pending_receipts(db: Session, now: datetime) -> int:
    from app.jobs import runner
    from app.payments.receipts import generate_and_deliver
    from app.storage import r2

    if not r2.is_configured():
        return 0
    ids = list(db.execute(
        select(PaymentReceipt.id).where(PaymentReceipt.email_status == ReceiptEmailStatus.pending,
                                        PaymentReceipt.voided_at.is_(None),
                                        PaymentReceipt.created_at < _cutoff(now))
        .order_by(PaymentReceipt.id).limit(settings.sweeper_batch_size)).scalars())
    for receipt_id in ids:
        logger.info("Sweeper: retrying receipt %s", receipt_id)
        runner.submit(lambda rid=receipt_id: generate_and_deliver(rid), f"receipt {receipt_id} (sweeper)")
    return len(ids)
