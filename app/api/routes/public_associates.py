"""Public, unauthenticated associate endpoints: the two registration forms, the ID-card
verification lookup, and the reference data the forms need (territories, the KYC
consent notice, seat availability).

Same public-but-guarded pattern as app/api/routes/public_intake.py: no auth, a
hand-rolled per-IP rate limit on the shared client-IP helper, and responses that
carry nothing sensitive. Registration bodies are parsed by hand rather than by
FastAPI's validation so that a rejected value is never echoed back in the error —
FastAPI's default 422 body includes each offending input, and one of these inputs
is an Aadhaar number, which must not travel anywhere it could be logged.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.associates import service, texts, uploads, validation
from app.config import settings
from app.memory.db import get_db
from app.notifications import notifier as notifier_module
from app.notifications.deferred import notify_after_commit
from app.memory.models import (
    APPLICATION_DOCUMENTS,
    ApplicationStatus,
    AssociateApplication,
    AssociateCapability,
    AssociateStatus,
    AssociateType,
    PreferredAssociateApplication,
    TerritoryBlock,
    TerritoryDistrict,
    TerritoryLevel,
    TerritoryState,
)
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog

router = APIRouter(prefix="/api/v1/public")

# Per-IP limits, one bucket per purpose. A registration is a deliberate one-off
# action with four document uploads, so its limit is the tightest. Verification is one
# QR scan per card; Associate IDs are sequential and printed on the cards, so its limit
# is kept low to slow down walking through them (BE-1 S10).
_APPLY_LIMIT, _VERIFY_LIMIT, _LOOKUP_LIMIT = 5, 10, 60
_WINDOW_SECONDS = 60
_apply_request_log = RateLimitLog(_WINDOW_SECONDS)
_verify_request_log = RateLimitLog(_WINDOW_SECONDS)
_lookup_request_log = RateLimitLog(_WINDOW_SECONDS)


def _check_rate_limit(request: Request, log: RateLimitLog, limit: int) -> None:
    if not log.hit(client_ip(request), limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before trying again.")


def _lookup_limit(request: Request) -> None:
    _check_rate_limit(request, _lookup_request_log, _LOOKUP_LIMIT)


# ── Form parsing ─────────────────────────────────────────────────────────────────

class _Form:
    """Collects every field error before answering, so the applicant sees them all at
    once. Errors carry the field name and a message — never the submitted value."""

    def __init__(self, data):
        self.data = data
        self.errors: list[dict] = []

    def error(self, field: str, message: str, kind: str = "value_error") -> None:
        self.errors.append({"loc": ["body", field], "msg": message, "type": kind})

    def raw(self, field: str) -> Optional[str]:
        value = self.data.get(field)
        if value is None or not isinstance(value, str):
            return None
        return value.strip() or None

    def text(self, field: str, max_len: int, required: bool = True) -> Optional[str]:
        value = self.raw(field)
        if value is None:
            if required:
                self.error(field, "Field required", "missing")
            return None
        if len(value) > max_len:
            self.error(field, f"Must be at most {max_len} characters.")
            return None
        return value

    def apply(self, field: str, fn: Callable[[str], object], required: bool = True):
        value = self.raw(field)
        if value is None:
            if required:
                self.error(field, "Field required", "missing")
            return None
        try:
            return fn(value)
        except ValueError as exc:
            self.error(field, str(exc))
            return None

    def integer(self, field: str, required: bool = True, minimum: Optional[int] = None,
                maximum: Optional[int] = None) -> Optional[int]:
        def _int(value: str) -> int:
            try:
                number = int(value)
            except ValueError:
                raise ValueError("Must be a whole number.")
            if minimum is not None and number < minimum:
                raise ValueError(f"Must be at least {minimum}.")
            if maximum is not None and number > maximum:
                raise ValueError(f"Must be at most {maximum}.")
            return number
        return self.apply(field, _int, required)

    def choice(self, field: str, enum_cls):
        def _choice(value: str):
            try:
                return enum_cls(value)
            except ValueError:
                raise ValueError(f"Must be one of: {', '.join(e.value for e in enum_cls)}.")
        return self.apply(field, _choice)

    def check(self) -> None:
        if self.errors:
            raise HTTPException(status_code=422, detail=self.errors)


def _parse_dob(value: str) -> date:
    try:
        born = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Must be a date in YYYY-MM-DD format.")
    return validation.check_adult(born, service.today_ist())


def _parse_common(form: _Form) -> dict:
    version, _ = texts.current_consent()
    values = {
        "full_name": form.apply("full_name", validation.normalize_name),
        "date_of_birth": form.apply("date_of_birth", _parse_dob),
        "address_line": form.text("address_line", 500),
        "state_code": form.integer("state_lgd_code"),
        "district_code": form.integer("district_lgd_code"),
        "block_code": form.integer("block_lgd_code", required=False),
        "pin_code": form.apply("pin_code", validation.normalize_pin),
        "mobile": form.apply("mobile", validation.normalize_mobile),
        "email": form.apply("email", validation.normalize_email),
        "aadhaar_last4": form.apply("aadhaar_number", validation.aadhaar_last4),
        "pan": form.apply("pan", validation.normalize_pan),
        "bank_account": form.apply("bank_account_number", validation.normalize_bank_account),
        "bank_ifsc": form.apply("bank_ifsc", validation.normalize_ifsc),
    }
    consent_version = form.raw("kyc_consent_version")
    if consent_version is None:
        form.error("kyc_consent_version", "Field required", "missing")
    elif consent_version != version:
        form.error("kyc_consent_version",
                   "This consent notice version is no longer current. Please reload the form and accept the current notice.")
    if (form.raw("kyc_consent_accepted") or "").lower() != "true":
        form.error("kyc_consent_accepted", "The KYC consent notice must be accepted.")
    values["kyc_consent_version"] = consent_version
    return values


async def _read_documents(form: _Form) -> dict[str, uploads.Document]:
    documents = {}
    for doc in APPLICATION_DOCUMENTS:
        upload = form.data.get(doc)
        if upload is None or isinstance(upload, str):
            form.error(doc, "File required", "missing")
            continue
        try:
            documents[doc] = await uploads.read_document(upload, doc)
        except uploads.UploadRejected as exc:
            if exc.status_code == 413:
                raise HTTPException(status_code=413, detail=str(exc))
            form.error(doc, str(exc))
    return documents


def _error(exc: service.AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _require_registration_open() -> None:
    if settings.is_production and texts.consent_is_placeholder():
        raise HTTPException(status_code=503, detail={
            "code": "kyc_consent_not_configured",
            "message": "Registrations are not open yet.",
        })


async def _submit(request: Request, db: Session, application_type: AssociateType,
                  parse_specific: Callable[[_Form, dict], dict], build: Callable[[dict, dict, service.Place], object],
                  seat_check: Optional[Callable[[dict], None]] = None):
    _require_registration_open()
    form = _Form(await request.form(max_files=len(APPLICATION_DOCUMENTS), max_fields=40))
    common = _parse_common(form)
    specific = parse_specific(form, common)
    documents = await _read_documents(form)
    form.check()
    # Everything after reading the request is database and storage work: run it in
    # the threadpool, never on the event loop.
    return await run_in_threadpool(_persist_application, db, application_type, common, specific, documents,
                                   build, seat_check)


def _persist_application(db: Session, application_type: AssociateType, common: dict, specific: dict,
                         documents: dict, build: Callable[[dict, dict, service.Place], object],
                         seat_check: Optional[Callable[[dict], None]]):
    try:
        place = service.resolve_place(
            db, state_code=common["state_code"], district_code=common["district_code"],
            block_code=common["block_code"], field_prefix="", need_district=True, need_block="if_any",
        )
        if seat_check:
            seat_check(specific)
    except service.AssociateError as exc:
        raise _error(exc)

    from app.security.encryption import EncryptionKeyMissingError, encrypt
    from app.security.pii import protect_pan
    from app.storage import r2

    try:
        claims = service.identity_claims(
            pan=common["pan"], mobile=common["mobile"], ifsc=common["bank_ifsc"], account=common["bank_account"],
            aadhaar_last4=common["aadhaar_last4"], dob=common["date_of_birth"],
            name_key=validation.name_key(common["full_name"]),
        )
        bank_account_encrypted = encrypt(common["bank_account"])
        pan_encrypted, pan_last4 = protect_pan(common["pan"])
    except EncryptionKeyMissingError:
        raise HTTPException(status_code=503, detail={
            "code": "encryption_not_configured", "message": "Registrations cannot be accepted right now.",
        })
    if not r2.is_configured():
        raise HTTPException(status_code=503, detail={
            "code": "storage_not_configured", "message": "Registrations cannot be accepted right now.",
        })

    try:
        service.check_not_duplicate(db, claims)
    except service.AssociateError as exc:
        raise _error(exc)

    now = datetime.now(timezone.utc)
    kyc = dict(
        status=ApplicationStatus.submitted,
        full_name=common["full_name"], date_of_birth=common["date_of_birth"],
        address_line=common["address_line"], state_id=place.state.id, district_id=place.district.id,
        block_id=place.block.id if place.block else None, pin_code=common["pin_code"],
        mobile=common["mobile"], email=common["email"], aadhaar_last4=common["aadhaar_last4"],
        pan_encrypted=pan_encrypted, pan_last4=pan_last4, bank_account_encrypted=bank_account_encrypted,
        bank_account_last4=common["bank_account"][-4:], bank_ifsc=common["bank_ifsc"],
        kyc_consent_version=common["kyc_consent_version"], kyc_consent_accepted_at=now,
        documents={}, submitted_at=now, status_changed_at=now, verification_call_completed=False,
    )
    application = build(kyc, specific, place)
    db.add(application)
    try:
        db.flush()
        service.record_claims(db, claims, application_type, application.id)
        stored = {}
        for doc, document in documents.items():
            key = f"associate-applications/{application_type.value}/{application.id}/{doc}/{_uuid()}{document.extension}"
            r2.upload_bytes(key, document.content, document.content_type)
            stored[doc] = {"storage_key": key, "filename": document.filename,
                           "content_type": document.content_type, "size": len(document.content)}
        application.documents = stored
        service.audit(db, service.application_entity(application_type), str(application.id), "submitted", "public",
                      after={"status": "submitted", "state_id": application.state_id,
                             "documents": sorted(stored), "kyc_consent_version": application.kyc_consent_version})
        alert = {"application_id": application.id, "application_type": application_type.value,
                 "name": application.full_name, "state": place.state.name}
        notify_after_commit(db, lambda: notifier_module.notifier.notify("associate_application_submitted", alert),
                            f"associate application {application_type.value} {application.id}")
        db.commit()
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except Exception:
        db.rollback()
        raise
    return JSONResponse(status_code=201, content={
        "application_id": application.id,
        "application_type": application_type.value,
        "status": ApplicationStatus.submitted.value,
        "submitted_at": application.submitted_at.isoformat(),
    })


def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


# ── Registration endpoints ───────────────────────────────────────────────────────

@router.post("/associates/apply", status_code=201)
async def apply_associate(request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request, _apply_request_log, _APPLY_LIMIT)

    def parse(form: _Form, common: dict) -> dict:
        return {"capability": form.choice("capability", AssociateCapability)}

    def build(kyc: dict, specific: dict, place: service.Place):
        return AssociateApplication(**kyc, capability=specific["capability"])

    return await _submit(request, db, AssociateType.associate, parse, build)


@router.post("/preferred/apply", status_code=201)
async def apply_preferred(request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request, _apply_request_log, _APPLY_LIMIT)

    def parse(form: _Form, common: dict) -> dict:
        values = {
            "requested_level": form.choice("requested_level", TerritoryLevel),
            "requested_state_code": form.integer("requested_state_lgd_code"),
            "requested_district_code": form.integer("requested_district_lgd_code", required=False),
            "requested_block_code": form.integer("requested_block_lgd_code", required=False),
            "business_type": form.text("business_type", 100),
            "gstin": form.apply("gstin", validation.normalize_gstin, required=False),
            "office_address": form.text("office_address", 500),
            "prior_experience": form.text("prior_experience", 2000),
            "team_size": form.integer("team_size", minimum=0, maximum=100_000),
        }
        references = []
        applicant_mobile = common["mobile"]
        for n in (1, 2):
            name = form.text(f"reference_{n}_name", 200)
            mobile = form.apply(f"reference_{n}_mobile", validation.normalize_mobile)
            references.append({"name": name, "mobile": mobile})
        mobiles = [r["mobile"] for r in references]
        if all(mobiles) and mobiles[0] == mobiles[1]:
            form.error("reference_2_mobile", "The two references must be different people.")
        for n, m in enumerate(mobiles, start=1):
            if m and applicant_mobile and m == applicant_mobile:
                form.error(f"reference_{n}_mobile", "A reference cannot be the applicant.")
        values["references"] = references
        return values

    def resolve_requested(db_: Session, specific: dict) -> service.Place:
        level = specific["requested_level"]
        return service.resolve_place(
            db_, state_code=specific["requested_state_code"],
            district_code=specific["requested_district_code"] if level != TerritoryLevel.state_head else None,
            block_code=specific["requested_block_code"] if level == TerritoryLevel.block else None,
            field_prefix="requested_", need_district=level != TerritoryLevel.state_head,
            need_block=level == TerritoryLevel.block,
        )

    requested: dict = {}

    def seat_check(specific: dict) -> None:
        place = resolve_requested(db, specific)
        requested["place"] = place
        if not service.seat_is_available(db, specific["requested_level"], place):
            raise service.SeatFullError(service._seat_full_message(specific["requested_level"], place))

    def build(kyc: dict, specific: dict, place: service.Place):
        target = requested["place"]
        return PreferredAssociateApplication(
            **kyc,
            requested_level=specific["requested_level"],
            requested_state_id=target.state.id,
            requested_district_id=target.district.id if target.district else None,
            requested_block_id=target.block.id if target.block else None,
            business_type=specific["business_type"], gstin=specific["gstin"],
            office_address=specific["office_address"], prior_experience=specific["prior_experience"],
            team_size=specific["team_size"], references=specific["references"],
        )

    return await _submit(request, db, AssociateType.preferred_associate, parse, build, seat_check)


# ── Verification (what the ID card's QR code opens) ─────────────────────────────

@router.get("/verify/{associate_id}")
def verify_associate(associate_id: str, request: Request, db: Session = Depends(get_db)):
    """Only name, photo, type, level and active/inactive — nothing else about the
    associate is public."""
    _check_rate_limit(request, _verify_request_log, _VERIFY_LIMIT)
    associate = service.get_associate(db, associate_id) if service.ASSOCIATE_ID_RE.fullmatch(associate_id) else None
    if associate is None:
        raise HTTPException(status_code=404, detail="No associate with this ID.")
    photo_url = None
    if associate.photo_storage_key:
        from app.storage import r2
        try:
            photo_url, _ = r2.signed_get_url(associate.photo_storage_key)
        except r2.StorageNotConfiguredError:
            photo_url = None
    return {
        "name": associate.full_name,
        "photo_url": photo_url,
        "type": associate.associate_type.value,
        "level": associate.level.value if associate.level else None,
        "status": "active" if associate.status == AssociateStatus.active else "inactive",
    }


# ── Reference data for the forms ─────────────────────────────────────────────────

def _require_territories(db: Session) -> None:
    """With no active state at all the master has not been loaded (or not migrated
    yet): say so, rather than answer 200 with an empty list the form shows as a blank
    dropdown, or 404 "unknown state" for every code."""
    if db.execute(select(TerritoryState.id).where(TerritoryState.is_active.is_(True)).limit(1)).first() is None:
        raise HTTPException(status_code=503, detail={"code": "territories_not_seeded",
                                                     "message": "Territory data has not been loaded yet."})


@router.get("/territories/states", dependencies=[Depends(_lookup_limit)])
def list_states(db: Session = Depends(get_db)):
    _require_territories(db)
    rows = db.execute(select(TerritoryState).where(TerritoryState.is_active.is_(True))
                      .order_by(TerritoryState.name)).scalars()
    return {"items": [{"lgd_code": s.lgd_code, "iso_code": s.iso_code, "name": s.name} for s in rows]}


@router.get("/territories/states/{state_lgd_code}/districts", dependencies=[Depends(_lookup_limit)])
def list_districts(state_lgd_code: int, db: Session = Depends(get_db)):
    _require_territories(db)
    state = db.execute(select(TerritoryState).where(
        TerritoryState.lgd_code == state_lgd_code, TerritoryState.is_active.is_(True))).scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown state.")
    rows = db.execute(select(TerritoryDistrict).where(
        TerritoryDistrict.state_id == state.id, TerritoryDistrict.is_active.is_(True))
        .order_by(TerritoryDistrict.name)).scalars()
    items = []
    for d in rows:
        items.append({"lgd_code": d.lgd_code, "name": d.name, "has_blocks": service.district_has_blocks(db, d.id)})
    return {"items": items}


@router.get("/territories/districts/{district_lgd_code}/blocks", dependencies=[Depends(_lookup_limit)])
def list_blocks(district_lgd_code: int, db: Session = Depends(get_db)):
    _require_territories(db)
    district = db.execute(select(TerritoryDistrict).where(
        TerritoryDistrict.lgd_code == district_lgd_code, TerritoryDistrict.is_active.is_(True))).scalar_one_or_none()
    if district is None:
        raise HTTPException(status_code=404, detail="Unknown district.")
    rows = db.execute(select(TerritoryBlock).where(
        TerritoryBlock.district_id == district.id, TerritoryBlock.is_active.is_(True))
        .order_by(TerritoryBlock.name)).scalars()
    return {"items": [{"lgd_code": b.lgd_code, "name": b.name} for b in rows]}


@router.get("/kyc-consent", dependencies=[Depends(_lookup_limit)])
def kyc_consent():
    version, text = texts.current_consent()
    return {"version": version, "text": text}


@router.get("/preferred/seat-availability", dependencies=[Depends(_lookup_limit)])
def seat_availability(level: TerritoryLevel, state_lgd_code: int, district_lgd_code: Optional[int] = None,
                      block_lgd_code: Optional[int] = None, db: Session = Depends(get_db)):
    """Whether a Preferred Associate seat can still be applied for — so the form can
    say so before the applicant uploads anything. The submission re-checks."""
    try:
        place = service.resolve_place(
            db, state_code=state_lgd_code,
            district_code=district_lgd_code if level != TerritoryLevel.state_head else None,
            block_code=block_lgd_code if level == TerritoryLevel.block else None,
            field_prefix="", need_district=level != TerritoryLevel.state_head,
            need_block=level == TerritoryLevel.block,
        )
    except service.AssociateError as exc:
        raise _error(exc)
    capacity = service.seat_capacity(level)
    held = len(service.held_seats(db, level, place))
    return {"level": level.value, "capacity": capacity, "held": held,
            "available": capacity is None or held < capacity}
