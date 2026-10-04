"""
Grid Nova Energy — verified company facts for the Sales Agent's system prompt.

Kept in its own module rather than inline in app/llm/client.py for two reasons:
1. This is factual content, not client/session logic — client.py already carries
   extraction, reflection and the mock-mode machinery, and this doesn't belong
   next to any of that.
2. app/agents/sales_agent.py needs the same text as an addition to what a reply
   is allowed to assert (see _justifiable_text) — a fact like "4,800+ street
   lamps" is a comma-grouped 4-digit figure, exactly the shape
   app/agents/guardrails.py's money check treats as an invented price unless
   something in the conversation justifies it. A verified company fact is
   never invented, so it belongs in the allow-list too. Importing this module
   from both places keeps one source of truth instead of copying the numbers.

Every fact below is pre-verified, not guessed. Do not add anything here that
has not been confirmed the same way — the boundary rule right after it exists
specifically so the model never fills a gap on its own.
"""
from __future__ import annotations

from app.config import settings
from app.pricing.subsidy import (
    MAX_SUBSIDY_INR,
    RATE_PER_KW_FIRST_2KW,
    RATE_PER_KW_THIRD_KW,
    SUBSIDY_ELIGIBLE_UP_TO_KW,
)

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _business_days_phrase() -> str:
    """Real, sourced from app.config.settings.business_days (the same list the
    actual scheduling engine uses to generate slots) — never a separate,
    independently-typed copy of which days Grid Nova operates."""
    days = sorted(settings.business_days)
    if days == list(range(days[0], days[0] + len(days))) and len(days) > 1:
        return f"{_DAY_NAMES[days[0]]}–{_DAY_NAMES[days[-1]]}"
    return ", ".join(_DAY_NAMES[d] for d in days)


def _business_hours_phrase() -> str:
    def _fmt(hour: int) -> str:
        period = "AM" if hour < 12 else "PM"
        display_hour = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
        return f"{display_hour}:00 {period}"

    return f"{_fmt(settings.business_hours_start_ist)} – {_fmt(settings.business_hours_end_ist)} IST"


def _packages_block() -> str:
    """Packages, makes and terms from Grid Nova's Pricing & Equipment Master Data
    (config/price_list.yaml) — makes "or equivalent" as supplied, never models, never
    prices, never DCR status (not supplied). The panel life span line deliberately
    carries no warranty wording, so app/agents/guardrails.py's warranty check cannot
    read it as a 20–25 year warranty."""
    from app.pricing import price_list

    data = price_list.load()
    labels = {"solar_panel": "solar panels", "inverter": "inverter", "cables": "cables"}
    lines = ["Rooftop solar packages (Grid Nova's master data):"]
    for package in price_list.PACKAGES:
        parts = []
        for component, spec in data["equipment"][package].items():
            makes = ", ".join(spec.get("makes") or [])
            text = f"{labels.get(component, component)}: {makes}" + (", or equivalent" if spec.get("or_equivalent") else "")
            if spec.get("technology"):
                text += f" ({' or '.join(spec['technology'])})"
            parts.append(text)
        lines.append(f"- {price_list.display_name(package)} — " + "; ".join(parts))
    spec_makes = sorted({m for item in data["electrical_spec_up_to_5kw"].values() for m in (item.get("makes") or [])})
    lines.append(f"- Electrical items for systems up to 5 kW use makes such as: {', '.join(spec_makes)}")
    terms = data["commercial_terms"]
    lines += [
        f"Standard warranty: {terms['standard_warranty']}.",
        f"Operation and maintenance: {terms['o_and_m']}.",
        f"Solar panel life span: {terms['solar_panel_life_span']} — how long panels are expected to last, "
        "not a cover period.",
        "Package prices are not stated here: send customers to the Solar Calculator on the website or to "
        "their Grid Nova associate for prices. Never state a model number, or that any make is DCR "
        "compliant — DCR status per make is not confirmed.",
    ]
    return "\n".join(lines)


COMPANY_FACTS = f"""GRID NOVA ENERGY — VERIFIED FACTS

Company: Grid Nova Energy Pvt. Ltd. — "Eco-Energy Initiative"
Head office: Plot No. 83, Kasba Industrial Estate, Phase II, Kolkata – 700107, West Bengal
Contact: +91 98747 74440, info@gridnova.in, www.gridnova.in
Business hours: {_business_days_phrase()}, {_business_hours_phrase()}

Track record (verified, exact figures — never round or approximate these):
- 4,800+ street lamps delivered
- 330+ 10kW WBREDA schools powered
- 2,350+ PM-KUSUM solar pumps installed
- 1,200+ PM Surya Ghar residential installations
- 1.3 MWp EPC project delivered for Showa India

Services offered (8 areas):
Rooftop Solar (Residential/Housing Society/Commercial/Industrial/Institutional),
Ground-Mounted Solar, Government & Institutional Solar, Solar Street Lighting,
Solar Water Pumps (PM-KUSUM), O&M Services, Energy Consulting & Project Services,
and emerging solutions (BESS/EV/microgrid — explicitly NOT yet available, do not
offer these as current services).

Service coverage: Grid Nova now serves customers across India, not a limited
region — this supersedes any earlier West Bengal/Bihar-only guidance.

PM Surya Ghar residential rooftop subsidy (Central Financial Assistance):
₹{RATE_PER_KW_FIRST_2KW:,.0f}/kW for the first 2kW, ₹{RATE_PER_KW_THIRD_KW:,.0f} for the 3rd kW,
capped at ₹{MAX_SUBSIDY_INR:,.0f} for any system of {SUBSIDY_ELIGIBLE_UP_TO_KW:.0f}kW or more.
Applies to genuine individual residential rooftop only — never Housing
Society/RWA or any commercial/industrial/government segment.

{_packages_block()}

Leadership: Michael Gomes (Managing Partner), Shouvik Biswas (Partner),
Raju Paul (Partner), Srinjay Das (VP Business), Moloy Das (GM Projects),
Arun Kumar (Zonal Sales Manager), Naren Mukherjee (Senior Advisor — Solar Power).
"""

COMPANY_FACTS_BOUNDARY = """You may only state facts that appear above. If a customer asks something not
covered here — company history, specific past projects beyond what's listed,
certifications, comparisons to competitors, or anything else — say a team
member will follow up with specifics, and move the conversation forward.
Never guess, estimate, or infer a company fact that isn't explicitly stated
above. This rule has the same weight as the existing rule against inventing
a price or a meeting slot — treat any violation of it exactly as seriously.
"""

# The unit the reply prompt inserts: facts, then the rule about their limits,
# immediately adjacent so one can never be read without the other.
COMPANY_FACTS_BLOCK = COMPANY_FACTS + "\n" + COMPANY_FACTS_BOUNDARY

# The one real, confirmed response-time commitment. "Shortly" and "soon" promise
# nothing and read as a brush-off — this is the actual number, used everywhere
# customer-facing timing comes up: the Sales Agent's reply prompt
# (app/llm/client.py's _REPLY_RULES) and its deterministic stand-down reply
# (app/agents/sales_agent.py). One constant so the two can never drift apart.
RESPONSE_TIME_COMMITMENT = "within 48 business hours"
