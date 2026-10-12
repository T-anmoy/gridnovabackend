"""A customer's payment receipt, by the random token in their receipt email — same
pattern as the proposal PDF: rate-limited per IP, 302 to a short-lived signed URL, 404
for any bad or not-yet-rendered token, and while the receipt is held (placeholder
content) or void (the payment was dishonoured)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.models import PaymentReceipt, ReceiptEmailStatus
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog

router = APIRouter()
# Same limit as the proposal PDF link (app/api/routes/public_proposals.py).
_RATE_LIMIT, _RATE_WINDOW_SECONDS = 30, 60
_receipt_request_log = RateLimitLog(_RATE_WINDOW_SECONDS)


@router.get("/api/v1/public/receipts/{token}/pdf")
def download_receipt(token: str, request: Request, db: Session = Depends(get_db)):
    from app.storage import r2

    if not _receipt_request_log.hit(client_ip(request), _RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    receipt = db.execute(select(PaymentReceipt).where(PaymentReceipt.public_token == token)).scalar_one_or_none() \
        if 16 <= len(token) <= 64 else None
    if (receipt is None or not receipt.pdf_key or receipt.voided_at is not None
            or receipt.email_status == ReceiptEmailStatus.held):
        raise HTTPException(status_code=404, detail="Receipt not found")
    try:
        url, _ = r2.signed_get_url(receipt.pdf_key, filename=f"{receipt.receipt_number}.pdf", as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail="Receipts are temporarily unavailable.")
    return RedirectResponse(url, status_code=302)
