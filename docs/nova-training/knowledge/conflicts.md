# Conflicts to resolve before loading the knowledge base

Nova may only state one version of a fact. Each conflict below shows the sources that disagree, our recommendation, and who decides. Until resolved, the entry stays `status: conflict` and is not loaded.

| # | Topic | Source A | Source B | Recommendation | Decides |
|---|---|---|---|---|---|
| C1 | Number of services | Website Ask Nova FAQ (`messages/*.json nova.faq[0]`): "five core services: Solar EPC, Green Energy Projects, Grid-Scale Solar, O&M, Energy Consulting" | `app/llm/company_facts.py`: 8 service areas (rooftop by segment, ground-mounted, government & institutional, street lighting, PM-KUSUM pumps, O&M, consulting, emerging BESS/EV/microgrid not yet available) | Use the 8-area list everywhere; reword the FAQ answer to match (en/hi/bn) | Grid Nova |
| C2 | Where Grid Nova works | `company_facts.py`: "serves customers across India … supersedes West Bengal/Bihar-only guidance" | About page "Operating Footprint: West Bengal & Bihar"; FAQ track record "across West Bengal and Bihar" | Keep "across India" for service; keep "West Bengal and Bihar" only for where the track record was delivered and where online prices apply; change the About fact card | Grid Nova |
| C3 | Payback claim | Industries page: commercial rooftop "financially compelling within 3–5 year payback windows" | Nova's guardrails and evals forbid savings/payback promises | Nova never states it; consider removing the claim from the site or citing a basis | Grid Nova + Highline |
| C4 | Associate payout schedule | Careers FAQ: "paid monthly, by the 10th of the following month" (published) | Registry C5-10: "draft says monthly, by the 10th" — still open | Confirm with the Sirs; then mark `published` | Grid Nova (Phase 2) |
| C5 | Roof space per kW | Calculator 100 sq ft/kW | Proposal prints 83–100 sq ft/kWp (PRO-17); old survey 90 | One confirmed figure (EST/PRO-17) | Grid Nova |
| C6 | Track record wording | `company_facts.py`: "Track record of Grid Nova's team and branches" | Some marketing copy may read as Grid Nova Energy's own company history | Always say "Grid Nova's team and branches"; check every page | Highline |
