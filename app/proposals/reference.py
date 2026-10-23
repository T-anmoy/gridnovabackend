"""The proposal reference printed on the customer document (BE-13).

    GN/23/10/2026/01       the first proposal issued on 23 Oct 2026 (IST), per-day sequence
    GN/23/10/2026/01-R1    a later approved version of the same lead's proposal

Grid Nova's samples print GN/DD/MM/YYYY, which repeats when two proposals go out on one
day; the per-day sequence keeps the look and makes it unique. It is allocated when a
version is approved — the moment the customer document is produced — inside the
approval's own transaction, from proposal_reference_sequences with one atomic
INSERT … ON CONFLICT … RETURNING (the receipt-number pattern), so two approvals can
never draw the same number and a rolled-back approval leaves no gap. The unique index
uq_proposals_document_reference is the last wall. Once set, a reference never changes.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.memory.models import Proposal

PREFIX = "GN"


def format_reference(day: date, seq: int) -> str:
    return f"{PREFIX}/{day:%d/%m/%Y}/{seq:02d}"


def next_sequence(db: Session, day: date) -> int:
    return int(db.execute(text(
        "INSERT INTO proposal_reference_sequences (day, value) VALUES (:day, 1) "
        "ON CONFLICT(day) DO UPDATE SET value = proposal_reference_sequences.value + 1 RETURNING value"),
        {"day": day.isoformat()}).scalar_one())


def today_ist(now: Optional[datetime] = None) -> date:
    from app.timezones import _as_ist
    return _as_ist(now or datetime.now(timezone.utc)).date()


def assign(db: Session, proposal: Proposal, now: Optional[datetime] = None) -> str:
    """The proposal's reference, allocating it (and the document date) on first call.
    A version after one already issued on the lead takes that base reference + -R{n}."""
    if proposal.document_reference:
        return proposal.document_reference
    day = today_ist(now)
    earlier = list(db.execute(
        select(Proposal.document_reference).where(
            Proposal.lead_id == proposal.lead_id, Proposal.version < proposal.version,
            Proposal.document_reference.is_not(None))
        .order_by(Proposal.version)).scalars())
    if earlier:
        reference = f"{earlier[0].split('-R')[0]}-R{len(earlier)}"
    else:
        reference = format_reference(day, next_sequence(db, day))
    proposal.document_reference = reference
    proposal.document_date = day
    db.flush()
    return reference
