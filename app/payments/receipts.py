"""Payment receipts — issued only for a VERIFIED payment.

    verify (one transaction): receipt number allocated atomically + payment_receipts row
    after commit:             render the PDF (WeasyPrint) → private storage → email the
                              customer a token link (GET /api/v1/public/receipts/{token}/pdf)

The number follows payments.receipt.numbering (placeholder P4) with a per-year counter
(receipt_sequences, INSERT … ON CONFLICT … RETURNING — the same atomic pattern as
associate IDs); it is allocated in the verification's own transaction, so a rolled-back
verification leaves no gap.

Never sent with placeholder content: a receipt whose company block or wording still
contains DUMMY text is HELD (held_reason = placeholder_content) in every environment,
and in production also while registry entries P5/P6 are unresolved. Verification still
succeeds; held receipts are visible, filterable, and released in bulk once resolved.
No customer email → email_skipped_reason = no_customer_email. Every stall emails
Accounts (receipt_blocked) after the commit.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.execution import config
from app.memory.models import (
    Customer, CustomerOnboarding, LedgerEntryKind, Payment, PaymentLedgerEntry, PaymentMilestone, PaymentReceipt,
    Project, ReceiptEmailStatus,
)

logger = logging.getLogger(__name__)
HOLD_REGISTRY_ENTRIES = ("P5", "P6")
RESEND_MIN_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Numbers and words ────────────────────────────────────────────────────────────

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = [f"{_ONES[hundreds]} Hundred"] if hundreds else []
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def indian_words(n: int) -> str:
    """Whole number in words with Indian grouping: crore, lakh, thousand, hundred."""
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(f"{indian_words(crore)} Crore")
    if lakh:
        parts.append(f"{_below_hundred(lakh)} Lakh")
    if thousand:
        parts.append(f"{_below_hundred(thousand)} Thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    words = f"Rupees {indian_words(rupees)}"
    if paise:
        words += f" and {_below_hundred(paise)} Paise"
    return words + " Only"


def indian_figures(amount: Decimal) -> str:
    """Rs 12,34,567.89 — the price list's currency label for documents, Indian digit
    grouping (app/pricing/price_list.document_amount)."""
    from app.pricing.price_list import document_amount
    return document_amount(Decimal(amount))


def format_number(template: str, year: int, seq: int) -> str:
    def _seq(m: re.Match) -> str:
        return f"{seq:0{int(m.group(1))}d}" if m.group(1) else str(seq)
    return re.sub(r"\{SEQ(?::0?(\d+))?\}", _seq, template.replace("{YYYY}", f"{year:04d}"))


def next_number(db: Session, year: int) -> str:
    seq = db.execute(text(
        "INSERT INTO receipt_sequences (year, value) VALUES (:year, 1) "
        "ON CONFLICT(year) DO UPDATE SET value = receipt_sequences.value + 1 RETURNING value"),
        {"year": year}).scalar_one()
    return format_number(config.payments()["receipt"]["numbering"], year, int(seq))


# ── Issue ────────────────────────────────────────────────────────────────────────

def for_payment(db: Session, payment_id: int) -> Optional[PaymentReceipt]:
    return db.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id == payment_id)).scalar_one_or_none()


def on_verified(db: Session, payment: Payment, onboarding: CustomerOnboarding, actor: str) -> PaymentReceipt:
    """Inside the verification transaction: number the receipt. Rendering and email
    run after the commit."""
    from app.memory.memory_manager import MemoryManager
    from app.timezones import _as_ist

    existing = for_payment(db, payment.id)
    if existing is not None:
        return existing
    year = _as_ist(payment.verified_at or _now()).year
    receipt = PaymentReceipt(payment_id=payment.id, receipt_number=next_number(db, year),
                             public_token=secrets.token_urlsafe(32), email_status=ReceiptEmailStatus.pending,
                             created_at=_now())
    db.add(receipt)
    db.flush()
    from app.memory.models import AuditLog
    db.add(AuditLog(entity_type="payment_receipt", entity_id=str(receipt.id), action="numbered", performed_by=actor,
                    after_value={"payment_id": payment.id, "receipt_number": receipt.receipt_number}))
    receipt_id = receipt.id
    MemoryManager(db).notify_after_commit(lambda: generate_and_deliver(receipt_id), "payment_receipt")
    return receipt


def _content_has_dummy() -> bool:
    from app.placeholders.registry import DUMMY_TEXT_PREFIX
    receipt_cfg = config.payments()["receipt"]
    blob = " ".join(str(v) for v in [*receipt_cfg["company"].values(), *receipt_cfg["wording"].values()])
    return DUMMY_TEXT_PREFIX in blob or "DUMMY" in blob


def hold_reason(db: Session) -> Optional[str]:
    """Why a receipt must not go to the customer yet, or None."""
    from app.config import settings
    from app.placeholders import registry

    if _content_has_dummy():
        return "placeholder_content"
    if settings.is_production and registry.unresolved_ids(list(HOLD_REGISTRY_ENTRIES), db):
        return "placeholder_content"
    return None


def public_url(receipt: PaymentReceipt) -> Optional[str]:
    from app.config import settings
    base = (settings.public_api_base_url or "").rstrip("/")
    return f"{base}/api/v1/public/receipts/{receipt.public_token}/pdf" if base else None


def _ledger_position(db: Session, payment: Payment) -> tuple[Decimal, Optional[Decimal]]:
    """Total paid up to and including this payment's credit, and the balance then —
    fixed by ledger order, so a re-render later shows the same figures."""
    from app.payments.service import accepted_total, money

    credit = db.execute(select(PaymentLedgerEntry).where(PaymentLedgerEntry.payment_id == payment.id,
                                                         PaymentLedgerEntry.kind == LedgerEntryKind.credit)
                        ).scalar_one()
    upto = db.execute(select(PaymentLedgerEntry.amount).where(
        PaymentLedgerEntry.project_id == payment.project_id, PaymentLedgerEntry.id <= credit.id)).scalars().all()
    total_paid = money(sum(upto, Decimal(0)))
    onboarding = db.get(CustomerOnboarding, payment.onboarding_id) if payment.onboarding_id is not None else None
    total = accepted_total(db, onboarding) if onboarding is not None else None
    return total_paid, (total - total_paid if total is not None else None)


def receipt_html(db: Session, receipt: PaymentReceipt) -> str:
    from app.timezones import format_ist_date

    cfg = config.payments()["receipt"]
    company, wording = cfg["company"], cfg["wording"]
    payment = db.get(Payment, receipt.payment_id)
    project = db.get(Project, payment.project_id)
    customer = db.get(Customer, project.customer_id)
    milestone = db.get(PaymentMilestone, payment.milestone_id) if payment.milestone_id else None
    total_paid, balance = _ledger_position(db, payment)
    e = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    rows = [
        ("Receipt number", receipt.receipt_number),
        ("Date received", format_ist_date(payment.verified_at)),
        ("Received from", payment.payer_name or (customer.name if customer else "")),
        ("Customer", customer.name if customer else ""),
        ("Project", project.reference_id if project else ""),
        ("Towards", milestone.label if milestone else "Project payment"),
        ("Amount", indian_figures(payment.amount)),
        ("Amount in words", amount_in_words(payment.amount)),
        ("Mode", payment.mode.value.replace("_", " ").upper()),
        ("Reference", payment.reference),
    ]
    if payment.bank_name:
        rows.append(("Bank", payment.bank_name))
    if payment.instrument_date:
        rows.append(("Instrument date", payment.instrument_date.strftime("%d %B %Y")))
    rows.append(("Total received to date", indian_figures(total_paid)))
    if balance is not None:
        rows.append(("Balance", indian_figures(balance)))
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: sans-serif; font-size: 11pt; margin: 20mm; }}
h1 {{ font-size: 16pt; margin: 0 0 4mm; }} th {{ text-align: left; padding: 2mm 6mm 2mm 0; vertical-align: top; }}
.company {{ margin-bottom: 8mm; }} .footer {{ margin-top: 10mm; font-size: 9pt; color: #444; }}
</style></head><body>
<div class="company"><strong>{e(company["legal_name"])}</strong><br>{e(company["address"])}<br>
{e(company["phone"])} · {e(company["email"])}<br>GSTIN: {e(company["gstin"])}</div>
<h1>{e(wording["heading"])}</h1>
<table>{body}</table>
<div class="footer">{e(wording["footer"])}</div>
</body></html>"""


def generate_and_deliver(receipt_id: int) -> None:
    """After the verification commits, in its own session: render, store, then email
    (or hold / skip). Never raises."""
    from app.memory.db import SessionLocal

    db = SessionLocal()
    try:
        receipt = db.get(PaymentReceipt, receipt_id)
        if receipt is None:
            return
        if receipt.pdf_key is None and not _render(db, receipt):
            db.commit()
            _blocked(db, receipt, "render_failed")
            return
        deliver(db, receipt)
        from app.notifications import associate_emails
        payment = db.get(Payment, receipt.payment_id)
        held = receipt.email_status == ReceiptEmailStatus.held
        associate_emails.payment_verified(db, payment, None if held else public_url(receipt))
        db.commit()
    except Exception:
        logger.exception("Receipt %s: generate/deliver failed", receipt_id)
        db.rollback()
    finally:
        db.close()


def _render(db: Session, receipt: PaymentReceipt) -> bool:
    from app.associates import documents
    from app.storage import r2

    try:
        pdf = documents.render_pdf(receipt_html(db, receipt))
        key = f"receipts/{receipt.receipt_number}.pdf"
        r2.upload_bytes(key, pdf, "application/pdf")
    except Exception as exc:
        receipt.render_error = f"{type(exc).__name__}: {exc}"[:500]
        receipt.email_status = ReceiptEmailStatus.failed
        return False
    receipt.pdf_key, receipt.generated_at, receipt.render_error = key, _now(), None
    db.flush()
    return True


def deliver(db: Session, receipt: PaymentReceipt) -> None:
    """Email the customer the receipt link — unless held or nobody to send to.
    Commits; emails Accounts about a stall after the commit."""
    from app.memory.memory_manager import is_placeholder_email
    from app.notifications import notifier as notifier_module

    payment = db.get(Payment, receipt.payment_id)
    customer = db.get(Customer, db.get(Project, payment.project_id).customer_id)
    reason = hold_reason(db)
    if reason:
        receipt.email_status, receipt.held_reason = ReceiptEmailStatus.held, reason
        db.commit()
        _blocked(db, receipt, "held")
        return
    email = customer.email if customer else None
    if not email or is_placeholder_email(email):
        receipt.email_status, receipt.email_skipped_reason = ReceiptEmailStatus.skipped, "no_customer_email"
        db.commit()
        _blocked(db, receipt, "no_customer_email")
        return
    url = public_url(receipt)
    if url is None:
        receipt.email_status, receipt.email_skipped_reason = ReceiptEmailStatus.failed, "no_public_api_base_url"
        db.commit()
        _blocked(db, receipt, "no_public_api_base_url")
        return
    sent, why = notifier_module.notifier.send_receipt_to_customer(
        customer_email=email, customer_name=customer.name, receipt_number=receipt.receipt_number, receipt_url=url)
    if sent:
        receipt.email_status, receipt.emailed_at = ReceiptEmailStatus.sent, _now()
        receipt.held_reason = receipt.email_skipped_reason = None
    else:
        receipt.email_status, receipt.email_skipped_reason = ReceiptEmailStatus.failed, why
    db.commit()
    if not sent:
        _blocked(db, receipt, why or "send_failed")


def _blocked(db: Session, receipt: PaymentReceipt, reason: str) -> None:
    from app.notifications import notifier as notifier_module
    payment = db.get(Payment, receipt.payment_id)
    try:
        notifier_module.notifier.notify("receipt_blocked", {
            "receipt_number": receipt.receipt_number, "payment_id": payment.id,
            "onboarding_id": payment.onboarding_id, "reason": reason})
    except Exception:
        logger.exception("receipt_blocked notification failed")


def release_held(db: Session, actor: str) -> dict:
    """Accounts' one bulk action once the placeholders are resolved."""
    from app.memory.models import AuditLog

    if hold_reason(db):
        return {"released": 0, "still_held": _count_held(db), "reason": "placeholder_content"}
    held = list(db.execute(select(PaymentReceipt).where(
        PaymentReceipt.email_status == ReceiptEmailStatus.held)).scalars())
    for receipt in held:
        receipt.email_status, receipt.held_reason = ReceiptEmailStatus.pending, None
        db.add(AuditLog(entity_type="payment_receipt", entity_id=str(receipt.id), action="released",
                        performed_by=actor))
    db.commit()
    for receipt in held:
        deliver(db, receipt)
    return {"released": len(held), "still_held": _count_held(db), "reason": None}


def _count_held(db: Session) -> int:
    return len(db.execute(select(PaymentReceipt.id).where(
        PaymentReceipt.email_status == ReceiptEmailStatus.held)).all())


def resend(db: Session, payment_id: int, actor: str) -> PaymentReceipt:
    from app.memory.models import AuditLog

    receipt = for_payment(db, payment_id)
    if receipt is None:
        raise AssociateError("This payment has no receipt (it is not verified).", code="no_receipt", status_code=409)
    if receipt.emailed_at and _now() - (receipt.emailed_at if receipt.emailed_at.tzinfo
                                        else receipt.emailed_at.replace(tzinfo=timezone.utc)) \
            < timedelta(minutes=RESEND_MIN_MINUTES):
        raise AssociateError(f"The receipt was emailed in the last {RESEND_MIN_MINUTES} minutes.",
                             code="resend_too_soon", status_code=429)
    db.add(AuditLog(entity_type="payment_receipt", entity_id=str(receipt.id), action="resend", performed_by=actor))
    receipt.email_status = ReceiptEmailStatus.pending
    db.commit()
    if receipt.pdf_key is None and not _render(db, receipt):
        db.commit()
        _blocked(db, receipt, "render_failed")
        return receipt
    deliver(db, receipt)
    return receipt


# ── Read ─────────────────────────────────────────────────────────────────────────

def attention(receipt: PaymentReceipt) -> list[str]:
    return {ReceiptEmailStatus.held: ["receipt_held"], ReceiptEmailStatus.skipped: ["receipt_pending_email"],
            ReceiptEmailStatus.failed: ["receipt_failed"]}.get(receipt.email_status, [])


def receipt_json(receipt: PaymentReceipt, *, staff: bool) -> dict:
    body = {"receipt_number": receipt.receipt_number, "email_status": receipt.email_status.value,
            "emailed_at": receipt.emailed_at.isoformat() if receipt.emailed_at else None,
            "pdf_available": receipt.pdf_key is not None,
            "receipt_pending_email": receipt.email_status == ReceiptEmailStatus.skipped,
            "held": receipt.email_status == ReceiptEmailStatus.held,
            "url": public_url(receipt) if receipt.pdf_key else None}
    if staff:
        body.update(email_skipped_reason=receipt.email_skipped_reason, held_reason=receipt.held_reason,
                    render_error=receipt.render_error)
    return body
