"""BE-18 — the customer's side of a Proposal Studio proposal (unauthenticated, rate-limited
per IP, noindex). Contract: docs/api/proposal_studio.md.

    GET  /api/v1/public/studio/{token}           the online proposal: summary, state, milestones
    GET  /api/v1/public/studio/{token}/pdf       the issued PDF (redirect to a signed link)
    POST /api/v1/public/studio/{token}/accept    full name + last 4 of the mobile + terms
    POST /api/v1/public/studio/{token}/decline   optional reason
    GET  /api/v1/public/proposals/verify/{code}  what a printed QR opens

404 for every unknown token or code, never saying why. Views and downloads are recorded
with the IP and user agent (the owner is alerted on the first view).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog
from app.studio import public
from app.studio.service import StudioError

router = APIRouter()

RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60
VERIFY_LIMIT = 60
_page_log = RateLimitLog(RATE_WINDOW_SECONDS)
_pdf_log = RateLimitLog(RATE_WINDOW_SECONDS)
_action_log = RateLimitLog(RATE_WINDOW_SECONDS)
_verify_log = RateLimitLog(RATE_WINDOW_SECONDS)
# Never indexed, never cached, never leaking the token in a Referer.
PRIVATE = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _limit(request: Request, log: RateLimitLog, limit: Optional[int] = None) -> None:
    if not log.hit(client_ip(request), RATE_LIMIT if limit is None else limit):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.", headers=PRIVATE)


def _ua(request: Request) -> Optional[str]:
    return (request.headers.get("user-agent") or "")[:400] or None


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Proposal not found.", headers=PRIVATE)


def _refuse(db: Session, exc: StudioError) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=exc.status_code, detail=exc.body(), headers=PRIVATE)


@router.get("/api/v1/public/studio/{token}")
def view(token: str, request: Request, db: Session = Depends(get_db)):
    _limit(request, _page_log)
    v = public.by_token(db, token)
    if v is None:
        raise _not_found()
    public.record_view(db, v, client_ip(request), _ua(request))
    db.commit()
    body = public.summary(db, v)
    body["pdf_path"] = f"/api/v1/public/studio/{token}/pdf"
    return JSONResponse(body, headers=PRIVATE)


@router.get("/api/v1/public/studio/{token}/pdf")
def pdf(token: str, request: Request, db: Session = Depends(get_db)):
    from app.storage import r2
    _limit(request, _pdf_log)
    v = public.by_token(db, token)
    if v is None or not v.pdf_storage_key:
        raise _not_found()
    try:
        url, _ = r2.signed_get_url(v.pdf_storage_key, filename=v.pdf_filename, as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail="The proposal PDF is not available right now.", headers=PRIVATE)
    public.record_download(db, v, client_ip(request), _ua(request))
    db.commit()
    return RedirectResponse(url, status_code=302, headers=PRIVATE)


class AcceptIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    mobile_last4: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    accept_terms: bool


@router.post("/api/v1/public/studio/{token}/accept")
def accept(token: str, body: AcceptIn, request: Request, db: Session = Depends(get_db)):
    """409 proposal_expired / proposal_superseded / proposal_declined; 422 mobile_mismatch /
    terms_not_accepted. Accepting twice is a no-op success."""
    _limit(request, _action_log)
    try:
        out = public.accept(db, token, full_name=body.full_name, mobile_last4=body.mobile_last4,
                            accept_terms=body.accept_terms, ip=client_ip(request), user_agent=_ua(request))
    except StudioError as exc:
        if exc.status_code == 404:
            db.rollback()
            raise _not_found()
        raise _refuse(db, exc)
    db.commit()
    return JSONResponse(out, headers=PRIVATE)


class DeclineIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)


@router.post("/api/v1/public/studio/{token}/decline")
def decline(token: str, body: DeclineIn, request: Request, db: Session = Depends(get_db)):
    _limit(request, _action_log)
    try:
        out = public.decline(db, token, reason=body.reason, ip=client_ip(request), user_agent=_ua(request))
    except StudioError as exc:
        if exc.status_code == 404:
            db.rollback()
            raise _not_found()
        raise _refuse(db, exc)
    db.commit()
    return JSONResponse(out, headers=PRIVATE)


@router.get("/api/v1/public/proposals/verify/{code}")
def verify(code: str, request: Request, response: Response, db: Session = Depends(get_db)):
    """Status: valid | superseded | expired | accepted | declined. The customer's name is
    masked (first name and an initial)."""
    _limit(request, _verify_log, VERIFY_LIMIT)
    out = public.verify(db, code)
    if out is None:
        raise HTTPException(status_code=404, detail="No Grid Nova proposal has this code.", headers=PRIVATE)
    return JSONResponse(out, headers=PRIVATE)
