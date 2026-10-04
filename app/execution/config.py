"""config/execution_payments.yaml — loaded once, validated at startup (app.main).

Values Grid Nova has not supplied are dummies registered in the placeholder registry
(config/placeholder_registry.yaml); this module only checks that the file is usable.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

import yaml

from app.memory.models import ConnectionType, ProjectStage

_PATH = Path(__file__).resolve().parents[2] / "config" / "execution_payments.yaml"

EXECUTION_STAGES = (
    ProjectStage.feasibility_submitted, ProjectStage.material_dispatched, ProjectStage.installed,
    ProjectStage.inspection_net_meter, ProjectStage.commissioned, ProjectStage.subsidy_claimed,
    ProjectStage.subsidy_received, ProjectStage.handover_complete,
)
EVIDENCE_KEYS = ("text", "documents", "optional_documents", "photos", "register_verified")
PAYMENT_MODES = ("cheque", "demand_draft", "neft", "rtgs", "imps", "upi")
REGISTER_SANITY_KEYS = ("module_wp_min", "module_wp_max", "inverter_kw_min", "inverter_kw_max", "warranty_max_years",
                        "capacity_tolerance_percent", "serial_min_length", "serial_max_length", "bulk_max_rows")


class ExecutionConfigError(RuntimeError):
    pass


def _validate(data: dict) -> dict:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise ExecutionConfigError(f"{_PATH.name}: {message}")

    stage_names = {s.value for s in EXECUTION_STAGES}
    ex = data.get("execution") or {}
    app = (ex.get("stages") or {}).get("applicability") or {}
    for key in ("grid_connected_only", "pm_surya_ghar_only"):
        need(set(app.get(key) or []) <= stage_names, f"execution.stages.applicability.{key} must name execution stages")
    need(set(app.get("grid_connected_types") or []) <= {c.value for c in ConnectionType},
         "execution.stages.applicability.grid_connected_types must be connection types")
    stuck = ex.get("stuck_threshold_days") or {}
    need(set(stuck) <= stage_names and all(isinstance(v, int) and v > 0 for v in stuck.values()),
         "execution.stuck_threshold_days must map execution stages to positive whole days")
    slots = ex.get("photo_slots") or []
    docs = ex.get("document_types") or []
    need(slots and len(set(slots)) == len(slots), "execution.photo_slots must list unique slots")
    need(docs and len(set(docs)) == len(docs), "execution.document_types must list unique types")
    evidence = ex.get("evidence") or {}
    need(set(evidence) == stage_names, "execution.evidence must describe every execution stage")
    for stage, rule in evidence.items():
        need(set(rule) <= set(EVIDENCE_KEYS), f"execution.evidence.{stage} keys must be among {EVIDENCE_KEYS}")
        need(set(rule.get("photos") or []) <= set(slots), f"execution.evidence.{stage}.photos must be photo_slots")
        need(set((rule.get("documents") or []) + (rule.get("optional_documents") or [])) <= set(docs),
             f"execution.evidence.{stage} documents must be document_types")
    gates = ex.get("gates") or {}
    for name in ("material_dispatched", "commissioned"):
        g = gates.get(name) or {}
        need(isinstance(g.get("enabled"), bool) and isinstance(g.get("through_milestone"), int),
             f"execution.gates.{name} needs enabled (true/false) and through_milestone (a number)")
    gate = gates["material_dispatched"]
    policy = ex.get("photo_policy") or {}
    need(isinstance(policy.get("allow_gallery"), bool) and isinstance(policy.get("gallery_counts_fully"), bool)
         and isinstance(policy.get("exif_max_age_days"), int) and policy["exif_max_age_days"] > 0,
         "execution.photo_policy needs allow_gallery, gallery_counts_fully and a positive exif_max_age_days")
    sanity = (ex.get("register") or {}).get("sanity") or {}
    need(set(sanity) == set(REGISTER_SANITY_KEYS) and all(isinstance(v, (int, float)) and v > 0
                                                          for v in sanity.values()),
         f"execution.register.sanity must give positive {REGISTER_SANITY_KEYS}")

    pay = data.get("payments") or {}
    template = pay.get("schedule_template") or []
    need(template, "payments.schedule_template must list the milestones")
    need([m.get("number") for m in template] == list(range(1, len(template) + 1)),
         "payments.schedule_template numbers must run 1, 2, 3…")
    try:
        percents = [Decimal(str(m.get("percent"))) for m in template]
    except InvalidOperation:
        raise ExecutionConfigError(f"{_PATH.name}: payments.schedule_template percents must be numbers")
    need(all(p > 0 for p in percents) and sum(percents) == 100, "payments.schedule_template percents must sum to 100")
    all_stages = {s.value for s in ProjectStage}
    def _trigger_ok(trigger) -> bool:
        stages = trigger if isinstance(trigger, list) else [trigger]
        return bool(stages) and all(t in all_stages for t in stages)
    need(all(_trigger_ok(m.get("trigger")) and m.get("label") for m in template),
         "each payments.schedule_template milestone needs a label and a trigger stage (or a list of stages)")
    for name in ("material_dispatched", "commissioned"):
        need(gates[name]["through_milestone"] in range(1, len(template) + 1),
             f"execution.gates.{name}.through_milestone must be a milestone number")
    modes = pay.get("enabled_modes") or []
    need(modes and set(modes) <= set(PAYMENT_MODES), f"payments.enabled_modes must be among {PAYMENT_MODES} (no cash)")
    th = pay.get("thresholds") or {}
    for key in ("awaiting_verification_hours", "cheque_hold_days", "milestone_overdue_days"):
        need(isinstance(th.get(key), int) and th[key] > 0, f"payments.thresholds.{key} must be a positive whole number")
    try:
        need(Decimal(str(th.get("overpayment_tolerance_inr"))) >= 0, "payments.thresholds.overpayment_tolerance_inr >= 0")
    except InvalidOperation:
        raise ExecutionConfigError(f"{_PATH.name}: payments.thresholds.overpayment_tolerance_inr must be a number")
    need(isinstance(pay.get("proof_required_for_associates"), bool), "payments.proof_required_for_associates true/false")
    receipt = pay.get("receipt") or {}
    need("{SEQ" in str(receipt.get("numbering")), "payments.receipt.numbering must include {SEQ…}")
    company = receipt.get("company") or {}
    for key in ("legal_name", "address", "phone", "email", "gstin", "bank_name", "account_number", "ifsc"):
        need(company.get(key), f"payments.receipt.company.{key} is required")
    wording = receipt.get("wording") or {}
    need(wording.get("heading") and wording.get("footer"), "payments.receipt.wording needs heading and footer")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_PATH, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def execution() -> dict:
    return load()["execution"]


def payments() -> dict:
    return load()["payments"]


def evidence_rule(stage: ProjectStage) -> dict:
    return execution()["evidence"][stage.value]


def photo_slots() -> list[str]:
    return list(execution()["photo_slots"])


def document_types() -> list[str]:
    return list(execution()["document_types"])


def photo_policy() -> dict:
    return execution()["photo_policy"]
