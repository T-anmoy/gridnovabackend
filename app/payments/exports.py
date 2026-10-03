"""CSV exports for Accounts — payments, receipts, ledger entries.

UTF-8 with a byte-order mark (so Excel reads Hindi/Bengali names correctly), amounts as
decimal strings, every timestamp twice (UTC ISO 8601 and IST). CSV-injection safe: a
text cell starting with = + - @ (or a tab/carriage return) is prefixed with an
apostrophe so a spreadsheet never evaluates it; the exported numbers themselves (which
may legitimately start with "-", e.g. a ledger reversal) are left as numbers.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import (
    Customer, Payment, PaymentLedgerEntry, PaymentReceipt, Project,
)

BOM = "﻿"
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def safe(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_DANGEROUS) and not _NUMBER.match(text):
        return "'" + text
    return text


def _times(dt: Optional[datetime]) -> tuple[str, str]:
    from app.timezones import _as_ist
    if dt is None:
        return "", ""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(), _as_ist(dt).strftime("%Y-%m-%d %H:%M:%S IST")


def _csv(header: list[str], rows: Iterable[list]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe(v) for v in row])
    return BOM + out.getvalue()


def _context(db: Session, project_id: int, cache: dict) -> tuple[str, str]:
    if project_id not in cache:
        project = db.get(Project, project_id)
        customer = db.get(Customer, project.customer_id) if project else None
        cache[project_id] = (customer.name if customer else "", project.reference_id if project else "")
    return cache[project_id]


def _in_range(q, column, date_from, date_to):
    from datetime import time
    if date_from:
        q = q.where(column >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        q = q.where(column <= datetime.combine(date_to, time.max, tzinfo=timezone.utc))
    return q


def payments_csv(db: Session, date_from=None, date_to=None) -> str:
    from app.payments.service import s
    cache: dict = {}
    q = _in_range(select(Payment), Payment.recorded_at, date_from, date_to).order_by(Payment.id)
    rows = []
    for p in db.execute(q).scalars():
        name, ref = _context(db, p.project_id, cache)
        rows.append([p.id, ref, name, s(p.amount), p.mode.value, p.reference, p.bank_name,
                     p.instrument_date.isoformat() if p.instrument_date else "", p.payer_name, p.status.value,
                     p.recorded_by, *_times(p.recorded_at), p.verified_by, *_times(p.verified_at),
                     p.dishonour_reason, p.cancel_reason])
    return _csv(["payment_id", "project_reference_id", "customer_name", "amount_inr", "mode", "reference",
                 "bank_name", "instrument_date", "payer_name", "status", "recorded_by", "recorded_at_utc",
                 "recorded_at_ist", "verified_by", "verified_at_utc", "verified_at_ist", "dishonour_reason",
                 "cancel_reason"], rows)


def receipts_csv(db: Session, date_from=None, date_to=None) -> str:
    from app.payments.service import s
    cache: dict = {}
    q = _in_range(select(PaymentReceipt), PaymentReceipt.created_at, date_from, date_to).order_by(PaymentReceipt.id)
    rows = []
    for r in db.execute(q).scalars():
        p = db.get(Payment, r.payment_id)
        name, ref = _context(db, p.project_id, cache)
        rows.append([r.receipt_number, p.id, ref, name, s(p.amount), p.mode.value, *_times(r.created_at),
                     r.email_status.value, r.email_skipped_reason, r.held_reason, *_times(r.emailed_at)])
    return _csv(["receipt_number", "payment_id", "project_reference_id", "customer_name", "amount_inr", "mode",
                 "issued_at_utc", "issued_at_ist", "email_status", "email_skipped_reason", "held_reason",
                 "emailed_at_utc", "emailed_at_ist"], rows)


def ledger_csv(db: Session, date_from=None, date_to=None) -> str:
    from app.payments.service import s
    cache: dict = {}
    q = _in_range(select(PaymentLedgerEntry), PaymentLedgerEntry.created_at, date_from, date_to) \
        .order_by(PaymentLedgerEntry.id)
    rows = []
    for e in db.execute(q).scalars():
        p = db.get(Payment, e.payment_id)
        name, ref = _context(db, p.project_id, cache)
        rows.append([e.id, e.kind.value, s(e.amount), e.payment_id, e.reverses_entry_id, ref, name, e.actor,
                     *_times(e.created_at)])
    return _csv(["entry_id", "kind", "amount_inr", "payment_id", "reverses_entry_id", "project_reference_id",
                 "customer_name", "actor", "created_at_utc", "created_at_ist"], rows)
