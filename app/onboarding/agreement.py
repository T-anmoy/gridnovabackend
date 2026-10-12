"""Customer agreement for associate-sourced projects: a PDF for printing on ₹10
non-judicial stamp paper, then the signed scan with the stamp paper's number and date.

The PDF carries the parties and the project's details from the system; the agreement's
own wording comes from config/associate_onboarding.yaml (agreement.body), which is a
clearly labelled placeholder until Grid Nova supplies the text. Rendered with WeasyPrint
like the proposal PDF and the associate ID card; stored in the private bucket.
"""
from __future__ import annotations

import html
import re
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.pdf.fonts import FONT_STACK
from app.memory.models import (
    AuditLog, Customer, CustomerAgreement, CustomerOnboarding, Project, ProjectStage, Proposal, ProposalInputs,
    ProposalStatus,
)
from app.onboarding import config, progress

_STAMP_NUMBER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 /\-]{0,49}")


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def source_proposal(db: Session, onboarding: CustomerOnboarding) -> Optional[Proposal]:
    """The formal proposal the customer accepted (online or in person) — never an
    indicative estimate."""
    return db.execute(select(Proposal).where(Proposal.lead_id == onboarding.lead_id,
                                             Proposal.status == ProposalStatus.accepted)
                      .order_by(Proposal.version.desc())).scalars().first()


def get(db: Session, onboarding: CustomerOnboarding) -> Optional[CustomerAgreement]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and onboarding.id in pre.agreements:
        return pre.agreements[onboarding.id]
    return db.execute(select(CustomerAgreement).where(CustomerAgreement.onboarding_id == onboarding.id)).scalar_one_or_none()


def agreement_html(db: Session, onboarding: CustomerOnboarding, proposal: Proposal, today: date) -> str:
    from app.proposals.content_engine import load_content_config

    company = load_content_config().get("company", {})
    cfg = config.load()["agreement"]
    customer = db.get(Customer, onboarding.customer_id)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    project = db.get(Project, onboarding.project_id)
    from app.pricing.price_list import document_amount
    value = document_amount(proposal.total_value) if proposal.total_value else "As per the accepted proposal"
    rows = [
        ("Date", today.isoformat()),
        ("Customer", customer.name),
        ("Site address", f"{inputs.full_address or ''}, {inputs.district}, {inputs.state} — {inputs.pin_code}"),
        ("Electricity consumer number", inputs.consumer_number or "—"),
        ("DISCOM", inputs.discom or "—"),
        ("Project reference", project.reference_id),
        ("System capacity", f"{inputs.desired_output_kw:g} kW" if inputs.desired_output_kw else "—"),
        ("Accepted proposal", f"#{proposal.id}, version {proposal.version}"),
        ("Project value", value),
    ]
    table = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><style>
  @page {{ size: A4; margin: 20mm 20mm 20mm 20mm; }}
  @page :first {{ margin-top: {float(cfg['stamp_paper_top_margin_mm'])}mm; }}
  body {{ font-family: {FONT_STACK}; color: #111; font-size: 10.5pt; line-height: 1.5; }}
  h1 {{ font-size: 14pt; text-align: center; margin: 0 0 6mm; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 6mm; }}
  th {{ text-align: left; width: 38%; padding: 1.2mm 2mm; border: 0.2mm solid #999; background: #f3f3f3; }}
  td {{ padding: 1.2mm 2mm; border: 0.2mm solid #999; }}
  .body {{ white-space: pre-wrap; margin-bottom: 14mm; }}
  .sign {{ display: flex; justify-content: space-between; margin-top: 18mm; }}
  .sign div {{ width: 45%; border-top: 0.3mm solid #111; padding-top: 2mm; font-size: 9.5pt; }}
</style></head><body>
  <h1>Agreement between {_e(company.get('name') or 'Grid Nova Energy')} and {_e(customer.name)}</h1>
  <table>{table}</table>
  <div class="body">{_e(cfg['body'])}</div>
  <div class="sign">
    <div>For {_e(company.get('name') or 'Grid Nova Energy')}<br>Name, signature and seal</div>
    <div>Customer: {_e(customer.name)}<br>Signature</div>
  </div>
</body></html>"""


def generate(db: Session, onboarding: CustomerOnboarding, performed_by: str, today: date) -> CustomerAgreement:
    """(Re)generate the agreement PDF. Needs the `accepted` stage; refused once the
    signed copy is in. Caller commits."""
    from app.associates.documents import render_pdf
    from app.storage import r2

    if ProjectStage.accepted not in progress.recorded(db, onboarding.project_id):
        raise AssociateError("The customer has not accepted a proposal yet.", code="not_accepted", status_code=409)
    existing = get(db, onboarding)
    if existing is not None and existing.signed_scan_key:
        raise AssociateError("The signed agreement is already on file; it cannot be regenerated.",
                             code="already_signed", status_code=409)
    proposal = source_proposal(db, onboarding)
    if proposal is None:
        raise AssociateError("No proposal was sent to this customer.", code="no_proposal", status_code=409)
    pdf = render_pdf(agreement_html(db, onboarding, proposal, today))
    key = f"agreements/{onboarding.id}/{uuid.uuid4().hex}.pdf"
    r2.upload_bytes(key, pdf, "application/pdf")
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = CustomerAgreement(onboarding_id=onboarding.id, proposal_id=proposal.id,
                                     agreement_storage_key=key, generated_at=now, generated_by=performed_by)
        db.add(existing)
        before = None
    else:
        before = {"agreement_storage_key": existing.agreement_storage_key, "proposal_id": existing.proposal_id}
        existing.agreement_storage_key, existing.proposal_id = key, proposal.id
        existing.generated_at, existing.generated_by = now, performed_by
    db.flush()
    db.add(AuditLog(entity_type="customer_agreement", entity_id=str(existing.id),
                    action="generated" if before is None else "regenerated", performed_by=performed_by,
                    before_value=before, after_value={"proposal_id": proposal.id, "agreement_storage_key": key}))
    return existing


def record_signed(db: Session, onboarding: CustomerOnboarding, performed_by: str, *, content: bytes,
                  content_type: str, filename: str, stamp_paper_number: str, stamp_paper_date: date,
                  today: date) -> CustomerAgreement:
    """Store the signed scan with the stamp paper's number and date, and move the
    project to `agreement_signed`. Caller commits."""
    from app.storage import r2

    agreement = get(db, onboarding)
    if agreement is None:
        raise AssociateError("Generate the agreement before uploading the signed copy.", code="not_generated",
                             status_code=409)
    if agreement.signed_scan_key:
        raise AssociateError("The signed agreement is already on file.", code="already_signed", status_code=409)
    number = (stamp_paper_number or "").strip()
    if not _STAMP_NUMBER_RE.fullmatch(number):
        raise AssociateError("Stamp paper number must be 1–50 letters, digits, spaces, '/' or '-'.",
                             code="invalid_stamp_paper_number", status_code=422, field="stamp_paper_number")
    if stamp_paper_date > today:
        raise AssociateError("Stamp paper date cannot be in the future.", code="invalid_stamp_paper_date",
                             status_code=422, field="stamp_paper_date")
    key = f"agreements/{onboarding.id}/signed/{uuid.uuid4().hex}_{filename}"
    r2.upload_bytes(key, content, content_type)
    agreement.signed_scan_key, agreement.signed_scan_filename = key, filename
    agreement.signed_scan_content_type = content_type
    agreement.stamp_paper_number, agreement.stamp_paper_date = number, stamp_paper_date
    agreement.signed_uploaded_at, agreement.signed_uploaded_by = datetime.now(timezone.utc), performed_by
    db.flush()
    evidence = {"agreement_id": agreement.id, "stamp_paper_number": number,
                "stamp_paper_date": stamp_paper_date.isoformat(), "signed_scan_filename": filename}
    db.add(AuditLog(entity_type="customer_agreement", entity_id=str(agreement.id), action="signed_uploaded",
                    performed_by=performed_by, after_value=evidence))
    progress.record(db, onboarding, ProjectStage.agreement_signed, performed_by, evidence=evidence)
    return agreement


def serialize(agreement: Optional[CustomerAgreement]) -> dict:
    if agreement is None:
        return {"generated": False, "signed": False}
    return {
        "generated": True,
        "agreement_id": agreement.id,
        "proposal_id": agreement.proposal_id,
        "generated_at": agreement.generated_at.isoformat(),
        "generated_by": agreement.generated_by,
        "signed": agreement.signed_scan_key is not None,
        "stamp_paper_number": agreement.stamp_paper_number,
        "stamp_paper_date": agreement.stamp_paper_date.isoformat() if agreement.stamp_paper_date else None,
        "signed_uploaded_at": agreement.signed_uploaded_at.isoformat() if agreement.signed_uploaded_at else None,
        "signed_uploaded_by": agreement.signed_uploaded_by,
        "signed_scan_filename": agreement.signed_scan_filename,
    }


def links(agreement: Optional[CustomerAgreement]) -> dict:
    """Short-lived signed links to the generated PDF and the signed scan."""
    from app.storage import r2

    out = {}
    if agreement is None:
        return out
    try:
        url, expires = r2.signed_get_url(agreement.agreement_storage_key, filename=f"agreement-{agreement.onboarding_id}.pdf")
        out["agreement_pdf"] = {"url": url, "expires_at": expires.isoformat()}
        if agreement.signed_scan_key:
            url, expires = r2.signed_get_url(agreement.signed_scan_key, filename=agreement.signed_scan_filename)
            out["signed_scan"] = {"url": url, "expires_at": expires.isoformat()}
    except r2.StorageNotConfiguredError:
        return {}
    return out
