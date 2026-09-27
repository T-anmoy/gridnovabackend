"""Lead scoring v2 for associate-sourced customers — a transparent, rule-based 0–100.

Every factor is stored with its weight, the fraction earned and a plain-language
detail, so anyone can see exactly why a customer scored what they did. Weights, band
cut-offs and response-time targets live in config/associate_onboarding.yaml (scoring).

    score = Σ weight_f × fraction_f            (fractions in [0, 1]; weights sum to 100)

Factors
  verification_call      1 if the staff verification call is done, else 0
  documents_complete     required documents present ÷ required documents
  subsidy_eligibility    criteria met ÷ criteria that apply: property in the customer's
                         name, connection in the customer's name, and — only when the
                         site assessment is linked to PM Surya Ghar — DCR panels
  system_fit             1, less `system_fit_penalty` for each capacity warning
                         (above sanctioned load; above consumption); `unknown_fraction`
                         when there is no requested capacity, or neither a sanctioned
                         load nor a consumption figure to compare it with
  customer_timeline      per-answer fraction from config (unsure / not given → its value)
  site_readiness         installation photo slots filled ÷ 7
  associate_track_record of the owner's OTHER onboardings that were submitted, the share
                         that went on to be verified; `new_associate_fraction` until they
                         have `track_record_min_history` such onboardings

Bands (config): Hot ≥ hot_min, Warm ≥ warm_min, else Cold. Each band has a response-time
target: the staff verification call is due that long after the associate submits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.memory.models import (
    INSTALLATION_PHOTO_SLOTS, CustomerOnboarding, GovernmentScheme, LeadScore, OnboardingStatus, ProposalInputs,
    ScoreBand, TerritoryDistrict,
)
from app.onboarding import config

BAND_TO_LEAD_SCORE = {ScoreBand.hot: LeadScore.high, ScoreBand.warm: LeadScore.medium, ScoreBand.cold: LeadScore.low}


def _factor(key: str, fraction: float, detail: str, weights: dict) -> dict:
    fraction = max(0.0, min(1.0, fraction))
    return {"factor": key, "weight": weights[key], "fraction": round(fraction, 4),
            "points": round(weights[key] * fraction, 2), "detail": detail}


def _track_record(db: Session, onboarding: CustomerOnboarding, cfg: dict) -> tuple[float, str]:
    reached = (OnboardingStatus.submitted, OnboardingStatus.verified, OnboardingStatus.closed)
    base = select(func.count(CustomerOnboarding.id)).where(
        CustomerOnboarding.owner_associate_id == onboarding.owner_associate_id,
        CustomerOnboarding.id != onboarding.id, CustomerOnboarding.status.in_(reached))
    submitted = db.execute(base).scalar_one()
    if submitted < cfg["track_record_min_history"]:
        return cfg["new_associate_fraction"], (
            f"{submitted} earlier submitted customer(s) — fewer than {cfg['track_record_min_history']}, "
            "so a neutral value is used")
    verified = db.execute(base.where(CustomerOnboarding.status == OnboardingStatus.verified)).scalar_one()
    return verified / submitted, f"{verified} of {submitted} earlier submitted customers were verified"


def compute(db: Session, onboarding: CustomerOnboarding) -> dict:
    """{score, band, factors} — pure computation, nothing written."""
    from app.onboarding import service  # avoid an import cycle

    cfg = config.load()["scoring"]
    weights = cfg["weights"]
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    factors = []

    done = onboarding.verification_call_completed
    factors.append(_factor("verification_call", 1.0 if done else 0.0,
                           "verification call completed" if done else "verification call not yet made", weights))

    checklist = service.checklist(db, onboarding)
    required = checklist["required"]
    present = len([k for k in required if k in checklist["present"]])
    factors.append(_factor("documents_complete", present / len(required) if required else 1.0,
                           f"{present} of {len(required)} required documents uploaded", weights))

    criteria = {"property in the customer's name": onboarding.property_in_customer_name,
                "connection in the customer's name": onboarding.connection_in_customer_name}
    if inputs.scheme_name == GovernmentScheme.pm_surya_ghar:
        criteria["DCR panels (PM Surya Ghar)"] = bool(inputs.dcr_panels_required)
    met = [k for k, ok in criteria.items() if ok]
    unmet = [k for k, ok in criteria.items() if not ok]
    factors.append(_factor("subsidy_eligibility", len(met) / len(criteria),
                           f"met: {', '.join(met) or 'none'}" + (f"; not met: {', '.join(unmet)}" if unmet else ""),
                           weights))

    district_code = db.get(TerritoryDistrict, onboarding.district_id).lgd_code
    capacity_warnings = [w for w in service.warnings_for(inputs, district_code)
                         if w["code"] in ("kw_exceeds_sanctioned_load", "kw_high_for_consumption")]
    comparable = inputs.desired_output_kw and (inputs.sanctioned_load_kw or inputs.avg_monthly_units_kwh
                                               or inputs.avg_monthly_bill_inr)
    if not comparable:
        fit, detail = cfg["unknown_fraction"], "not enough information to compare capacity with load or consumption"
    else:
        fit = 1.0 - cfg["system_fit_penalty"] * len(capacity_warnings)
        detail = ("capacity fits the load and consumption" if not capacity_warnings
                  else "; ".join(w["code"] for w in capacity_warnings))
    factors.append(_factor("system_fit", fit, detail, weights))

    timeline = onboarding.customer_timeline.value if onboarding.customer_timeline else "unsure"
    factors.append(_factor("customer_timeline", cfg["timeline_fractions"][timeline],
                           f"customer timeline: {timeline.replace('_', ' ')}", weights))

    photos = [s for s in INSTALLATION_PHOTO_SLOTS if (inputs.installation_photo_refs or {}).get(s, {}).get("storage_key")]
    factors.append(_factor("site_readiness", len(photos) / len(INSTALLATION_PHOTO_SLOTS),
                           f"{len(photos)} of {len(INSTALLATION_PHOTO_SLOTS)} site photos uploaded", weights))

    fraction, detail = _track_record(db, onboarding, cfg)
    factors.append(_factor("associate_track_record", fraction, detail, weights))

    score = int(round(sum(f["points"] for f in factors)))
    band = ScoreBand.hot if score >= cfg["bands"]["hot_min"] else (
        ScoreBand.warm if score >= cfg["bands"]["warm_min"] else ScoreBand.cold)
    return {"score": score, "band": band, "factors": factors}


def response_target(band: ScoreBand) -> timedelta:
    return timedelta(hours=config.load()["scoring"]["response_hours"][band.value])


def rescore(db: Session, onboarding: CustomerOnboarding, performed_by: str) -> dict:
    """Recompute and store the score; mirror the band onto the lead's existing
    high/medium/low score so every existing dashboard shows it; (re)set the response
    deadline while the customer waits for the verification call. Caller commits."""
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import AuditLog, Lead

    result = compute(db, onboarding)
    changed = (onboarding.score != result["score"] or onboarding.score_band != result["band"])
    onboarding.score = result["score"]
    onboarding.score_band = result["band"]
    onboarding.score_factors = result["factors"]
    onboarding.scored_at = datetime.now(timezone.utc)
    if onboarding.status == OnboardingStatus.submitted and onboarding.submitted_at is not None:
        submitted = onboarding.submitted_at if onboarding.submitted_at.tzinfo else \
            onboarding.submitted_at.replace(tzinfo=timezone.utc)
        onboarding.response_due_at = submitted + response_target(result["band"])
    elif onboarding.status != OnboardingStatus.submitted:
        onboarding.response_due_at = None
    lead = db.get(Lead, onboarding.lead_id)
    lead_score = BAND_TO_LEAD_SCORE[result["band"]]
    if lead.lead_score != lead_score:
        lead.lead_score = lead_score
    db.flush()
    if changed:
        db.add(AuditLog(entity_type="customer_onboarding", entity_id=str(onboarding.id), action="scored",
                        performed_by=performed_by,
                        after_value={"score": result["score"], "band": result["band"].value}))
    return result


def serialize(onboarding: CustomerOnboarding) -> Optional[dict]:
    if onboarding.score is None:
        return None
    due = onboarding.response_due_at
    return {"score": onboarding.score, "band": onboarding.score_band.value,
            "factors": onboarding.score_factors, "scored_at": onboarding.scored_at.isoformat(),
            "response_due_at": due.isoformat() if due else None,
            "escalation_level": onboarding.escalation_level}
