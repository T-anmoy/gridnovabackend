"""A customer's payment receipt, by the random token in their receipt email — same
pattern as the proposal PDF: rate-limited per IP, 302 to a short-lived signed URL, 404
for any bad or not-yet-rendered token."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.public_proposals import _check_rate_limit
from app.memory.db import get_db
from app.memory.models import PaymentReceipt

router = APIRouter()
_receipt_request_log: defaultdict[str, list[datetime]] = defaultdict(list)


@router.get("/api/v1/public/receipts/{token}/pdf")
def download_receipt(token: str, request: Request, db: Session = Depends(get_db)):
    from app.storage import r2

    _check_rate_limit(request, _receipt_request_log)
    receipt = db.execute(select(PaymentReceipt).where(PaymentReceipt.public_token == token)).scalar_one_or_none() \
        if 16 <= len(token) <= 64 else None
    if receipt is None or not receipt.pdf_key:
        raise HTTPException(status_code=404, detail="Receipt not found")
    try:
        url, _ = r2.signed_get_url(receipt.pdf_key, filename=f"{receipt.receipt_number}.pdf", as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail="Receipts are temporarily unavailable.")
    return RedirectResponse(url, status_code=302)
