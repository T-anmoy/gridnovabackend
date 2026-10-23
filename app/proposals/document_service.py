"""The proposal document in the proposal's life (BE-13): attached when a version is
drafted, edited by staff while it is a draft, checked before approval and sending, and
read by the dashboard, the preview and the accept page.

    attach()        every draft path (staff draft, Proposal Agent, associate formal
                    proposal) goes through ProposalAgent.draft_proposal, which calls this.
    edit()          one section, by a role config/proposal_document.yaml allows, while the
                    proposal is a draft; stale edits get 409; every field change audited;
                    a change that moves the price re-evaluates the approval tier.
    problem()       the 409 body that stops approval and sending while required data is
                    missing (code missing_required).
    view()          what the dashboard shows: document, figures, missing, editable
                    sections for this viewer; Engineers see the technical part only.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import AuditLog, Lead, Proposal, ProposalStatus
from app.proposals import document as d
from app.proposals import document_config

# A version can be changed only before it is approved; after that, request-revision.
EDITABLE_STATUSES = (ProposalStatus.draft, ProposalStatus.pending_pricing_validation, ProposalStatus.pending_approval)
# Sections of the generic composed content that the document's fixed text replaces
# (PRO-1, PRO-2, PRO-3, PRO-4, PRO-5, PRO-6, PRO-9): dropped from a documented proposal's
# `sections`, so no superseded placeholder copy sits beside the real document.
REPLACED_SECTIONS = ("cover_letter_intro", "standard_terms", "closing", "residential_rooftop_scope",
                     "commercial_rooftop_scope", "industrial_rooftop_scope", "pm_surya_ghar_subsidy_note")
TECHNICAL_SECTIONS = ("system", "boq")


def _no_price_body() -> str:
    from app.proposals import content_engine
    return content_engine.PLACEHOLDER_MARKER + ": Price to be confirmed by Grid Nova.]"


def _previous_document(db: Session, lead_id: int) -> Optional[d.ProposalDocument]:
    latest = db.execute(select(Proposal).where(Proposal.lead_id == lead_id).order_by(Proposal.version.desc())
                        .limit(1)).scalar_one_or_none()
    return d.stored(latest.content) if latest is not None else None


def attach(db: Session, lead, content: dict, list_ctx: Optional[dict]) -> tuple[dict, Optional[float]]:
    """The new version's content with its document, and the total_value it implies (the
    GST-inclusive total, or None while there is no price). A revision starts from the
    previous version's document, keeping staff edits."""
    fresh = d.build(db, lead, list_ctx)
    tier_total = (content.get("pricing") or {}).get("total_value")
    if fresh.price.mode == "none" and tier_total:
        # A configured per-kW tier rate (proposal_content.yaml pricing, outside the price
        # list) priced it: keep that total exactly, as before documents existed.
        fresh.price = d.Price(mode="staff_total", total=d.money(tier_total))
    previous = _previous_document(db, lead.id)
    doc = d.carry_forward(previous, fresh) if previous is not None else fresh
    out = _sync_coverage(d.with_document(content, doc), doc)
    out["sections"] = [s for s in out.get("sections") or [] if s.get("id") not in REPLACED_SECTIONS]
    total = d.figures(doc)["total"]
    total_value = float(total) if total is not None else None
    return _sync_sections(out, total_value), total_value


def _sync_coverage(content: dict, doc: d.ProposalDocument) -> dict:
    """The generic engine matches rules on the lead's `segment` only; the document also
    reads the site assessment's site type and the consumer category staff choose. When
    the document covers the segment, the engine's generic fallback scope is not part of
    the proposal and it needs no manual-review flag; when it does not, both stay."""
    fallback = doc.coverage == "fallback"
    if not fallback:
        content["sections"] = [s for s in content.get("sections") or [] if s.get("id") != "fallback_generic_scope"]
    content["used_fallback_template"] = fallback
    return content


def _sync_sections(content: dict, total_value: Optional[float]) -> dict:
    from app.proposals import content_engine
    if total_value:
        return content_engine.with_adjusted_price(content, total_value) if _priced_by_hand(content) else \
            _pricing_body(content, total_value)
    for section in content.get("sections") or []:
        if section.get("id") == "pricing":
            section["body"] = _no_price_body()
            section["total_value"] = None
    if isinstance(content.get("pricing"), dict):
        content["pricing"]["total_value"] = None
    return content


def _priced_by_hand(content: dict) -> bool:
    doc = d.stored(content)
    return doc is not None and doc.price.mode in ("staff_basic", "staff_total")


def _pricing_body(content: dict, total_value: float) -> dict:
    from app.pricing.price_list import document_amount
    from app.proposals import content_engine
    for section in content.get("sections") or []:
        if section.get("id") == "pricing" and content_engine.PLACEHOLDER_MARKER in str(section.get("body")):
            section["body"] = f"Proposed project value: {document_amount(total_value)}."
            section["total_value"] = total_value
    if isinstance(content.get("pricing"), dict):
        content["pricing"]["total_value"] = total_value
    return content


def problem(proposal: Proposal) -> Optional[dict]:
    """None when the documented proposal may be approved / sent; otherwise the 409 body.
    None too for a proposal drafted before BE-13 (it has no document)."""
    doc = d.stored(proposal.content)
    if doc is None:
        return None
    missing = d.missing_required(doc)
    if not missing:
        return None
    return {"code": "missing_required",
            "message": "This proposal cannot be approved or sent yet; it still needs: "
                       + "; ".join(m["message"] for m in missing) + ".",
            "missing": missing}


def editable_sections(role: str) -> list[str]:
    return [s for s in document_config.EDITABLE_SECTIONS if role in document_config.edit_roles(s)]


def view(proposal: Proposal, role: str) -> dict:
    doc = d.stored(proposal.content)
    if doc is None:
        return {"proposal_id": proposal.id, "has_document": False}
    locked = ProposalStatus(proposal.status) not in EDITABLE_STATUSES
    sections = [] if locked else editable_sections(role)
    base = {"proposal_id": proposal.id, "has_document": True, "version": proposal.version,
            "status": ProposalStatus(proposal.status).value, "revision": doc.revision, "layout": d.layout(doc),
            "locked": locked, "editable_sections": sections}
    if role == "engineer":
        # Technical part only — no customer, price or contact (the Engineer surface).
        return {**base, "document": {"view": "technical", "revision": doc.revision,
                                     "plant": {"capacity_kwp": d.kw_text(doc.plant.capacity_kwp)
                                                                         or None, "dcr": doc.plant.dcr},
                                     "system": doc.system.model_dump(mode="json"),
                                     "boq": [r.model_dump(mode="json") for r in doc.boq]}}
    figs = d.figures(doc)
    return {**base, "document": d.dump(doc), "figures": figs, "missing_required": d.missing_required(doc, figs),
            "document_reference": proposal.document_reference,
            "document_date": proposal.document_date.isoformat() if proposal.document_date else None,
            "signatory": document_config.load()["signatory"]}


def lock_proposal(db: Session, proposal_id: int) -> Optional[Proposal]:
    """The proposal re-read under a row lock (PostgreSQL), so a concurrent edit waits and
    then sees the revision this one wrote."""
    return db.execute(select(Proposal).where(Proposal.id == proposal_id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def edit(db: Session, proposal: Proposal, *, role: str, employee_id: str, section: str, value,
         base_revision: int) -> dict:
    """Apply one section edit. Raises d.DocumentError (403 field_not_editable, 409
    proposal_locked / stale_version, 422 invalid_value / payment_terms_total /
    make_not_in_catalogue). Caller commits."""
    doc = d.stored(proposal.content)
    if doc is None:
        raise d.DocumentError("no_document", "This proposal was drafted before structured documents; draft a new "
                                             "version to edit it.", status_code=409)
    if ProposalStatus(proposal.status) not in EDITABLE_STATUSES:
        raise d.DocumentError("proposal_locked", f"This proposal is '{ProposalStatus(proposal.status).value}'; an "
                                                 "approved or sent version cannot change. Request a revision.",
                              status_code=409)
    if section not in document_config.EDITABLE_SECTIONS or role not in document_config.edit_roles(section):
        raise d.DocumentError("field_not_editable", f"Your role cannot change '{section}'.", field=section,
                              status_code=403)
    if base_revision != doc.revision:
        raise d.DocumentError("stale_version", "Someone changed this proposal after you opened it. Reload and "
                                               "apply your change again.", status_code=409,
                              extra={"current_revision": doc.revision})
    before_total = d.figures(doc)["total"]
    new, changes, regenerated = d.apply_edit(db, doc, section, value)
    figs = d.figures(new)
    content = d.with_document(proposal.content, new)
    price_changed = figs["total"] != before_total
    tier = None
    if price_changed or section == "plant":
        tier = _reprice(db, proposal, new, figs, employee_id)
    content = _sync_coverage(content, new)
    proposal.used_fallback_template = content["used_fallback_template"]
    proposal.content = _sync_sections(content, float(figs["total"]) if figs["total"] is not None else None)
    db.add(AuditLog(entity_type="proposal", entity_id=str(proposal.id), action="document_edited",
                    performed_by=employee_id,
                    before_value={"revision": doc.revision, "section": section,
                                  "fields": {c["field"]: c["before"] for c in changes}},
                    after_value={"revision": new.revision, "section": section,
                                 "fields": {c["field"]: c["after"] for c in changes},
                                 "regenerated_sections": regenerated,
                                 "total": figs["total"], "requires_manager_signoff": proposal.requires_manager_signoff}))
    db.flush()
    return {"changes": changes, "regenerated_sections": regenerated, "tier_changed": tier}


def _reprice(db: Session, proposal: Proposal, doc: d.ProposalDocument, figs: dict, employee_id: str) -> Optional[bool]:
    """total_value follows the document; the manager-signoff flag is recomputed by the same
    rule as at draft time (capacity and value). Returns the new flag when it changed."""
    from app.rules.business_rules import evaluate_lead

    lead = db.get(Lead, proposal.lead_id)
    total = float(figs["total"]) if figs["total"] is not None else None
    collected = dict(lead.collected_fields or {}) if lead else {}
    if doc.plant.capacity_kwp is not None:
        collected["capacity_kw"] = float(doc.plant.capacity_kwp)
    flag = evaluate_lead(collected, total_value=total).requires_manager_signoff
    before = (proposal.total_value, proposal.requires_manager_signoff)
    proposal.total_value = total
    changed = flag != proposal.requires_manager_signoff
    proposal.requires_manager_signoff = flag
    if before != (total, flag):
        db.add(AuditLog(entity_type="proposal", entity_id=str(proposal.id), action="document_repriced",
                        performed_by=employee_id,
                        before_value={"total_value": before[0], "requires_manager_signoff": before[1]},
                        after_value={"total_value": total, "requires_manager_signoff": flag}))
    if changed and flag:
        from app.memory.memory_manager import MemoryManager
        from app.notifications.notifier import notifier
        proposal_id, lead_id = proposal.id, proposal.lead_id
        MemoryManager(db).notify_after_commit(
            lambda: notifier.manager_signoff_required(proposal_id=proposal_id, lead_id=lead_id, total_value=total),
            "manager_signoff_required")
    return flag if changed else None


def priced_content(proposal: Proposal, *, basic: Optional[Decimal], rate_per_kwp: Optional[Decimal],
                   total: Optional[Decimal]) -> tuple[Optional[dict], Optional[float]]:
    """adjust-pricing on a documented proposal: (new content, new total_value). (None, None)
    when the proposal has no document (the original route behaviour applies)."""
    doc = d.stored(proposal.content)
    if doc is None:
        return None, None
    new = d.set_price(doc, basic=basic, rate_per_kwp=rate_per_kwp, total=total)
    figs = d.figures(new)
    if figs["total"] is None and rate_per_kwp is not None:
        raise d.DocumentError("missing_required", "A rate per kWp needs the plant capacity first.",
                              field="plant.capacity_kwp", status_code=409)
    total_value = float(figs["total"]) if figs["total"] is not None else 0.0
    return d.with_document(proposal.content, new), total_value


def summary_for_customer(proposal: Proposal) -> Optional[dict]:
    """The accept page's view of a documented proposal — the same figures the PDF prints."""
    doc = d.stored(proposal.content)
    if doc is None:
        return None
    from app.proposals.document_render import context
    figs = d.figures(doc)
    ctx = context(doc, figs)
    return {"layout": figs["layout"], "capacity_kwp": figs["capacity_kwp"], "reference": proposal.document_reference,
            "date": proposal.document_date.isoformat() if proposal.document_date else None,
            "basic": figs["basic"], "gst": figs["gst"], "total": figs["total"], "words": figs["words"]["total"],
            "payments": figs["payments"], "subsidy": figs["subsidy"], "generation": figs["generation"],
            "customer_scope": [ctx.fill(b) for b in doc.customer_scope]}
