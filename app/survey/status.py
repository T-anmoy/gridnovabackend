"""Where an on-site survey stands: which sections are complete (config/site_survey.yaml
decides what each variant needs), the two signatures and the customer's declaration.
Computed from the saved site assessment — never stored."""
from __future__ import annotations

from typing import Any, Optional

from app.memory.models import CustomerOnboarding, ProposalInputs
from app.survey import config

PARTIES = ("customer", "associate")


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _plain(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True   # a list counts even when empty ("none"), as does False or 0


def sections(inputs: ProposalInputs) -> list[dict]:
    cfg = config.load()
    variant = _plain(inputs.survey_variant) or "individual_house"
    out = []
    for key, fields in config.required_fields(variant):
        fields = list(fields)
        for rule in cfg.get("conditional") or []:
            (when_field, when_value), = rule["when"].items()
            if rule["section"] == key and _plain(getattr(inputs, when_field)) == when_value:
                fields += [f for f in rule["require"] if f not in fields]
        missing = []
        extra = {}
        for f in fields:
            if f == "consumption_history":
                recorded = len(inputs.consumption_history or [])
                extra = {"months_recorded": recorded, "months_required": cfg["min_consumption_months"],
                         "months_requested": cfg["requested_consumption_months"]}
                if recorded < cfg["min_consumption_months"]:
                    missing.append(f)
            elif not _filled(getattr(inputs, f)):
                missing.append(f)
        out.append({"key": key, "complete": not missing, "missing": missing, **extra})
    return out


def _signature(ref: Optional[dict]) -> dict:
    return {"signed": bool(ref and ref.get("storage_key")), "signed_at": (ref or {}).get("signed_at")}


def signatures(inputs: ProposalInputs) -> dict:
    return {"customer": _signature(inputs.customer_signature_ref),
            "associate": _signature(inputs.associate_signature_ref)}


def unsigned(inputs: ProposalInputs) -> list[str]:
    sigs = signatures(inputs)
    return [p for p in PARTIES if not sigs[p]["signed"]]


def required_to_submit(onboarding: CustomerOnboarding) -> bool:
    return config.load()["survey_required_to_submit"] and not onboarding.survey_grandfathered


def submit_gaps(onboarding: CustomerOnboarding, inputs: ProposalInputs) -> dict:
    """{missing_survey_sections, survey_unsigned} for the submit gate — both empty when
    the survey is not required (config off, or a grandfathered customer)."""
    if not required_to_submit(onboarding):
        return {"missing_survey_sections": [], "survey_unsigned": []}
    return {"missing_survey_sections": [s["key"] for s in sections(inputs) if not s["complete"]],
            "survey_unsigned": unsigned(inputs)}


def summary(onboarding: CustomerOnboarding, inputs: ProposalInputs, *, staff: bool = False) -> dict:
    """GET …/survey."""
    secs = sections(inputs)
    review = {"outcome": _plain(inputs.feasibility_outcome), "notes": inputs.feasibility_notes,
              "conditions": inputs.feasibility_conditions or [], "revisit_items": inputs.revisit_items or [],
              "reviewed_at": _iso(inputs.feasibility_reviewed_at),
              "revised_after_review": bool(inputs.survey_revised_after_review)}
    if staff:
        review["reviewed_by"] = inputs.feasibility_reviewed_by_id
    return {
        "onboarding_id": onboarding.id,
        "variant": _plain(inputs.survey_variant),
        "sections": secs,
        "complete": all(s["complete"] for s in secs),
        "signatures": signatures(inputs),
        "declaration": {"name": inputs.customer_declaration_name,
                        "declared_at": _iso(inputs.customer_declared_at)},
        "required_to_submit": required_to_submit(onboarding),
        "grandfathered": bool(onboarding.survey_grandfathered),
        "review": review,
    }
