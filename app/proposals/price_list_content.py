"""Formal-proposal content from Grid Nova's price list and master data (PM-A).

`context_for_lead` decides whether a lead's proposal can be list-priced: the same
applicability as the calculator (app/pricing/list_pricing.py — West Bengal, residential,
grid-connected without battery, known phase) plus what a FORMAL proposal needs on top:
ONE package (Nova Assured or Nova Signature from the onboarding's equipment package —
"as recommended" or none means the price stays to be confirmed; policy
as_recommended_package_in_proposal) and a requested size that is a whole kW in the
phase's range (never rounded). The facts come from the lead's site assessment
(ProposalInputs); a lead without one is never list-priced.

`sections` returns the deterministic sections, wording from config/price_list.yaml:
equipment makes for the chosen package ("or equivalent", never models), the common
electrical specification ONLY for systems up to 5 kW (omitted above — no substitute
text), payment terms (labels verbatim), standard warranty, O&M and panel life span
(labelled as a life span), and — when list-priced — the destination and the
price-change clause.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.pricing import list_pricing, price_list

ELECTRICAL_SPEC_MAX_KW = 5
_COMPONENT_LABELS = {"solar_panel": "Solar panels", "inverter": "Inverter", "cables": "Cables"}
_SPEC_LABELS = {"ac_cable": "AC cable", "dc_cable": "DC cable", "earthing_cable": "Earthing cable",
                "conduit_pipe": "Conduit pipe", "flex_pipe": "Flex pipe"}
SEGMENT_FROM_SITE_TYPE = {"rooftop_residential": "residential", "rooftop_commercial": "commercial"}


def _inputs_for_lead(db: Session, lead_id: int):
    from app.memory.models import ProposalInputs
    return db.execute(select(ProposalInputs).where(ProposalInputs.lead_id == lead_id)
                      .order_by(ProposalInputs.id.desc())).scalars().first()


def _val(v):
    return v.value if hasattr(v, "value") else v


def context_for_lead(db: Session, lead) -> Optional[dict]:
    """None when the lead has no site assessment; otherwise
    {status, reasons, package, phase, kw, quote}."""
    inputs = _inputs_for_lead(db, lead.id)
    if inputs is None:
        return None
    collected = dict(lead.collected_fields or {})
    segment = collected.get("segment") or SEGMENT_FROM_SITE_TYPE.get(_val(inputs.site_type))
    phase_value = _val(inputs.connection_phase)
    reasons = list_pricing.applicability_reasons(
        state=inputs.state, segment=segment, is_housing_society=False,
        connection_type=_val(inputs.connection_type), battery_storage=bool(inputs.battery_storage),
        connection_phase=phase_value)
    package = price_list.PACKAGE_FOR_TIER.get(_val(inputs.equipment_tier))
    if package is None:
        reasons.append("package_not_chosen")
    kw = inputs.desired_output_kw or collected.get("capacity_kw")
    phase = list_pricing.phase_of(phase_value)
    quote = None
    if kw in (None, "", 0):
        reasons.append("size_unknown")
    elif phase is not None:
        q = price_list.quote(package or "NOVA_ASSURED", phase, kw)
        if isinstance(q, price_list.Unsupported):
            reasons.append(q.reason)
        elif package is not None:
            quote = q
    status = "list_price" if quote is not None and not reasons else "custom_quote"
    return {"status": status, "reasons": reasons if status == "custom_quote" else [], "package": package,
            "phase": phase, "kw": kw, "quote": quote if status == "list_price" else None}


def pricing_body(quote: price_list.PriceQuote) -> str:
    rule = price_list.gst_rule()
    phase = "single-phase" if quote.phase == "SINGLE" else "three-phase"
    return (f"{price_list.display_name(quote.package)} — {quote.kw} kW {phase} grid-connected rooftop solar system.\n"
            f"Basic price: {price_list.document_amount(quote.basic)}\n"
            f"GST ({rule['description'].removeprefix('GST at ').rstrip('.')}): "
            f"{price_list.document_amount(quote.gst)}\n"
            f"Final price (including GST): {price_list.document_amount(quote.final)}")


def pricing_fields(quote: price_list.PriceQuote) -> dict:
    return {"price_status": "list_price", "package": quote.package, "package_name": price_list.display_name(quote.package),
            "phase": quote.phase, "kw": quote.kw, **quote.amounts(), "gst_rule": price_list.gst_rule(),
            "price_list_version": quote.price_list_version, "source_sha256": price_list.source_sha256(),
            "destination": price_list.destination(), "custom_quote_reasons": []}


def _makes(spec: dict) -> str:
    makes = " / ".join(spec.get("makes") or [])
    return f"{makes} or equivalent" if spec.get("or_equivalent") else makes


def sections(*, package: Optional[str], kw, list_priced: bool) -> list[dict]:
    data = price_list.load()
    out = []
    if package:
        lines = []
        for component, spec in data["equipment"][package].items():
            line = f"{_COMPONENT_LABELS.get(component, component)}: {_makes(spec)}"
            if spec.get("technology"):
                line += f" ({', '.join(spec['technology'])})"
            lines.append(line)
        out.append({"id": "equipment_makes", "category": "note",
                    "body": f"{price_list.display_name(package)} equipment:\n" + "\n".join(lines)})
    try:
        size = float(kw) if kw not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    if size is not None and size <= ELECTRICAL_SPEC_MAX_KW:
        lines = []
        for item, spec in data["electrical_spec_up_to_5kw"].items():
            makes = " / ".join(spec.get("makes") or [])
            lines.append(f"{_SPEC_LABELS.get(item, item)}: {spec['spec']}" + (f" — {makes}" if makes else ""))
        out.append({"id": "electrical_specification", "category": "note", "body": "\n".join(lines)})
    terms = data["commercial_terms"]
    out.append({"id": "payment_terms", "category": "terms",
                "body": "\n".join(f"{m['percent']}% {m['label']}" for m in terms["payment_schedule"])})
    out.append({"id": "warranty_and_service", "category": "terms",
                "body": f"Standard warranty: {terms['standard_warranty']}.\n"
                        f"Operation and maintenance: {terms['o_and_m']}.\n"
                        f"Solar panel life span: {terms['solar_panel_life_span']} (a life span, not a warranty)."})
    if list_priced:
        out.append({"id": "price_terms", "category": "terms",
                    "body": f"Prices for destination within {data['destination']}.\n{data['price_change_clause']}."})
    return out
