"""
Parameterized proposal composition engine.

Grid Nova staff drive proposal content entirely from ``config/proposal_content.yaml``:
reusable section blocks, composable parameter rules, and capacity-tiered pricing.
This module loads and validates that config, and turns a lead's collected fields into
a fully composed proposal ``content`` dict.

Design principles carried over from the rest of the platform:
- The AI never invents facts here — section selection and pricing are deterministic,
  zero-LLM functions driven by the YAML.
- Fail loudly: an invalid config (overlapping pricing tiers, a rule referencing a
  missing section) raises at load time so the app refuses to boot rather than draft a
  broken proposal.
- Never silently ship an empty-scope proposal: an unmatched combination falls back to a
  generic scope block and flags the proposal for manual review.
"""
from __future__ import annotations

import copy
import logging
import threading
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = "config/proposal_content.yaml"

# Fixed document order for composed sections. Every contributed section id is placed
# into one of these categories; scope always precedes notes, notes precede pricing, etc.
_CATEGORY_ORDER = ("intro", "scope", "note", "pricing", "terms", "closing")

_CONTENT_CONFIG: Optional[dict] = None
_lock = threading.Lock()


class ProposalConfigError(RuntimeError):
    """Raised when config/proposal_content.yaml is structurally invalid."""


# ── Loading + validation ────────────────────────────────────────────────────────

def load_content_config(force_reload: bool = False) -> dict:
    """Lazy, process-lifetime, thread-safe load of the proposal content config.

    Validates on first load (and on every forced reload) so a broken config surfaces
    immediately rather than mid-request.
    """
    global _CONTENT_CONFIG
    with _lock:
        if _CONTENT_CONFIG is None or force_reload:
            with open(_CONFIG_PATH, "r") as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                raise ProposalConfigError(f"{_CONFIG_PATH} did not parse to a mapping.")
            validate_content_config(cfg)
            _CONTENT_CONFIG = cfg
    return _CONTENT_CONFIG


def _tier_bounds(tier: dict) -> tuple[float, float]:
    """Return (min, max) for a tier; a null/absent max means unbounded (+inf)."""
    lo = tier.get("capacity_min_kw", 0) or 0
    hi = tier.get("capacity_max_kw", None)
    hi = float("inf") if hi is None else hi
    return float(lo), float(hi)


def validate_content_config(cfg: dict) -> None:
    """Raise ProposalConfigError on structural problems. See module docstring."""
    library = cfg.get("sections_library") or {}
    if not isinstance(library, dict):
        raise ProposalConfigError("sections_library must be a mapping of id -> text.")

    # (b) Every id referenced by a rule's `contributes` must exist in sections_library.
    rules = cfg.get("parameter_rules") or []
    for rule in rules:
        rule_id = rule.get("id", "<unnamed>")
        for section_id in rule.get("contributes", []):
            if section_id not in library:
                raise ProposalConfigError(
                    f"parameter_rules[{rule_id}] contributes unknown section id "
                    f"'{section_id}' — not found in sections_library."
                )

    # The fallback scope must exist — it is the safety net for unmatched leads.
    if "fallback_generic_scope" not in library:
        raise ProposalConfigError(
            "sections_library is missing required id 'fallback_generic_scope'."
        )

    # (a) No two pricing tiers for the same project_type may have overlapping ranges.
    tiers = (cfg.get("pricing") or {}).get("tiers") or []
    by_type: dict[str, list[dict]] = {}
    for tier in tiers:
        by_type.setdefault(tier.get("project_type"), []).append(tier)
    for project_type, group in by_type.items():
        ordered = sorted(group, key=lambda t: _tier_bounds(t)[0])
        for a, b in zip(ordered, ordered[1:]):
            a_lo, a_hi = _tier_bounds(a)
            b_lo, b_hi = _tier_bounds(b)
            # Ranges are [min, max); they overlap if the next one starts before the
            # previous one ends.
            if b_lo < a_hi:
                raise ProposalConfigError(
                    f"Overlapping pricing tiers for project_type '{project_type}': "
                    f"[{a_lo:g}, {a_hi:g}) and [{b_lo:g}, {b_hi:g}) overlap."
                )

    # Placeholder pricing (rate_per_kw == 0) is structurally valid — the app still
    # boots — but every proposal it produces will price at ₹0 until Grid Nova staff
    # fill in real rates. That's easy to miss since nothing else fails loudly about
    # it after boot, so flag it clearly, once, right when it matters most.
    placeholder_tiers = [t for t in tiers if not t.get("rate_per_kw")]
    if placeholder_tiers:
        described = ", ".join(
            f"{t.get('project_type')} [{_tier_bounds(t)[0]:g}, "
            f"{'∞' if _tier_bounds(t)[1] == float('inf') else f'{_tier_bounds(t)[1]:g}'})"
            for t in placeholder_tiers
        )
        logger.warning(
            "%d of %d pricing tier(s) in %s still have rate_per_kw = 0 (PLACEHOLDER) — "
            "every proposal matching one of these tiers will price at ₹0 until real "
            "rates are entered. Affected tiers: %s",
            len(placeholder_tiers), len(tiers), _CONFIG_PATH, described,
        )


# ── Selection ────────────────────────────────────────────────────────────────────

def _coerce_capacity(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _rule_matches(rule: dict, collected: dict) -> bool:
    """A rule matches only if EVERY key in its `match` block matches the lead.

    String keys are exact matches; `capacity_kw_min` means capacity_kw >= value.
    """
    match = rule.get("match", {})
    for key, expected in match.items():
        if key == "capacity_kw_min":
            capacity = _coerce_capacity(collected.get("capacity_kw"))
            if capacity is None or capacity < float(expected):
                return False
        else:
            if collected.get(key) != expected:
                return False
    return True


def select_sections(collected: dict, cfg: dict) -> tuple[list[str], bool]:
    """Evaluate every parameter rule; union the `contributes` of all matching rules
    in rule-definition order, deduplicated.

    Returns (section_ids, used_fallback). If no rule matched at all, returns the
    fallback generic scope and used_fallback=True (never an empty scope).
    """
    rules = cfg.get("parameter_rules") or []
    section_ids: list[str] = []
    for rule in rules:
        if _rule_matches(rule, collected):
            for section_id in rule.get("contributes", []):
                if section_id not in section_ids:
                    section_ids.append(section_id)

    if not section_ids:
        return ["fallback_generic_scope"], True
    return section_ids, False


def select_pricing(
    project_type: Optional[str],
    capacity_kw,
    segment: Optional[str],
    cfg: dict,
) -> Optional[float]:
    """First pricing tier whose project_type matches and whose [min, max) range
    contains capacity_kw, with the matching segment modifier applied (default 1.0).

    Returns the computed rate_per_kw, or None if nothing matches (never guesses).
    """
    capacity = _coerce_capacity(capacity_kw)
    if project_type is None or capacity is None:
        return None

    pricing = cfg.get("pricing") or {}
    for tier in pricing.get("tiers") or []:
        if tier.get("project_type") != project_type:
            continue
        lo, hi = _tier_bounds(tier)
        if lo <= capacity < hi:
            rate = tier.get("rate_per_kw")
            if rate is None:
                return None
            modifier = 1.0
            if segment is not None:
                modifier = (pricing.get("segment_modifiers") or {}).get(segment, 1.0)
            return float(rate) * float(modifier)
    return None


# ── Composition ──────────────────────────────────────────────────────────────────

def _categorize(section_id: str) -> str:
    """Bucket a library section id into a document category by naming convention."""
    if section_id.endswith("_scope"):
        return "scope"
    return "note"


def compose_content(lead_ctx: dict, cfg: dict) -> dict:
    """Compose the resolved proposal `content` dict from base_sections + the rule-
    selected library sections + computed pricing, in fixed document order.
    """
    lead = lead_ctx["lead"]
    customer = lead_ctx["customer"]
    collected = dict(lead.collected_fields or {})
    library = cfg.get("sections_library") or {}
    base = cfg.get("base_sections") or {}

    project_type = collected.get("project_type")
    segment = collected.get("segment")
    capacity_kw = collected.get("capacity_kw")
    location = collected.get("location")
    timeline = collected.get("timeline")

    section_ids, used_fallback = select_sections(collected, cfg)
    if used_fallback:
        if project_type == "rooftop" and not segment:
            # REQUIRED_FIELDS (business_rules.py) doesn't include `segment`, so a
            # lead can be "qualified" and still hit this — every rooftop parameter
            # rule needs segment (residential/commercial/industrial) to match, so a
            # rooftop lead missing it always falls back, silently otherwise.
            logger.warning(
                "Proposal fell back to the generic template because 'segment' was "
                "never collected for a rooftop lead — residential/commercial/"
                "industrial pricing and scope rules all require it to match.",
                extra={"lead_id": lead.id, "project_type": project_type},
            )
        else:
            logger.warning(
                "Proposal fell back to the generic template — no parameter rule "
                "matched this lead's collected fields.",
                extra={"lead_id": lead.id, "project_type": project_type, "segment": segment},
            )
    rate_per_kw = select_pricing(project_type, capacity_kw, segment, cfg)

    # total_value only when a tier matched with a real (>0) rate — placeholders (0)
    # and unmatched tiers leave it unset rather than inventing a number.
    capacity_num = _coerce_capacity(capacity_kw)
    total_value: Optional[float] = None
    if rate_per_kw is not None and rate_per_kw > 0 and capacity_num is not None:
        total_value = rate_per_kw * capacity_num

    segment_modifier = 1.0
    if segment is not None:
        segment_modifier = ((cfg.get("pricing") or {}).get("segment_modifiers") or {}).get(segment, 1.0)

    # Build ordered section list: intro → scope(s) → notes → pricing → terms → closing.
    sections: list[dict] = []
    sections.append({"id": "cover_letter_intro", "category": "intro",
                     "body": base.get("cover_letter_intro", "[PLACEHOLDER]")})

    for section_id in section_ids:
        sections.append({
            "id": section_id,
            "category": _categorize(section_id),
            "body": library.get(section_id, "[PLACEHOLDER]"),
        })

    pricing_body = _format_pricing_body(rate_per_kw, capacity_num, total_value)
    sections.append({
        "id": "pricing",
        "category": "pricing",
        "body": pricing_body,
        "rate_per_kw": rate_per_kw,
        "total_value": total_value,
    })

    sections.append({"id": "standard_terms", "category": "terms",
                     "body": base.get("standard_terms", "[PLACEHOLDER]")})
    sections.append({"id": "closing", "category": "closing",
                     "body": base.get("closing", "[PLACEHOLDER]")})

    # Stable sort by category order (input is already in the right order, but this
    # guarantees the invariant even if section ids are reordered later).
    sections.sort(key=lambda s: _CATEGORY_ORDER.index(s["category"]))

    return {
        "version_note": "Draft — not yet reviewed or approved",
        "customer_name": customer.name if customer else "[PLACEHOLDER]",
        "customer_email": customer.email if customer else "",
        "customer_company": (customer.company or "") if customer else "",
        "project_type": project_type,
        "segment": segment,
        "capacity_kw": capacity_kw,
        "location": location,
        "timeline": timeline,
        "project_overview": (
            f"Grid Nova Energy proposes to design, supply, and commission a "
            f"{capacity_kw} kW {project_type} solar energy system at {location}. "
            f"Estimated project timeline: {timeline}."
        ),
        "sections": sections,
        "matched_rule_ids": [
            rule.get("id") for rule in (cfg.get("parameter_rules") or [])
            if _rule_matches(rule, collected)
        ],
        "pricing": {
            "rate_per_kw": rate_per_kw,
            "segment_modifier": segment_modifier,
            "total_value": total_value,
        },
        "used_fallback_template": used_fallback,
    }


PLACEHOLDER_MARKER = "[PLACEHOLDER"


def placeholder_sections(content: Optional[dict]) -> list[str]:
    """Ids of the stored sections whose body still carries placeholder copy."""
    return [
        str(section.get("id"))
        for section in ((content or {}).get("sections") or [])
        if isinstance(section, dict) and PLACEHOLDER_MARKER in str(section.get("body") or "")
    ]


def has_placeholder_content(content: Optional[dict], total_value: Optional[float]) -> bool:
    """True while a proposal is not fit to send to a customer: some section is still
    placeholder copy, or it has no real price."""
    return bool(placeholder_sections(content)) or not total_value


def with_adjusted_price(content: Optional[dict], total_value: float) -> Optional[dict]:
    """A copy of stored proposal content whose pricing section states a price staff
    set by hand (adjust-pricing), so the section, the PDF and total_value agree. The
    sentence is the engine's own ("Proposed project value: INR …"), without a per-kW
    rate, because a hand-set price has none."""
    if not content or not isinstance(content.get("sections"), list):
        return content
    updated = copy.deepcopy(content)
    for section in updated["sections"]:
        if isinstance(section, dict) and section.get("id") == "pricing":
            section["body"] = f"Proposed project value: INR {total_value:,.0f}."
            section["total_value"] = total_value
    if isinstance(updated.get("pricing"), dict):
        updated["pricing"]["total_value"] = total_value
    return updated


def _format_pricing_body(rate_per_kw: Optional[float], capacity, total_value: Optional[float]) -> str:
    if total_value is not None:
        return (
            f"Proposed project value: INR {total_value:,.0f} "
            f"({capacity} kW @ INR {rate_per_kw:,.0f}/kW)."
        )
    if rate_per_kw is not None:
        return "Pricing pending — a rate is configured but the project value is not yet finalized."
    return "[PLACEHOLDER: Pricing to be confirmed after site assessment.]"
