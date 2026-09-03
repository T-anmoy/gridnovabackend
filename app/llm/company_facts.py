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

COMPANY_FACTS = """GRID NOVA ENERGY — VERIFIED FACTS

Company: Grid Nova Energy Pvt. Ltd.
Head office: Plot No. 83, Kasba Industrial Estate, Phase II, Kolkata – 700107
Contact: +91 98747 74440, info@gridnova.in, www.gridnova.in

Track record (verified, exact figures — never round or approximate these):
- 4,800+ street lamps delivered
- 330+ 10kW WBREDA schools powered
- 2,350+ PM-KUSUM solar pumps installed
- 1,200+ PM Surya Ghar residential installations

Services offered (8 areas):
Rooftop Solar (Residential/Society/Commercial/Industrial), Ground-Mounted Solar,
Government & Institutional Solar, Solar Street Lighting, Solar Water Pumps,
O&M Services, Energy Consulting & Project Services, and emerging solutions
(BESS/EV/microgrid — explicitly NOT yet available, do not offer these as current services).

Primary service areas: West Bengal and Bihar. Other eastern states considered
on a feasibility basis — never promise service outside West Bengal/Bihar without
this caveat.

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
