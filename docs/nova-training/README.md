# Nova training pack — for the CTO

Prepared 26 Oct 2026 for the Grid Nova Energy platform (backend `T-anmoy/gridnovabackend`, frontend `T-anmoy/gridnova`). Read with `docs/HANDOFF.md`.

## 1. The decision: "train" Nova with runtime knowledge, not fine-tuning

Nova runs on an OpenAI-compatible model (`gpt-4o-mini`) behind strict guardrails. We make it expert by giving it **verified knowledge at runtime** and **proving it with evals** — not by fine-tuning a model.

| | Runtime knowledge (chosen) | Fine-tuning (not now) |
|---|---|---|
| A fact changes (subsidy, price list, policy) | Edit one YAML line; live on next deploy | Retrain; old answers linger |
| Proof the answer is allowed | Guardrails check every reply against the injected facts | Model memory cannot be checked |
| Data needed | Approved facts and real customer questions | Hundreds to thousands of high-quality example chats we do not have yet |
| Risk | Low; unknowns route to the team | Confident invented facts |

Fine-tuning becomes worth considering only after launch, for **tone**, using months of reviewed real chats (data request R26) — never for facts.

## 2. How Nova works today (code facts)

- **Customer Nova** (website chat, en/hi/bn): `app/agents/sales_agent.py` + `app/llm/client.py` prompt (`_REPLY_RULES`) + `app/llm/company_facts.py` (verified facts built partly from config: offices, hours, subsidy rules, packages and makes, warranty, O&M) + `app/agents/guardrails.py` (blocks invented prices, rates, savings %, warranty terms, model numbers, DCR claims, subsidy rulings, personal data, leaked internals; detects prompt injection) + customer memory (`app/memory/`).
- **Nova Records** (staff assistant): answers only from database records with citations; `app/agents/grounding.py` checks every figure against the evidence. Its "training data" is the platform's own records (leads, projects, the Project Archive).
- **Evals:** `scripts/eval_nova.py` with `evals/customer_nova.yaml` (66 safety cases in 3 languages), `evals/staff_memory.yaml`, `evals/staff_records.yaml`. Live runs need a real `LLM_API_KEY`.
- **Gap this pack fills:** Nova knows only the short `company_facts.py` block. It cannot answer most real questions (process, net metering, subsidy process, after-sales, associates), and nothing measures whether it answers correctly.

## 3. What is in this pack

| File | What | Use |
|---|---|---|
| `01_DATA_INVENTORY.md` | Every kind of data Nova needs, where it comes from, and its status | Planning and tracking |
| `knowledge/nova_knowledge.yaml` | **45 knowledge entries** across company, services, process, pricing routes, subsidy, technical, after-sales, associates. Each has example questions (en/hi/bn), atomic facts, `do_not_say`, source file and status | Becomes `config/nova_knowledge.yaml` |
| `knowledge/conflicts.md` | 6 contradictions between the website, chat facts and policy | Resolve before loading those entries |
| `evals/customer_nova_knowledge.yaml` | **42 knowledge test cases** (en + hi + bn) in the existing eval format | Becomes `evals/customer_nova_knowledge.yaml` |
| `02_GRID_NOVA_DATA_REQUEST.md` | 26 items only Grid Nova can supply | Send to Grid Nova now |
| `03_BE-16_PROMPT.md` | Claude Code prompt that loads the knowledge, grounds answers, registers the evals | Run in the backend repo |

Entry status today: 15 published (already on the website), 13 verified (Grid Nova / config), 6 external (government rules), 3 unconfirmed, 3 conflict, 5 needs Grid Nova. Only the first three groups (34 entries) are loaded.

## 4. Step by step

1. **Resolve the conflicts** in `knowledge/conflicts.md` with Grid Nova (C1 services list, C2 coverage, C3 payback claim, C4 payout schedule, C5 roof area, C6 track-record wording). Fix the website copy where it disagrees.
2. **Send `02_GRID_NOVA_DATA_REQUEST.md`.** Priority: R1 (real questions with approved answers), R4 timelines, R6 payment options, R7 documents, R10 net metering, R12 after-sales, R13 component warranties.
3. **Run BE-16** (Opus 5.5) in the backend. It loads the YAML, renders numbers from config, retrieves relevant entries per message, extends the guardrails' allow-list, routes unknowns to the team and logs them as knowledge gaps, and registers the `knowledge` eval suite.
4. **Live evals** on production or staging with the real key:
   `python scripts/eval_nova.py --suite customer --live` and `python scripts/eval_nova.py --suite knowledge --live`.
   Gate: 100 % of blocking cases pass in all three languages. Run `--suite records --live` once the Project Archive has real data.
5. **Add each Grid Nova answer** as a new entry (`status: verified`, `source: "Grid Nova, <name>, <date>"`) plus at least one eval case per entry (en; hi/bn for the most common). Redeploy; rerun evals.
6. **Hindi and Bengali:** questions in hi/bn are included for retrieval; facts stay in English and Nova answers in the customer's language. A native speaker reviews a sample of live hi/bn answers monthly.
7. **After launch, monthly loop:** read the "knowledge gaps" report and a sample of chats → ask Grid Nova for missing answers → add entries and evals → redeploy. Never add a fact without a named source.

## 5. Rules that keep Nova safe (do not relax)

- No prices, per-kW rates, discounts, EMIs, savings percentages or payback periods in chat — the Solar Calculator and the team give figures.
- Subsidy is explained as a rule, never as a ruling on a customer ("you will get", "you are eligible").
- No model numbers, no DCR claims for any make, no component warranty years until confirmed.
- Announced state subsidies: named, never an amount, never certain.
- Track record always "of Grid Nova's team and branches", exact figures.
- Unknown → "a team member will follow up" (within 48 business hours), and the gap is logged.
- Personal data: chats contain customer data; reviews follow the privacy policy and stay inside Grid Nova's systems (India's DPDP Act applies).
