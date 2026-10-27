# BE-16 — Nova knowledge base: load verified knowledge at runtime, ground every answer in it, prove it with evals

| | |
|---|---|
| **Repo** | `grid-nova-ai-backend` |
| **Branch** | `main` (pull first; clean, level with `origin/main`) |
| **Claude Code model** | **Opus 5.5** |
| **Before you start** | The pack is in the repo at `docs/nova-training/`. Copy `docs/nova-training/knowledge/nova_knowledge.yaml` → `config/nova_knowledge.yaml` and `docs/nova-training/evals/customer_nova_knowledge.yaml` → `evals/customer_nova_knowledge.yaml`. Resolve or leave as `conflict` the entries in `docs/nova-training/knowledge/conflicts.md`. |
| **When it finishes** | Report → review → live eval run → commit → push → CI green. |

Paste everything below the line into a fresh Claude Code session.

---

## Execution standard
- Quality, correctness and evidence over speed. Trace the real code (`app/agents/sales_agent.py`, `app/llm/client.py` `_REPLY_RULES` and prompt assembly, `app/llm/company_facts.py`, `app/agents/guardrails.py` `enforce` and `_justifiable_text`, `app/agents/records_agent.py`, `scripts/eval_nova.py`) before changing anything.
- Zero hallucination: Nova may state only facts from loaded knowledge entries and the existing config. No new facts are written by you; if an entry needs a value, it must come from config.
- Do not fine-tune or call any training API. This is runtime knowledge + guardrails + evals.
- Second pass: re-read every changed prompt path and run every eval suite; update the report.

## House rules
Pre-flight: `main`, clean except the copied pack files, level with `origin/main` — else STOP. Phase 0 report before edits. Baseline then after: full suite (SQLite, Postgres 16), `ruff`, `scripts/export_openapi.py --check`, placeholder registry check, `python scripts/eval_nova.py --suite customer --mock` and `--suite knowledge --mock`. **Never commit, never push.**

## Phase 0 (report, no edits)
1. How the customer reply prompt is built today, its token size, and where `COMPANY_FACTS_BLOCK` sits.
2. How guardrails decide what a reply may assert (`_justifiable_text`, money/warranty/equipment/subsidy/personal-data checks) and how a knowledge fact must be added to the allow-list so a correct answer is not blocked.
3. Whether embeddings are available (`MEMORY_SEMANTIC_SEARCH`, `MEMORY_EMBEDDING_MODEL`) and how `app/memory/recall.py` ranks; reuse it if sound.
4. Validate `config/nova_knowledge.yaml`: ids unique, statuses, every `{config:…}` placeholder resolvable to a real config path (list any that are not), and which entries are loadable (`verified`, `published`, `external`).
5. Which existing facts in `company_facts.py` duplicate knowledge entries.

## Build
1. **Loader + validator** (`app/nova/knowledge.py`): load `config/nova_knowledge.yaml` at startup; validate schema; render `{config:…}` placeholders from the real config (subsidies, price list, proposal content, sales policy, settings) so numbers never drift; refuse to start on an unknown status, a duplicate id, an unresolvable placeholder in a loadable entry, or a forbidden pattern in a fact (prices, per-kW rates, percentages of savings, `[PLACEHOLDER`, `DUMMY`). Load only `verified | published | external`; log the counts of skipped `unconfirmed | conflict | needs_grid_nova`.
2. **Retrieval:** for each customer message pick the most relevant entries (audience-filtered: `customer` on the website, `associate` in the portal, `staff` in Nova Records). Start with a deterministic scorer over `questions` (all locales) + `topic` + keywords; add embedding ranking only if Phase 0 shows it is already wired and cheap. Cap the injected block (e.g. top 6 entries) and always include `company-identity`, `company-contact`, `company-response-time`. Keep `COMPANY_FACTS_BLOCK` behaviour, but source its overlapping facts from the knowledge base (one source of truth) without changing what the existing evals expect.
3. **Prompt:** inject the selected entries as a "VERIFIED KNOWLEDGE" block with each entry's `facts` and `do_not_say`, directly followed by the existing boundary rule ("you may only state facts that appear above…"). Facts are answered in the conversation's language; never translate numbers or names into different values.
4. **Guardrails:** add the injected entries' text to the allow-list used by `_justifiable_text` so verified figures (e.g. "4,800+", "₹78,000" as a rule, "15 days", "120 units") pass, while everything else keeps failing exactly as today. Add a check that a reply never contains a `do_not_say` item of an injected entry.
5. **needs_grid_nova entries:** when a question matches one of them, the reply routes to the team ("a team member will follow up") and the lead's notes record the unanswered question (topic id) so Grid Nova sees which answers are missing most. No new table if an existing field/event fits — report the choice.
6. **Evals:** register `knowledge` in `scripts/eval_nova.py` `SUITES` (→ `evals/customer_nova_knowledge.yaml`); keep the invariants. Add a report section "knowledge gaps" listing which `needs_grid_nova` topics were hit.
7. **Admin visibility (read-only):** `GET /api/v1/dashboard/nova/knowledge` (Admin, Management): entries with id, topic, audience, status, source, as_of — no editing in this task. Contract `docs/api/nova_knowledge.md`, OpenAPI regenerated.
8. **Registry:** one registry entry per `unconfirmed | conflict | needs_grid_nova` knowledge entry (`area: nova`, `blocking: tunable` unless it is a launch blocker), pointing to `docs/nova-training/knowledge/conflicts.md` or the Grid Nova data request number (R1–R26).

## Tests
Loader refuses bad files (each rule); placeholders render from config and change when config changes; only loadable statuses are injected; retrieval picks the right entries for every `questions` example (all locales) and respects audience; the always-included entries are present; guardrails allow a verified figure and still block an unverified one; `do_not_say` blocked; needs_grid_nova routing records the gap; the existing `customer`, `staff`, `records` suites still pass in mock mode; new `knowledge` suite passes in mock mode.

## Final report
Phase 0 findings with file:line; design choices (retrieval method, prompt size before/after, allow-list change); entries loaded vs skipped by status; tests and suites vs baseline; the exact command for the live run (`python scripts/eval_nova.py --suite knowledge --live` and `--suite customer --live`) for the owner to run with a real key; anything not covered. Stop. No commit, no push.
