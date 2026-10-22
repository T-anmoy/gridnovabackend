"""Associate referral links (BE-10).

An Associate shares /estimate?ref=<their Associate ID> on WhatsApp or as a QR code. A
public estimate request or call booking carrying a `ref` that names an ACTIVE Associate
is recorded on the lead (leads.referred_by_associate_id) and audited; anything else —
unknown, malformed, suspended or deactivated — is ignored silently, because a customer
must never see an error because of a bad link. The value never appears in a
customer-facing response.

A referral is information, not attribution: the first complete onboarding verified by
Grid Nova still decides who the customer is attributed to (Associate Terms A7;
app/onboarding/service.py). Whether it should be is REF-1
(config/associate_onboarding.yaml referrals.auto_attribute).

The referring Associate sees their referred enquiries (GET /api/v1/associate/referrals):
the customer's name, location and the kind of enquiry, and the mobile only when the
customer ticked "share my details with the Associate who referred me"
(share_with_referrer). Never the email.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.associates.service import ASSOCIATE_ID_RE
from app.memory.models import Associate, AssociateStatus, AuditLog, Customer, Lead, LeadSource, PipelineStage

MAX_REF_LENGTH = 64
SHARE_PATH = "/estimate?ref={associate_id}"

# The kind of enquiry, by the lead's source (only the two public forms take a ref).
ENQUIRY_KIND = {LeadSource.website_estimate_form: "estimate", LeadSource.website_booking: "call_booked"}

# A plain status key for the Associate (the frontend words it): where the enquiry is,
# without Grid Nova's internal pipeline detail.
STATUS = {
    PipelineStage.new: "received",
    PipelineStage.qualifying: "in_progress",
    PipelineStage.qualified: "in_progress",
    PipelineStage.proposal_draft: "in_progress",
    PipelineStage.proposal_sent: "proposal_sent",
    PipelineStage.negotiation: "proposal_sent",
    PipelineStage.won: "won",
    PipelineStage.lost: "closed",
}


def normalise(ref: Any) -> Optional[str]:
    """`ref` as an Associate ID (GNA-WB-26-00001), or None when it cannot be one.
    Spaces are dropped and letters upper-cased, so a hand-typed link still works."""
    if not isinstance(ref, str) or len(ref) > MAX_REF_LENGTH:
        return None
    value = re.sub(r"\s+", "", ref).upper()
    return value if ASSOCIATE_ID_RE.fullmatch(value) else None


def lenient_ref(cls, value: Any) -> Optional[str]:
    """Pydantic before-validator for a request's `ref`: anything but a short string
    becomes None, so a mangled link can never fail the customer's request."""
    return value if isinstance(value, str) and len(value) <= MAX_REF_LENGTH else None


def referrer(db: Session, ref: Any) -> Optional[Associate]:
    """The ACTIVE Associate a ref names, else None. Every Associate row is an approved
    one (an Associate exists only by approving an application)."""
    associate_id = normalise(ref)
    if associate_id is None:
        return None
    associate = db.execute(select(Associate).where(Associate.associate_id == associate_id)).scalar_one_or_none()
    return associate if associate is not None and associate.status == AssociateStatus.active else None


def record(db: Session, lead: Lead, ref: Any, share_with_referrer: Optional[bool], performed_by: str) -> None:
    """Stamp a new public lead with its referrer, if `ref` names an active Associate.
    Silent otherwise. Caller commits."""
    associate = referrer(db, ref)
    if associate is None:
        return
    lead.referred_by_associate_id = associate.id
    lead.share_with_referrer = bool(share_with_referrer)
    db.flush()
    db.add(AuditLog(entity_type="lead", entity_id=str(lead.id), action="referral_recorded",
                    performed_by=performed_by,
                    after_value={"referred_by_associate_id": associate.associate_id,
                                 "share_with_referrer": lead.share_with_referrer,
                                 "source": lead.source.value}))


def link(associate: Associate) -> dict:
    return {"associate_id": associate.associate_id,
            "share_path": SHARE_PATH.format(associate_id=associate.associate_id)}


def referrers_by_pk(db: Session, pks) -> dict[int, Associate]:
    ids = {p for p in pks if p is not None}
    if not ids:
        return {}
    return {a.id: a for a in db.execute(select(Associate).where(Associate.id.in_(ids))).scalars()}


def brief(associate: Optional[Associate]) -> Optional[dict]:
    """The referring Associate on a staff lead response."""
    if associate is None:
        return None
    return {"associate_id": associate.associate_id, "full_name": associate.full_name}


def _location(fields: dict) -> Optional[str]:
    value = (fields or {}).get("location")
    return value if isinstance(value, str) and value.strip() else None


def item(lead: Lead, customer: Customer) -> dict:
    """One referred enquiry as the referring Associate sees it. `customer_mobile` is
    present only when the customer agreed to share it."""
    fields = lead.collected_fields or {}
    name = fields.get("name") if isinstance(fields.get("name"), str) else None
    out = {"lead_id": lead.id,
           "created_at": lead.created_at.isoformat(),
           "customer_name": name or customer.name,
           "location": _location(fields),
           "pin_code": fields.get("pin_code") if isinstance(fields.get("pin_code"), str) else None,
           "enquiry_kind": ENQUIRY_KIND.get(lead.source, "other"),
           "status": STATUS.get(lead.pipeline_stage, "in_progress"),
           "share_with_referrer": lead.share_with_referrer}
    # The number this enquiry was made with (the Customer row may be an older one,
    # matched by email).
    mobile = fields.get("phone") if isinstance(fields.get("phone"), str) else None
    if lead.share_with_referrer and (mobile or customer.phone):
        out["customer_mobile"] = mobile or customer.phone
    return out


def page(db: Session, associate: Associate, page_no: int, page_size: int) -> dict:
    """The Associate's OWN referred enquiries, newest first."""
    q = (select(Lead, Customer).join(Customer, Customer.id == Lead.customer_id)
         .where(Lead.referred_by_associate_id == associate.id))
    total = db.execute(select(func.count()).select_from(
        select(Lead.id).where(Lead.referred_by_associate_id == associate.id).subquery())).scalar_one()
    rows = db.execute(q.order_by(Lead.created_at.desc(), Lead.id.desc())
                      .offset((page_no - 1) * page_size).limit(page_size)).all()
    return {"items": [item(lead, customer) for lead, customer in rows], "page": page_no, "page_size": page_size,
            "total": int(total)}
