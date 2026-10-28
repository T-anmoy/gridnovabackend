"""Convert a Studio proposal into a lead (BE-18 §10), so the order-to-cash flows —
payments, projects, execution, accounts — work exactly as for a lead proposal.

One call creates, once: the pipeline Customer (matched by email or mobile), a Project, a
Lead (source proposal_studio, segment from the project type — left UNASSIGNED (BE-19): Sales
sees it through "assigned to you or nobody", and a `studio_converted` alert goes to the Sales
role; the Studio owner stays recorded on the Studio proposal and in its `converted` event)
and a Proposal on it carrying the same document, reference, PDF key and status (`accepted`
when the customer accepted, else `sent`). Never a second reference: the lead proposal reuses
the Studio version's. Idempotent: a second call returns the same lead.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import (
    LeadSource, PipelineStage, ProjectFamily, Proposal, ProposalStatus, StudioCustomer, StudioProposal, StudioProposalVersion,
    StudioStatus, TimelineEventType,
)
from app.proposals import document as d
from app.studio import service

SEGMENT = {"residential_rooftop": "residential", "housing_society": "residential", "commercial_rooftop": "commercial",
           "industrial_rooftop": "industrial", "ground_mounted": "commercial", "government_institutional": "government",
           "pm_kusum": "agricultural", "street_lighting": None, "generic": None}
FAMILY = {"ground_mounted": ProjectFamily.ground_mounted, "government_institutional": ProjectFamily.government_institutional,
          "street_lighting": ProjectFamily.street_lighting, "pm_kusum": ProjectFamily.water_pump}


def _source_version(db: Session, p: StudioProposal) -> StudioProposalVersion:
    """The accepted version, else the newest issued one."""
    issued = [v for v in service.versions(db, p) if v.issued_at is not None]
    if not issued:
        raise service.StudioError("not_issued", "Issue the proposal before creating a lead from it.")
    accepted = [v for v in issued if v.accepted_at is not None]
    return (accepted or issued)[-1]


def convert(db: Session, p: StudioProposal, actor: str) -> dict:
    from app.memory.memory_manager import MemoryManager
    if p.lead_id is not None:
        lead_proposal = db.execute(select(Proposal).where(Proposal.lead_id == p.lead_id)
                                   .order_by(Proposal.version.desc())).scalars().first()
        return {"lead_id": p.lead_id, "proposal_id": lead_proposal.id if lead_proposal else None, "created": False}
    v = _source_version(db, p)
    doc = service.doc_of(v)
    memory = MemoryManager(db)
    sc = db.get(StudioCustomer, p.customer_id)
    who = actor
    if sc.email:
        customer = memory.get_or_create_customer(sc.email, sc.name, performed_by=who, phone=sc.mobile)
    else:
        customer, _ = memory.get_or_create_customer_by_phone(sc.name, sc.mobile, who)
    cap = doc.plant.capacity_kwp
    project = memory.create_project(customer.id, performed_by=who, state=doc.site.state,
                                    project_family=FAMILY.get(p.project_type, ProjectFamily.rooftop_solar),
                                    capacity_kw=float(cap) if cap else None,
                                    location=", ".join(x for x in (doc.site.town, doc.site.state) if x) or None)
    lead = memory.create_lead(customer.id, performed_by=who, project_id=project.id, source=LeadSource.proposal_studio)
    lead.collected_fields = {k: v_ for k, v_ in {
        "name": sc.name, "phone": sc.mobile, "email": sc.email, "segment": SEGMENT.get(p.project_type),
        "project_type": "ground_mounted" if doc.mount_type == "ground_mounted" else "rooftop",
        "capacity_kw": float(cap) if cap else None, "location": doc.site.town,
        "studio_proposal": p.code}.items() if v_ is not None}
    accepted = v.accepted_at is not None
    content = {"document": d.dump(doc), "sections": [], "source": "proposal_studio", "studio_proposal_id": p.id,
               "studio_version": v.version}
    proposal = Proposal(lead_id=lead.id, version=1, status=ProposalStatus.accepted if accepted else ProposalStatus.sent,
                        content=content, pdf_storage_key=v.pdf_storage_key,
                        total_value=float(v.total) if v.total is not None else None,
                        date_created=service.now(), date_sent=v.issued_at, approved_at=v.approved_at or v.issued_at,
                        accepted_at=v.accepted_at, drafted_by=f"studio:{p.owner_id}",
                        document_reference=v.document_reference, document_date=v.document_date)
    db.add(proposal)
    db.flush()
    memory.add_timeline_event(lead_id=lead.id, event_type=TimelineEventType.proposal_sent, created_by=who,
                              description=f"Created from Proposal Studio {p.code} ({v.document_reference})")
    memory.advance_lead_stage(lead.id, PipelineStage.won if accepted else PipelineStage.proposal_sent,
                              performed_by=who, description=f"Proposal Studio {p.code}")
    p.lead_id = lead.id
    p.status = StudioStatus.converted
    p.updated_at = service.now()
    service.event(db, p, "converted", version=v.version, actor=actor,
                  detail={"lead_id": lead.id, "proposal_id": proposal.id, "reference": v.document_reference,
                          "status": ProposalStatus(proposal.status).value, "owner_id": p.owner_id,
                          "lead_assigned_to": None})
    db.flush()
    names = service.owner_names(db, [p.owner_id])
    service._notify_after_commit(db, "studio_converted", {
        "proposal_id": p.id, "code": p.code, "title": p.title, "reference": v.document_reference,
        "lead_id": lead.id, "customer_name": sc.name, "accepted": accepted, "owner_id": p.owner_id,
        "owner_name": names.get(p.owner_id)})
    return {"lead_id": lead.id, "proposal_id": proposal.id, "created": True}


def mark_lead_accepted(db: Session, p: StudioProposal, v: StudioProposalVersion) -> Optional[Proposal]:
    """A customer accepted a converted proposal on the Studio page: the lead's proposal
    becomes accepted too, so payments can schedule against it."""
    from app.memory.memory_manager import MemoryManager
    proposal = db.execute(select(Proposal).where(Proposal.lead_id == p.lead_id,
                                                 Proposal.document_reference == v.document_reference)).scalar_one_or_none()
    if proposal is None or proposal.accepted_at is not None:
        return proposal
    memory = MemoryManager(db)
    memory.mark_customer_accepted(proposal.id, when=v.accepted_at, via=f"Proposal Studio ({v.accepted_via})",
                                  recorded_by="customer" if v.accepted_via == "online" else "studio")
    memory.advance_lead_stage(p.lead_id, PipelineStage.won, performed_by="customer",
                              description=f"Accepted Proposal Studio {p.code}")
    return proposal
