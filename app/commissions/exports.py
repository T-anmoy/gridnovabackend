"""CSV exports for Accounts — commission entries, TDS, and the payout bank file.

Same conventions as app/payments/exports.py: UTF-8 with a byte-order mark, amounts as
decimal strings, every timestamp in UTC and IST, every cell through the shared
CSV-injection guard (app/security/csv_cells.py).

The PAN (TDS export) and the full bank account number (bank file, Accounts only) are
decrypted only here, through the audited helper app/security/pii.py — one audit row per
value — and each export itself is audited (who, which batch / year, how many rows).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commissions import periods, serialize
from app.commissions.payouts import application_of
from app.memory.models import (
    Associate, AuditLog, CommissionEntry, PayoutBatch, PayoutBatchStatus, PayoutLine,
)
from app.payments.exports import _csv, _times
from app.security import pii


def commissions_csv(db: Session, date_from: Optional[date] = None, date_to: Optional[date] = None) -> str:
    q = select(CommissionEntry)
    if date_from:
        q = q.where(CommissionEntry.created_at >= periods.start_utc(date_from))
    if date_to:
        q = q.where(CommissionEntry.created_at < periods.start_utc(date_to + timedelta(days=1)))
    entries = list(db.execute(q.order_by(CommissionEntry.id)).scalars())
    rows = []
    for row, e in zip(serialize.entries_json(db, entries, staff=True), entries):
        batch = row["batch"] or {}
        rows.append([row["entry_id"], (row["associate"] or {}).get("associate_id"),
                     (row["associate"] or {}).get("full_name"), row["earning_role"], row["kind"], row["status"],
                     row["customer_name"], row["project_reference_id"], row["payment_id"], row["basis_amount"],
                     row["rate_percent"], row["holders"], row["amount"], row["pending_reason"],
                     row["schedule_version"], row["agreement_date"], row["reverses_entry_id"], batch.get("period"),
                     row["reason"], row["created_by"], *_times(e.created_at), row["approved_by"],
                     *_times(e.approved_at), *_times(e.paid_at)])
    return _csv(["entry_id", "associate_id", "associate_name", "earning_role", "kind", "status", "customer_name",
                 "project_reference_id", "payment_id", "basis_inr", "rate_percent", "holders", "amount_inr",
                 "pending_reason", "schedule_version", "agreement_date", "reverses_entry_id", "payout_period",
                 "reason", "created_by", "created_at_utc", "created_at_ist", "approved_by", "approved_at_utc",
                 "approved_at_ist", "paid_at_utc", "paid_at_ist"], rows)


def tds_csv(db: Session, fy: str, actor: str) -> str:
    """Every payout line of the financial year's batches that are not cancelled, with
    the associate's PAN (decrypted, audited) for the TDS return."""
    lines = list(db.execute(select(PayoutLine, PayoutBatch).join(PayoutBatch, PayoutBatch.id == PayoutLine.batch_id)
                            .where(PayoutBatch.financial_year == fy,
                                   PayoutBatch.status != PayoutBatchStatus.cancelled)
                            .order_by(PayoutBatch.period, PayoutLine.associate_id)).all())
    associates = {a.id: a for a in db.execute(select(Associate).where(
        Associate.id.in_({ln.associate_id for ln, _ in lines}))).scalars()} if lines else {}
    pans: dict[int, str] = {}
    rows = []
    for line, batch in lines:
        a = associates[line.associate_id]
        if a.id not in pans:
            application = application_of(db, a)
            pans[a.id] = pii.reveal(db, application, "pan", actor=actor, purpose="tds_export")
        rows.append([fy, batch.period, batch.status.value, a.associate_id, a.full_name, pans[a.id],
                     "yes" if line.pan_on_file else "no", serialize.s(line.gross), serialize.s(line.recoveries),
                     serialize.s(line.carried_in), serialize.s(line.taxable), str(line.tds_rate_percent),
                     serialize.s(line.tds), serialize.s(line.net), serialize.s(line.fy_taxable_before + line.taxable),
                     serialize.s(line.fy_tds_before + line.tds), *_times(line.paid_at)])
    db.add(AuditLog(entity_type="commission_export", entity_id=f"tds-{fy}", action="tds_exported",
                    performed_by=actor, after_value={"financial_year": fy, "rows": len(rows),
                                                     "associates": len(pans)}))
    return _csv(["financial_year", "period", "batch_status", "associate_id", "associate_name", "pan", "pan_on_file",
                 "gross_inr", "recoveries_inr", "carried_in_inr", "taxable_inr", "tds_rate_percent", "tds_inr",
                 "net_inr", "fy_taxable_to_date_inr", "fy_tds_to_date_inr", "paid_at_utc", "paid_at_ist"], rows)


def payout_bank_csv(db: Session, batch: PayoutBatch, actor: str) -> str:
    """The bank upload file for one approved/paid batch: one row per transfer (lines
    that net to nothing are left out). The only place a full account number appears."""
    lines = list(db.execute(select(PayoutLine).where(PayoutLine.batch_id == batch.id, PayoutLine.net > 0)
                            .order_by(PayoutLine.associate_id)).scalars())
    associates = {a.id: a for a in db.execute(select(Associate).where(
        Associate.id.in_([ln.associate_id for ln in lines]))).scalars()} if lines else {}
    rows = []
    for line in lines:
        a = associates[line.associate_id]
        account = pii.reveal(db, application_of(db, a), "bank_account", actor=actor, purpose="payout_bank_export")
        rows.append([a.associate_id, line.account_holder_name, account, line.bank_ifsc, serialize.s(line.net),
                     f"Grid Nova commission {batch.period} {a.associate_id}", line.bank_reference])
    db.add(AuditLog(entity_type="payout_batch", entity_id=str(batch.id), action="bank_file_exported",
                    performed_by=actor, after_value={"period": batch.period, "status": batch.status.value,
                                                     "rows": len(rows)}))
    return _csv(["associate_id", "account_holder_name", "account_number", "ifsc", "amount_inr", "narration",
                 "bank_reference"], rows)
