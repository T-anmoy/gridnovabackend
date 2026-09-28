"""Associate registration, review and lifecycle — every write goes through here and
writes an AuditLog entry in the same transaction. Nothing is ever deleted: an
application ends approved or rejected, an associate ends deactivated, a seat ends
released.

Applications, associates and seats have no lead, so there is no TimelineEvent for
them (that table is lead-scoped); the audit log is their history.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Union

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.models import (
    MAX_DISTRICT_HEADS_PER_DISTRICT,
    MAX_STATE_HEADS_PER_STATE,
    ApplicationStatus,
    Associate,
    AssociateApplication,
    AssociateIdentityClaim,
    AssociateStatus,
    AssociateType,
    AuditLog,
    ClerkInviteStatus,
    PreferredAssociateApplication,
    TerritoryBlock,
    TerritoryDistrict,
    TerritoryLevel,
    TerritorySeat,
    TerritoryState,
)

Application = Union[AssociateApplication, PreferredAssociateApplication]
APPLICATION_MODELS = {
    AssociateType.associate: AssociateApplication,
    AssociateType.preferred_associate: PreferredAssociateApplication,
}
ASSOCIATE_ID_RE = re.compile(r"GNA-[A-Z]{2}-\d{2}-\d{5}")


class AssociateError(Exception):
    """A refused operation. `status_code` and `code` become the HTTP error."""
    status_code = 409
    code = "conflict"

    def __init__(self, message: str, *, code: Optional[str] = None, status_code: Optional[int] = None,
                 field: Optional[str] = None, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.field = field
        self.extra = extra

    def body(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        if self.extra:
            body.update(self.extra)
        return body


class SeatFullError(AssociateError):
    code = "seat_full"


class DuplicateRegistrationError(AssociateError):
    code = "duplicate_registration"


class InvalidTransitionError(AssociateError):
    code = "invalid_transition"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def today_ist() -> date:
    """Today's date in the business timezone — for ages and the Associate ID's year."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(settings.business_timezone)).date()


def _audit(db: Session, entity_type: str, entity_id: str, action: str, performed_by: str,
           before: Any = None, after: Any = None) -> None:
    db.add(AuditLog(entity_type=entity_type, entity_id=entity_id, action=action,
                    performed_by=performed_by, before_value=before, after_value=after))


def application_entity(application_type: AssociateType) -> str:
    return f"{application_type.value}_application"


# ── Territory lookups ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Place:
    state: TerritoryState
    district: Optional[TerritoryDistrict] = None
    block: Optional[TerritoryBlock] = None

    def label(self) -> str:
        parts = [p.name for p in (self.block, self.district, self.state) if p is not None]
        return ", ".join(parts)


def _active_by_code(db: Session, model, code: Optional[int]):
    if code is None:
        return None
    return db.execute(select(model).where(model.lgd_code == code, model.is_active.is_(True))).scalar_one_or_none()


def district_has_blocks(db: Session, district_id: int) -> bool:
    return db.execute(select(func.count(TerritoryBlock.id)).where(
        TerritoryBlock.district_id == district_id, TerritoryBlock.is_active.is_(True))).scalar_one() > 0


def resolve_place(db: Session, *, state_code: Optional[int], district_code: Optional[int],
                  block_code: Optional[int], field_prefix: str, need_district: bool,
                  need_block: Union[bool, str]) -> Place:
    """Resolve LGD codes into active master rows that nest correctly. need_block may
    be "if_any" — required only when the district has active blocks (Kolkata has none)."""
    state = _active_by_code(db, TerritoryState, state_code)
    if state is None:
        raise AssociateError("Unknown or inactive state.", code="invalid_territory", status_code=422,
                             field=f"{field_prefix}state_lgd_code")
    district = None
    if district_code is not None or need_district:
        district = _active_by_code(db, TerritoryDistrict, district_code)
        if district is None or district.state_id != state.id:
            raise AssociateError("Unknown or inactive district for that state.", code="invalid_territory",
                                 status_code=422, field=f"{field_prefix}district_lgd_code")
    block = None
    required = need_block is True or (need_block == "if_any" and district is not None
                                      and district_has_blocks(db, district.id))
    if block_code is not None or required:
        if district is None:
            raise AssociateError("A block needs a district.", code="invalid_territory", status_code=422,
                                 field=f"{field_prefix}district_lgd_code")
        block = _active_by_code(db, TerritoryBlock, block_code)
        if block is None or block.district_id != district.id:
            raise AssociateError("Unknown or inactive block for that district.", code="invalid_territory",
                                 status_code=422, field=f"{field_prefix}block_lgd_code")
    return Place(state=state, district=district, block=block)


# ── Seats ───────────────────────────────────────────────────────────────────────

def seat_capacity(level: TerritoryLevel) -> Optional[int]:
    return {TerritoryLevel.state_head: MAX_STATE_HEADS_PER_STATE,
            TerritoryLevel.district_head: MAX_DISTRICT_HEADS_PER_DISTRICT}.get(level)


def held_seats(db: Session, level: TerritoryLevel, place: Place) -> list[TerritorySeat]:
    q = select(TerritorySeat).where(TerritorySeat.level == level, TerritorySeat.released_at.is_(None))
    if level == TerritoryLevel.state_head:
        q = q.where(TerritorySeat.state_id == place.state.id)
    elif level == TerritoryLevel.district_head:
        q = q.where(TerritorySeat.district_id == place.district.id)
    else:
        q = q.where(TerritorySeat.block_id == place.block.id)
    return list(db.execute(q).scalars())


def seat_is_available(db: Session, level: TerritoryLevel, place: Place) -> bool:
    capacity = seat_capacity(level)
    return capacity is None or len(held_seats(db, level, place)) < capacity


def _seat_full_message(level: TerritoryLevel, place: Place) -> str:
    if level == TerritoryLevel.state_head:
        return (f"Both State Head seats for {place.state.name} are already filled "
                f"(maximum {MAX_STATE_HEADS_PER_STATE} per state).")
    return (f"The District Head seat for {place.district.name} is already filled "
            f"(maximum {MAX_DISTRICT_HEADS_PER_DISTRICT} per district).")


def claim_seat(db: Session, associate: Associate, level: TerritoryLevel, place: Place, performed_by: str) -> TerritorySeat:
    """Take a seat for an associate. The database refuses a seat beyond the limit
    (partial unique index + CHECK on territory_seats); that refusal — including the
    loser of two simultaneous approvals — surfaces as SeatFullError, after which the
    session must be rolled back (the caller's whole operation is abandoned)."""
    seat_no = None
    capacity = seat_capacity(level)
    if capacity is not None:
        taken = {s.seat_no for s in held_seats(db, level, place)}
        free = [n for n in range(1, capacity + 1) if n not in taken]
        if not free:
            raise SeatFullError(_seat_full_message(level, place))
        seat_no = free[0]
    seat = TerritorySeat(
        level=level, state_id=place.state.id,
        district_id=place.district.id if place.district else None,
        block_id=place.block.id if place.block else None,
        seat_no=seat_no, holder_id=associate.id, claimed_at=_now(),
    )
    db.add(seat)
    try:
        db.flush()
    except IntegrityError:
        raise SeatFullError(_seat_full_message(level, place))
    _audit(db, "territory_seat", str(seat.id), "claimed", performed_by,
           after={"level": level.value, "state_id": seat.state_id, "district_id": seat.district_id,
                  "block_id": seat.block_id, "seat_no": seat_no, "associate_id": associate.associate_id})
    return seat


def release_seat(db: Session, seat: TerritorySeat, performed_by: str, reason: str) -> TerritorySeat:
    if seat.released_at is not None:
        raise InvalidTransitionError("This seat has already been released.")
    seat.released_at = _now()
    seat.released_by = performed_by
    seat.release_reason = reason
    db.flush()
    _audit(db, "territory_seat", str(seat.id), "released", performed_by,
           before={"released_at": None}, after={"released_at": seat.released_at.isoformat(), "reason": reason})
    return seat


# ── Identity claims (duplicate-registration guard) ──────────────────────────────

def _claim_key() -> bytes:
    from app.security.encryption import EncryptionKeyMissingError

    if not settings.fernet_encryption_key:
        raise EncryptionKeyMissingError("FERNET_ENCRYPTION_KEY is not set — registrations cannot be accepted.")
    return hashlib.sha256(b"gridnova-associate-identity-claims:" + settings.fernet_encryption_key.encode()).digest()


def claim_hash(kind: str, value: str) -> str:
    return hmac.new(_claim_key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


CLAIM_MESSAGES = {
    "pan": ("pan", "A registration with this PAN already exists."),
    "mobile": ("mobile", "A registration with this mobile number already exists."),
    "bank_account": ("bank_account_number", "A registration with this bank account already exists."),
    "aadhaar_dob_name": ("aadhaar_number",
                         "A registration with this Aadhaar number, date of birth and name already exists."),
}


def identity_claims(*, pan: str, mobile: str, ifsc: str, account: str, aadhaar_last4: str,
                    dob: date, name_key: str) -> dict[str, str]:
    """kind -> HMAC for the four attributes a person may register only once with,
    across both programmes. A bank account is its IFSC bank code + account number,
    since account numbers are only unique within one bank."""
    return {
        "pan": claim_hash("pan", pan),
        "mobile": claim_hash("mobile", mobile),
        "bank_account": claim_hash("bank_account", f"{ifsc[:4]}|{account}"),
        "aadhaar_dob_name": claim_hash("aadhaar_dob_name", f"{aadhaar_last4}|{dob.isoformat()}|{name_key}"),
    }


def check_not_duplicate(db: Session, claims: dict[str, str]) -> None:
    for kind, value_hash in claims.items():
        exists = db.execute(select(AssociateIdentityClaim.id).where(
            AssociateIdentityClaim.kind == kind, AssociateIdentityClaim.value_hash == value_hash)).first()
        if exists:
            field, message = CLAIM_MESSAGES[kind]
            raise DuplicateRegistrationError(message, field=field)


def record_claims(db: Session, claims: dict[str, str], application_type: AssociateType, application_id: int) -> None:
    """Insert the claims; the unique index refuses one that raced past
    check_not_duplicate (the session must then be rolled back)."""
    for kind, value_hash in claims.items():
        db.add(AssociateIdentityClaim(kind=kind, value_hash=value_hash,
                                      application_type=application_type, application_id=application_id))
    try:
        db.flush()
    except IntegrityError:
        raise DuplicateRegistrationError("A registration with these identity details already exists.")


# ── Applications ────────────────────────────────────────────────────────────────

_TRANSITIONS = {
    ApplicationStatus.submitted: {ApplicationStatus.under_review},
    ApplicationStatus.under_review: {ApplicationStatus.needs_info, ApplicationStatus.approved,
                                     ApplicationStatus.rejected},
    ApplicationStatus.needs_info: {ApplicationStatus.under_review},
    ApplicationStatus.approved: set(),
    ApplicationStatus.rejected: set(),
}


def get_application(db: Session, application_type: AssociateType, application_id: int) -> Optional[Application]:
    return db.get(APPLICATION_MODELS[application_type], application_id)


def change_application_status(db: Session, application: Application, application_type: AssociateType,
                              target: ApplicationStatus, performed_by: str, note: Optional[str] = None) -> Application:
    """Move an application along submitted → under_review → needs_info ⇄ under_review
    → rejected. Approval has its own path (approve()), since it does far more."""
    if target == ApplicationStatus.approved:
        raise InvalidTransitionError("Use the approve endpoint to approve an application.")
    current = application.status
    if target not in _TRANSITIONS[current]:
        raise InvalidTransitionError(f"An application cannot move from {current.value} to {target.value}.")
    note = (note or "").strip() or None
    if target == ApplicationStatus.rejected and not note:
        raise AssociateError("A rejection needs a reason.", code="reason_required", status_code=422, field="reason")
    if target == ApplicationStatus.needs_info and not note:
        raise AssociateError("Say what information is needed.", code="note_required", status_code=422, field="note")

    application.status = target
    application.status_changed_at = _now()
    if target == ApplicationStatus.needs_info:
        application.needs_info_note = note
    if target == ApplicationStatus.rejected:
        application.rejection_reason = note
        application.decided_at = _now()
        application.decided_by_employee_id = performed_by
    db.flush()
    _audit(db, application_entity(application_type), str(application.id), f"status_{target.value}", performed_by,
           before={"status": current.value}, after={"status": target.value, "note": note})
    return application


def next_associate_sequence(db: Session, key: str) -> int:
    """Same single-statement atomic upsert as app/memory/reference_id.next_sequence."""
    row = db.execute(
        text(
            "INSERT INTO associate_id_counters (counter_key, value) VALUES (:key, 1) "
            "ON CONFLICT(counter_key) DO UPDATE SET value = associate_id_counters.value + 1 "
            "RETURNING value"
        ),
        {"key": key},
    ).first()
    return int(row[0])


def issue_associate_id(db: Session, state_iso: str, year: int) -> str:
    yy = f"{year % 100:02d}"
    seq = next_associate_sequence(db, f"GNA-{state_iso}-{yy}")
    return f"GNA-{state_iso}-{yy}-{seq:05d}"


def approve(db: Session, application: Application, application_type: AssociateType, performed_by: str,
            verification_call_note: str, today: date) -> Associate:
    """Approve an application under review: issue the Associate ID, create the
    associate and — for a Preferred Associate — take the requested seat. Everything
    here is one transaction; the caller commits. The Clerk invitation, the PDFs and
    the notification happen after that commit (see the approve route)."""
    if application.status != ApplicationStatus.under_review:
        raise InvalidTransitionError(
            f"Only an application under review can be approved (this one is {application.status.value}).")
    note = (verification_call_note or "").strip()
    if not note:
        raise AssociateError("Record what the verification call confirmed.", code="verification_call_required",
                             status_code=422, field="verification_call_note")

    place = None
    level = None
    if application_type == AssociateType.preferred_associate:
        level = application.requested_level
        place = Place(
            state=db.get(TerritoryState, application.requested_state_id),
            district=db.get(TerritoryDistrict, application.requested_district_id) if application.requested_district_id else None,
            block=db.get(TerritoryBlock, application.requested_block_id) if application.requested_block_id else None,
        )
        if not seat_is_available(db, level, place):
            raise SeatFullError(_seat_full_message(level, place))
        id_state = place.state
    else:
        id_state = db.get(TerritoryState, application.state_id)

    associate_id = issue_associate_id(db, id_state.iso_code, today.year)
    documents = application.documents or {}
    photo = documents.get("photo") or {}
    associate = Associate(
        associate_id=associate_id,
        associate_type=application_type,
        status=AssociateStatus.active,
        associate_application_id=application.id if application_type == AssociateType.associate else None,
        preferred_application_id=application.id if application_type == AssociateType.preferred_associate else None,
        full_name=application.full_name,
        email=application.email,
        mobile=application.mobile,
        state_id=id_state.id,
        capability=getattr(application, "capability", None),
        level=level,
        territory_state_id=place.state.id if place else None,
        territory_district_id=place.district.id if place and place.district else None,
        territory_block_id=place.block.id if place and place.block else None,
        photo_storage_key=photo.get("storage_key"),
        photo_content_type=photo.get("content_type"),
        approved_at=_now(),
        approved_by_employee_id=performed_by,
        status_changed_at=_now(),
    )
    db.add(associate)
    db.flush()
    if place is not None:
        claim_seat(db, associate, level, place, performed_by)

    before = application.status.value
    application.status = ApplicationStatus.approved
    application.status_changed_at = _now()
    application.verification_call_completed = True
    application.verification_call_note = note
    application.decided_at = _now()
    application.decided_by_employee_id = performed_by
    db.flush()
    _audit(db, application_entity(application_type), str(application.id), "status_approved", performed_by,
           before={"status": before},
           after={"status": "approved", "associate_id": associate_id, "verification_call_note": note})
    _audit(db, "associate", associate_id, "created", performed_by,
           after={"associate_type": application_type.value, "application_id": application.id,
                  "level": level.value if level else None})
    return associate


# ── Associate lifecycle: active ⇄ suspended → deactivated ────────────────────────

_ASSOCIATE_TRANSITIONS = {
    AssociateStatus.active: {AssociateStatus.suspended, AssociateStatus.deactivated},
    AssociateStatus.suspended: {AssociateStatus.active, AssociateStatus.deactivated},
    AssociateStatus.deactivated: set(),
}


def change_associate_status(db: Session, associate: Associate, target: AssociateStatus,
                            performed_by: str, reason: Optional[str]) -> Associate:
    """Suspension keeps the associate's seat (it is temporary); deactivation is final
    and releases it. A suspended or deactivated associate is refused at sign-in."""
    current = associate.status
    if target not in _ASSOCIATE_TRANSITIONS[current]:
        raise InvalidTransitionError(f"An associate cannot move from {current.value} to {target.value}.")
    reason = (reason or "").strip() or None
    if target in (AssociateStatus.suspended, AssociateStatus.deactivated) and not reason:
        raise AssociateError("Give a reason.", code="reason_required", status_code=422, field="reason")
    if target == AssociateStatus.active and associate.level is not None:
        # Reinstating a Preferred Associate whose seat was released while suspended:
        # they take it back if it is still free, and reinstatement is refused if not.
        holds = db.execute(select(TerritorySeat.id).where(
            TerritorySeat.holder_id == associate.id, TerritorySeat.released_at.is_(None))).first()
        if not holds:
            place = Place(
                state=db.get(TerritoryState, associate.territory_state_id),
                district=db.get(TerritoryDistrict, associate.territory_district_id) if associate.territory_district_id else None,
                block=db.get(TerritoryBlock, associate.territory_block_id) if associate.territory_block_id else None,
            )
            claim_seat(db, associate, associate.level, place, performed_by)
    associate.status = target
    associate.status_changed_at = _now()
    associate.status_reason = reason
    db.flush()
    _audit(db, "associate", associate.associate_id, f"status_{target.value}", performed_by,
           before={"status": current.value}, after={"status": target.value, "reason": reason})
    if target == AssociateStatus.deactivated:
        for seat in db.execute(select(TerritorySeat).where(
                TerritorySeat.holder_id == associate.id, TerritorySeat.released_at.is_(None))).scalars():
            release_seat(db, seat, performed_by, f"Associate {associate.associate_id} deactivated: {reason}")
    return associate


def get_associate(db: Session, associate_id: str) -> Optional[Associate]:
    return db.execute(select(Associate).where(Associate.associate_id == associate_id)).scalar_one_or_none()


def record_clerk_invite(db: Session, associate: Associate, status: ClerkInviteStatus,
                        invitation_id: Optional[str], performed_by: str, error: Optional[str] = None) -> None:
    before = associate.clerk_invite_status.value if associate.clerk_invite_status else None
    associate.clerk_invite_status = status
    if invitation_id:
        associate.clerk_invitation_id = invitation_id
    db.flush()
    _audit(db, "associate", associate.associate_id, f"clerk_invite_{status.value}", performed_by,
           before={"clerk_invite_status": before},
           after={"clerk_invite_status": status.value, "clerk_invitation_id": invitation_id, "error": error})


def record_documents(db: Session, associate: Associate, performed_by: str, *, id_card_key: Optional[str],
                     letter_key: Optional[str], error: Optional[str]) -> None:
    if id_card_key:
        associate.id_card_storage_key = id_card_key
    if letter_key:
        associate.welcome_letter_storage_key = letter_key
    if error is None:
        associate.documents_generated_at = _now()
    associate.documents_error = error
    db.flush()
    _audit(db, "associate", associate.associate_id,
           "documents_generated" if error is None else "documents_failed", performed_by,
           after={"id_card": bool(associate.id_card_storage_key),
                  "welcome_letter": bool(associate.welcome_letter_storage_key), "error": error})


def audit(db: Session, entity_type: str, entity_id: str, action: str, performed_by: str,
          before: Any = None, after: Any = None) -> None:
    _audit(db, entity_type, entity_id, action, performed_by, before, after)
