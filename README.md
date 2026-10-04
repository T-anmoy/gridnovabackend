# Grid Nova AI Operations Platform — Phase 1

AI-powered lead qualification and proposal management for Grid Nova Energy.

## Local development

Three commands, run from this folder:

```bash
# 1. Once: environment and dependencies
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cp .env.example .env

# 2. Database: schema + the territory lists (safe to re-run; add --dummy-data for the DUMMY equipment catalogue)
python scripts/dev_setup.py

# 3. Run the API
uvicorn app.main:app --reload --port 8000
```

`dev_setup.py` builds a new database (or upgrades one that is behind), loads the State → District → Block lists, and ends by printing the schema revision, the territory counts, the `uvicorn` command and the one line the frontend needs in `.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
```

It never deletes a database and refuses to run when `ENVIRONMENT=production`. If it cannot continue (for example the database was created by an earlier `uvicorn` start and has no recorded schema revision), it stops and prints exactly what to do by hand.

Check it worked: `http://localhost:8000/api/v1/health` (the process is up) and `http://localhost:8000/api/v1/health/ready` (database, schema revision and `territories: {seeded, states, districts, blocks}`).

### When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| The State dropdown on the Apply form is empty and `GET /api/v1/public/territories/states` answers **503 `territories_not_seeded`** | The database has no territory data | `python scripts/dev_setup.py` (or just `python scripts/seed_territories.py`) |
| The dropdown is empty and the browser console shows a **CORS error** or "Failed to fetch" | The API is not running, or the page's origin is not in `ALLOWED_ORIGINS`. Only `http://localhost:3000` (and the production site) are allowed by default — `http://127.0.0.1:3000` or another port is a different origin | Start `uvicorn` on port 8000; set `NEXT_PUBLIC_API_URL=http://localhost:8000` and open the frontend at `http://localhost:3000`; or add the origin to `ALLOWED_ORIGINS` in `.env` (a JSON array, e.g. `["http://localhost:3000","http://127.0.0.1:3000"]`) and restart |
| The territory lists fail with **HTTP 500** | The tables are not there yet, or the schema is behind this code | `python scripts/dev_setup.py`; `GET /api/v1/health/ready` names the schema state (`not_initialized`, `behind`) |
| **429** on `/api/v1/public/territories/...` | More than 60 lookups a minute from one IP (public form lookups are rate-limited; the counter is in memory) | Wait a minute, or restart `uvicorn`; look for a page or dev tool that refetches in a loop |
| `dev_setup.py` stops with "no recorded schema revision" | `uvicorn` created the tables itself on an earlier start, so the revision is unknown | Follow the printed steps: back the file up, then **move** it aside (not delete) and run `dev_setup.py` again |
| The app refuses to start with "ALLOWED_ORIGINS contains a localhost origin" | `ENVIRONMENT=production` with the default origins | Production must set `ALLOWED_ORIGINS` explicitly, e.g. `["https://www.gridnova.in"]` |

Production is not set up with `dev_setup.py`: it runs `scripts/bootstrap_db.py` and `scripts/seed_territories.py` (see "Database schema" below), and `GET /api/v1/health/ready` returns **503** there until the territory lists are loaded.

## Running tests

```bash
# From the project root (grid-nova-ai-backend/)
python -m pytest tests/ -v
```

No external services required — tests use an in-memory SQLite database and
LLM mock mode activates automatically when `LLM_API_KEY` is unset.

## Database schema: first deploy and subsequent deploys

The schema is owned by `scripts/bootstrap_db.py` + Alembic. With `ENVIRONMENT=production`
the app does **not** create tables on startup, so run the bootstrap (e.g. as a Cloud Run
job, with the same `DATABASE_URL`) **before** the service's first boot.

### First deploy / fresh database

```bash
DATABASE_URL=postgresql://... python scripts/bootstrap_db.py
DATABASE_URL=postgresql://... python scripts/seed_territories.py   # the State → District → Block lists; idempotent
```

The territory lists are reference data the public Apply forms cannot work without. In
production `GET /api/v1/health/ready` returns **503** (with `territories.seeded: false`)
until they are loaded, so an empty deploy cannot go live; the public lookups answer
503 `territories_not_seeded` in the meantime.

On a database with no application tables it runs `Base.metadata.create_all()` and then
`alembic stamp head`. The historical chain (0001–0022) has no baseline and cannot be
replayed on PostgreSQL, so a fresh database is always built from the models.

### Subsequent deploys

Run the same command. When the application tables and `alembic_version` both exist, it
runs `alembic upgrade head`, which is equivalent to running
`alembic -c alembic/alembic.ini upgrade head` yourself.

If application tables exist but `alembic_version` does not, the script changes nothing
and exits with code 2. It never guesses which revision an unversioned database is at.

New revision ids must be **32 characters or fewer**, because PostgreSQL's
`alembic_version.version_num` is `VARCHAR(32)`. SQLite does not enforce the length.

### Health checks

- `GET /api/v1/health` is **liveness** only: the process is up. It never touches the database.
- `GET /api/v1/health/ready` is **readiness**. It returns 200
  `{"status":"ready","db":"ok","schema":"at_head"}` only when the database answers and
  `alembic_version` equals this build's Alembic head. Otherwise it returns 503 with
  `db` set to `ok`/`unreachable` and `schema` set to `not_initialized`, `behind`,
  `unknown_revision` or `unknown`. Connection details are never included.

Point Cloud Run's **startup probe** (and any post-deploy smoke test) at
`/api/v1/health/ready`. That way a revision that boots before `scripts/bootstrap_db.py`
has run, or against a database behind its migrations, never receives traffic. It would
otherwise pass a plain health check while every database request fails.

## Environment variables required before deployment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (Cloud SQL for PostgreSQL, Mumbai `asia-south1`, HA) |
| `CLERK_SECRET_KEY` | Clerk secret key for JWT verification |
| `CLERK_JWT_ISSUER` | Your Clerk frontend API URL (e.g. `https://clerk.your-domain.com`) |
| `STORAGE_BUCKET` | Cloudflare R2 bucket name |
| `STORAGE_ENDPOINT_URL` | R2 S3-compatible endpoint |
| `STORAGE_ACCESS_KEY` | R2 access key ID |
| `STORAGE_SECRET_KEY` | R2 secret access key |
| `LLM_API_KEY` | OpenAI API key |
| `SENTRY_DSN` | Sentry DSN for error tracking |
| `ALLOWED_ORIGINS` | JSON array of allowed CORS origins |

Optional / defaulted:
- `LLM_BASE_URL` — defaults to `https://api.openai.com/v1`
- `LLM_MODEL` — defaults to `gpt-4o-mini`
- `PROPOSAL_VALUE_MANAGER_APPROVAL_THRESHOLD` — defaults to `500000` (INR)
- `CAPACITY_DIRECTOR_NOTIFY_THRESHOLD_MW` — defaults to `1.0` MW
- `AI_ESTIMATED_COST_PER_CALL` — defaults to `0.01`; used to derive
  `ai_estimated_cost` from the number of AI agent replies in `/analytics`

Meeting scheduling (Google Calendar) — scheduling stays inert until these are set:
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`
- `FERNET_ENCRYPTION_KEY` — encrypts employee Google refresh tokens at rest. Generate with
  `python -c "from app.security.encryption import generate_key; print(generate_key())"`
- `BUSINESS_HOURS_START_IST` (10), `BUSINESS_HOURS_END_IST` (18), `BUSINESS_DAYS` (`[0,1,2,3,4,5]`,
  Mon–Sat), `BUSINESS_TIMEZONE` (`Asia/Kolkata`), `SCHEDULING_DAYS_AHEAD` (5),
  `SCHEDULING_DEFAULT_DURATION_MINUTES` (30)

## Parameterized proposal engine

Proposal content is composed by `app/proposals/content_engine.py` entirely from
`config/proposal_content.yaml` — Grid Nova staff edit copy, pricing tiers, and matching
rules without touching code. Composable `parameter_rules` (all matching rules contribute
their sections) select the scope/notes; capacity-tiered `pricing` + `segment_modifiers`
compute the value. The config is **validated at startup** (overlapping tiers or a rule
referencing a missing section id refuse to boot). An unmatched combination falls back to a
generic scope and sets `used_fallback_template = true` on the proposal so the dashboard
flags it for manual review before sending.

## Meeting scheduling (Google Calendar)

Availability is a deterministic free/busy query across Sales/Admin employees who have
connected their calendars — never an LLM guess. Booking creates a real Google Meet event
and emails both parties (`sendUpdates="all"`), after re-verifying the slot is still free.

```
GET  /api/v1/dashboard/employees/me/calendar/connect   start Google OAuth (any authed employee)
GET  /api/v1/dashboard/employees/me/calendar/callback  OAuth callback — stores encrypted refresh token
POST /api/v1/dashboard/leads/{id}/schedule-meeting     employee-initiated booking (Sales/Admin)
```

The Sales Agent also offers real slots mid-conversation and auto-books once the customer
confirms one (the confirmed slot is always one it actually offered — re-verified free
before the event is created). Only Sales/Admin connected calendars receive bookings; with
zero connected calendars, scheduling fails with a clear error rather than doing nothing.

## Dashboard read/list endpoints

These back the internal dashboard. All require Clerk auth and enforce the same
role matrix as the action endpoints — **filtering happens in the query, not the
response**, so no role ever receives rows it cannot see.

```
GET  /api/v1/dashboard/proposals            list (status, date_from, date_to, lead_id, page, page_size)
GET  /api/v1/dashboard/proposals/{id}       detail + all versions for the lead
GET  /api/v1/dashboard/analytics            pipeline + AI-usage metrics (date_from, date_to, employee_id)
GET  /api/v1/dashboard/employees            Admin only
POST /api/v1/dashboard/employees/{id}/role  Admin only — syncs Clerk public_metadata.role
GET  /api/v1/dashboard/audit-log            Admin full, Management read-only
```

Role scoping: Sales is scoped to its own leads/proposals and its own analytics;
Management is read-only; Engineers get a technical-fields-only projection on
proposals and are blocked from analytics, employees, and the audit log.

## Architecture

```
Customer → POST /api/v1/chat/message
              └─ Orchestrator (LangGraph StateGraph)
                    └─ Sales Agent (field extraction + reply)
                          └─ MemoryManager (single DB gateway)

Employee → POST /api/v1/dashboard/proposals/draft
              └─ Orchestrator → gate (readiness check) → Proposal Agent
                                                              └─ MemoryManager

POST /api/v1/dashboard/proposals/{id}/approve          (Sales/Admin, standard)
POST /api/v1/dashboard/proposals/{id}/manager-sign-off (Management/Admin, high-value)
POST /api/v1/dashboard/proposals/{id}/send             (Sales/Admin, after approval)

Employee → POST /api/v1/dashboard/memory/ask
              └─ Orchestrator → Memory Agent (retrieve, then answer from records)
                                      └─ MemoryManager
```

## How Nova talks

The prompt is the product here, so it is treated as one. Measured over thirteen
replies across three scenarios (homeowner, industrial, a customer burned by a
previous vendor), before and after a rewrite:

| | Before | After |
|---|---|---|
| Replies stacking more than one question | 3 / 13 | **0** |
| Exclamation marks | 12 | **0** |
| Chatbot openers ("Thanks for sharing that!") | 3 | 1 |
| Median reply length | 39 words | **29** |

What changed, in order of how much it mattered:

- **One question. Not two.** Stacking questions is the fastest way to feel like a
  form, and people answer only the last one. It is now the first rule in the prompt,
  with the reason attached.
- **Banned openers, by name.** "Thanks for sharing that", "That's great to hear",
  "Great!", "Perfect!", "Just to get a clearer picture" — each one a chatbot tell.
  The replacement is to react to the specific thing the person said.
- **Almost no exclamation marks.** Enthusiasm you perform reads as false; interest
  you show by asking a good question reads as real.
- **Use their name once, not every message.** "Thanks, Meera!" in every reply is a
  salesperson's tic, not warmth.
- **Hospitality:** courteous and unhurried, never pressuring, and easy to say "not
  now" to. Hesitation is taken seriously rather than probed or brushed past with
  optimism.

Two bugs the live run exposed, both now fixed and regression-tested:

**Nova quoted a figure out of its own prompt.** A customer whose bill was 40,000 was
told "four lakh a month is a serious number" — lifted verbatim from an example. Every
illustrative figure in the prompt is now a placeholder, and the rule is stated
outright: the only numbers Nova may say are ones from the conversation.

**The money guardrail only understood digits.** "We can do this for about eight lakh"
— an invented price in words — passed every check. Amounts are now normalised to
their value, so "3 lakh", "300000" and "three lakh" are one number, and a percentage
can never justify a rupee amount of the same digits.

## What a conversation costs

Measured against the live model, five customer turns plus one reflection and one
recall — fourteen calls in total:

| Operation | Calls | Prompt | Completion |
|---|---|---|---|
| generate_reply | 5 | 8,437 | 154 |
| extract_fields | 5 | 1,300 | 223 |
| answer_from_memory | 1 | 721 | 14 |
| extract_meeting_signal | 2 | 612 | 23 |
| reflect | 1 | 598 | 349 |

**94% of all tokens are prompt tokens**, and the reply prompt alone is ~1,500 of them
resent on every reply. That is the shape to optimise, not the completions.

**Prompt caching is what makes it cheap.** OpenAI caches a prompt prefix from 1024
tokens up, on an exact match. The per-turn context (`CURRENT STAGE`, `ALREADY KNOWN`,
the memory block) used to sit at the TOP of the prompt, which put the shared prefix at
64 tokens and cached nothing at all. Moving it below the rulebook — which is now a
module constant so it is byte-identical every call — took caching from **0% to ~49%
of all input tokens**, and the cost of a five-turn conversation from $0.0023 to
**$0.0018**, about **$1.78 per 1,000 leads**.

There is a regression test for this. Anything that reintroduces per-turn text above
the rulebook silently doubles the bill for every reply, and it is not the kind of
mistake that shows up in behaviour.

## Model calls only when there is something to decide

The Sales Agent used to make three model calls on every turn regardless of what the
customer said. `app/agents/need.py` gates the two optional ones behind deterministic
checks, so a call is bought only when the agent could actually act on the answer.

| Turn | Calls | Skipped |
|---|---|---|
| "hello, I'm looking at solar for our factory" | 2 | 1 |
| "ok thanks" | **1** | 2 |
| "rooftop please, about 50 kW to start" | 3 | 0 |
| "can we get on a call this week?" | 3 | 0 |

Measured over a seven-turn conversation against gpt-4o-mini: **15 calls instead of
21, a 29% saving**, with latency on the cheapest turns dropping from ~2.5s to 1.3s.

- **Field extraction** is skipped only for a short message made entirely of
  acknowledgements, with no digits and no email. "ok thanks" has nothing to extract;
  "ok, it's 50 kW" does.
- **The meeting signal** is skipped unless slots are already pending, the stage is
  meeting_offer, or the message mentions meeting, calling or visiting. Pending slots
  force the call regardless of wording, because a bare "yes" is a customer picking a
  slot and contains no scheduling word at all.
- **The NeMo rail** is skipped once a deterministic check has already blocked the
  reply. A second opinion that cannot change the outcome is a bill, not a control.

Every gate is biased the same way: **when in doubt, call the model.** A wasted call
costs a fraction of a cent; a missed meeting request costs a customer, and a missed
field means asking someone twice for something they already told us. Skips are
counted on the run and reported per agent, so the saving is visible rather than an
invisible optimisation nobody can verify.

## Agent harness

Every agent runs inside one execution wrapper, `app/agents/harness.py`. Before it,
each agent was invoked ad-hoc: every call site repeated its own try/except,
governance was called separately inside each agent (and could simply be forgotten in
a fourth one), a turn had no identity you could trace, and nothing measured how long
an agent took or how many model calls it made.

```
harness.run(agent_id, action, fn, memory=..., lead_id=..., on_denied=..., on_error=...)
  -> AgentRun(run_id, code, result, error, governance_code,
              duration_ms, llm_calls, estimated_cost_usd, slow)
```

Every run gets: **governance** applied here rather than trusted to each agent; a
**run id** stamped into every log line for that turn; **timing**, with a warning past
`AGENT_SLOW_RUN_MS`; **model-call and cost accounting**; **exception containment**, so
a raised agent becomes a failed run with a caller-supplied fallback rather than an
exception escaping into a customer conversation; and a **durable record** in the
audit trail.

```
GET /api/v1/dashboard/governance/runs   (?days=7 - volume, failures, latency, spend)
```

```
5c54880505b6  memory_agent   answer_employee_question ok    3.3ms  llm=1  $0.0100
697a04e16693  sales_agent    reply_to_customer        ok    7.1ms  llm=3  $0.0300
```

Two things it deliberately does **not** do, because doing them badly would be worse
than not doing them:

- **No wall-clock timeout on the agent body.** A Python thread cannot be killed, and
  each agent holds a database session that is not safe to use from another thread, so
  a "timeout" here would return early while the work carried on underneath. Time is
  bounded where it is actually spent: `LLM_REQUEST_TIMEOUT_SECONDS` on the model
  client. What the harness measures, it measures honestly.
- **No retries.** A retried customer reply is a second message to a real person and a
  retried booking is a second calendar entry. The model client's own transport-level
  retries (`LLM_MAX_RETRIES`) cover the transient case, where repeating is safe.

Denied and failed runs are recorded on their own session, because the caller is about
to roll back and those are the runs most worth having a record of.

## Agent governance

`app/governance/registry.py` declares, as data, what each agent is permitted and
forbidden to do. `app/governance/policy.py` enforces that declaration at the moment
an action is attempted, so a prohibition is a runtime denial rather than a comment
someone might not read.

| Agent | Autonomy | Speaks to | LLM | Never |
|---|---|---|---|---|
| Sales Agent | autonomous | customers | yes | price, draft, approve, send |
| Proposal Agent | human-triggered | internal only | **no** | send, approve, reply to a customer, choose a price |
| Memory Agent | read-only | employees | yes | reply to a customer, write anything |

Four checks run before any governed action: is the agent switched on, is the action
in its capabilities, is it explicitly prohibited, and is the agent inside its budget
for this lead. Denials are written to the audit log against the agent, and an attempt
at something *prohibited* also raises an internal notification -- an agent reaching
for a forbidden action is not routine.

**Kill switch.** `DISABLED_AGENTS=["sales_agent"]` stops an agent acting through
configuration, with no deploy. A stood-down Sales Agent still logs the customer's
message and answers with a plain handoff ("let me bring someone from our team in"),
notifies the team, and leaves the lead untouched -- no extraction, no scoring, no
stage change. An agent that has been switched off should not still be quietly
writing to the record.

**Runaway protection.** `AGENT_DAILY_ACTION_LIMIT_PER_LEAD` (default 60) caps agent
replies to one lead per 24h, then hands the conversation to a person. It applies only
to customer replies: drafting and recall are human-triggered, so a person is already
the rate limit there.

**Reporting.**

```
GET /api/v1/dashboard/governance/agents     (registry + limits + which are enabled)
GET /api/v1/dashboard/governance/activity   (?days=7 - denials, guardrail blocks)
```

Both are Admin/Management only, and both are served from the same registry the
enforcement layer reads, so the page cannot drift from what the agents may actually
do -- the usual failure of a governance document maintained by hand.

**The invariants are now tested, not just written down.** `tests/test_governance.py`
parses the agent modules and asserts they do not import each other, that the Proposal
Agent has no reference to sending, that only the Sales Agent writes customer-facing
messages, and that the business rules and content engine never reach an LLM. Those
were the rules most likely to be broken by a future change made in good faith,
precisely because nothing was checking them.

Governance never crashes what it governs: if a check errors it allows the action and
logs loudly. The deterministic business rules, the guardrails and the human approval
gates are unchanged either way, and they are what actually protect the commercial
outcome.

## Sales Agent guardrails

Two layers. The first is deterministic and always on; the second is semantic,
optional, and additive.

### Layer 1: deterministic checks (always on)

The reply prompt tells Nova never to quote a price, never to invent a meeting time,
and never to claim something was approved or sent. Those are instructions to a model.
`app/agents/guardrails.py` enforces the same rules in code, on the way out, because a
prompt instruction is not a control -- the same reasoning that already validates a
confirmed meeting slot against the offered list.

The rule that makes it precise: **the agent may repeat a number the customer gave it,
and may not introduce one.** A customer saying "our bill is 3 lakh" and Nova replying
"3 lakh a month is a lot to carry" is fine; "we can do this for 8 lakh" is a
commercial commitment nobody authorised. Only the customer's own words, the fields on
file, and slots from a real calendar query count as justification -- never Nova's own
earlier replies, or one invented figure would license itself forever.

| Check | Blocks? |
|---|---|
| A money figure or percentage the conversation cannot justify | yes |
| A meeting time or day that was never actually free | yes |
| A completed or guaranteed claim ("has been approved", "I guarantee") | yes |
| System-prompt markers, employee ids, or a stored memory recited verbatim | yes |
| Empty reply | yes |
| Runaway length | no -- trimmed at a sentence |
| Three or more stacked questions | no -- recorded only |

A blocking violation swaps in the deterministic, stage-aware fallback the LLM client
already uses when a call fails. Redacting mid-sentence would leave a worse message
than replacing it. Every decision is written to the audit log against the lead with
the original text kept, so a model that starts drifting is visible rather than
silently corrected -- and a blocked reply also fires an internal notification.

Inbound, `detect_injection` flags attempts to talk the agent out of its instructions.
Detection only: the message is answered normally and never edited, because refusing to
answer a real customer who phrased something oddly is a worse failure than the attempt
itself. The output checks are what actually contain the damage.

### Layer 2: NeMo Guardrails (semantic, optional)

The deterministic checks above catch what can be enumerated. They cannot catch
internal information that has been *paraphrased* past every pattern:

| Reply | Deterministic | + NeMo |
|---|---|---|
| "Anything above 5 lakh needs manager sign-off" | blocked (money) | blocked |
| "Rajesh from our Surat team handles accounts like yours" | **passes** | blocked |
| "We did a similar 200 kW job for a textile mill down the road" | **passes** | blocked |
| "3 lakh a month is a lot to carry. What is the roof like?" | passes | passes |

`app/agents/nemo_guard.py` runs a NeMo Guardrails output rail over each generated
reply, judged against `app/agents/nemo/output_policy.md` -- a plain-language
disclosure policy the sales team can read and amend without touching code. It covers
pricing internals, approval machinery, named employees, other customers, internal
record-keeping, and system internals; it explicitly permits repeating a figure the
customer gave and offering a real meeting time.

It is **layered on top of, never in place of** the deterministic checks. An allow
verdict from the model cannot rescue a reply the deterministic layer already
rejected, and turning this off returns exactly the guarantees of Layer 1.

**Off by default**, because it is an extra LLM call on every customer reply -- real
latency and spend, and probabilistic, so it will occasionally miss and occasionally
misfire. That is also why the policy is written to prefer allowing an ambiguous
message. Set `NEMO_GUARDRAILS_ENABLED=true` (needs `LLM_API_KEY`) to turn it on; in
mock mode it stays off regardless, because there is no model to call.

On error or timeout it **fails open** -- the reply is sent, the deterministic checks
having already run -- because an outage would otherwise silence every conversation.
`NEMO_GUARDRAILS_FAIL_CLOSED=true` inverts that for a deployment that would rather
lose the reply than risk a leak.

Deployment note: `nemoguardrails` weighs roughly 400MB installed, pulling
`onnxruntime`, `numpy` and `huggingface-hub` through `fastembed`. Verified not to
downgrade `langchain-core` below the CVE-2025-68664 pin. Loading the rail needs no
model download and takes ~3s, once, lazily on the first reply that uses it. If image
size matters more than this rail, drop the line from `requirements.txt` -- the code
degrades to Layer 1 on its own when the package is absent.

## Customer memory (episodic + self-reflection)

The Sales Agent only sees the last few messages of a conversation. Anything older
falls out of that window, and unless it landed in one of the twelve structured
qualification fields it used to be lost. Customer memory is what keeps the rest.

**How a memory gets written.** At a milestone — qualification completes, a meeting
is booked, a lead goes quiet, or a long conversation crosses
`MEMORY_REFLECT_EVERY_N_MESSAGES` — the agent reflects on the conversation and
writes what is worth keeping into `customer_memories` (episodes, preferences,
objections, commitments) and rewrites `customer_profiles` (a rolling summary, open
questions, and the agent's own notes on how it handled the lead). That is one to
three LLM calls over a lead's whole life, run on a background thread after the
commit, so a customer's reply never waits on it.

**How a memory changes the next conversation.** Reflection alone would only give
you a record. On every later turn the Sales Agent loads that customer's profile and
their most relevant memories — ranked against what the customer just said — and
hands them to the reply generator, with the agent's own self-critique restated as a
directive ("what YOU should do differently"). The prompt tells it not to re-ask what
memory already answers, to move toward the recorded next step, and to treat the
whole block as data rather than instructions. A returning customer's new lead also
inherits their identity (name, email, phone, company) but never the old project's
numbers, so the stage machine sees a known person with an unknown project instead
of a blank slate.

**The Proposal Agent's half is deterministic.** The Sales Agent has to judge its own
conversation, which is why its reflection needs a model. The Proposal Agent does not:
every judgement on its work is a structured human action — approved, sent back with a
written reason, repriced from X to Y, accepted. So `record_proposal_outcome` writes
those as `outcome` memories with no LLM involved, and turns the two that actually
teach something (a revision request, a downward reprice) into lessons. A revision
reason is stored verbatim; paraphrasing the clearest feedback the agent ever gets
would be the one place a model could distort it.

On the next draft, `app/proposals/drafting_notes.py` reads those back into
`proposal.content["drafting_notes"]`: prior outcomes, lessons, what the Sales Agent
learned about the customer in conversation, and warnings when the tiered rate has
produced a price this customer already pushed back on. **It cannot move the price.**
Notes are attached after composition and pricing are settled, so sections and value
stay reproducible from `proposal_content.yaml` alone — memory advises the reviewing
employee, who has `adjust-pricing` for exactly this. The notes are internal: the PDF
renders named keys and the `sections` list, never this block.

Proposal lessons live on `customer_memories` only and never touch
`CustomerProfile.agent_notes`, which belongs to the Sales Agent — two agents
rewriting one field would silently clobber each other. That is also the one place
the two agents' memory meets: neither calls the other, they just read the same rows.

**How a memory gets found.** Keyword retrieval is the default and needs nothing
extra: term overlap, weighted by how important the memory was judged to be and how
recent it is. Setting `MEMORY_SEMANTIC_SEARCH=true` additionally embeds every new
memory and blends cosine similarity into the same ranking. The two coexist —
memories written before the flag was turned on keep working on keywords, so
switching it on strands nothing.

```
GET  /api/v1/dashboard/customers/{id}/memory  (profile + every memory; audit view)
      ?kind=outcome                           (just what happened to the drafts)
POST /api/v1/dashboard/memory/ask             ({"question": "...", "customer_id": optional})
POST /api/v1/dashboard/leads/{id}/reflect     (force a reflection now)
```

Three rules this feature does not bend:

- **Employees only.** The Memory Agent can read every customer's history, so it is
  never wired into the public chat endpoint. Engineers are excluded too — memory is
  full of PII and commercial context. Sales sees its own customers, matching the
  scoping on the rest of the Sales surface.
- **Grounded answers only.** The answer comes from retrieved records and cites them.
  What is not on file comes back as not on file, never as a plausible guess.
- **Opinion is labelled.** `insight` and `self_critique` memories are the assistant's
  own conclusions, and recall marks them as such so nobody reads them as something
  the customer said.

## Known limitations

- **Territory roster places plain Associates by home address.**
  `GET /api/v1/associate/territory/members` lets a Preferred Associate see the
  associates below them in their territory (`RosterScope` in `app/auth/scoping.py`).
  A Preferred Associate is placed by the territory they hold. A plain Associate holds
  no territory, so they are placed by the state/district/block of the address on
  their registration application. Someone who lives in one district but works in
  another therefore appears on the home district's roster. This is a deliberate
  business-rule choice, not a bug: revisit it once real associates are on the
  platform and it is clear whether home district matches working district in
  practice.

## Non-negotiable principles

1. One public AI face — only Sales Agent ever speaks to customers.
2. Only the Orchestrator calls an agent — agents never import each other.
3. One shared memory — all DB writes go through `MemoryManager`.
4. Every commercially significant action requires a human — no code path sends
   a proposal without an authenticated employee explicitly triggering it.
5. The AI drafts. It never decides — lead scoring, approval thresholds, and
   all business-critical judgments are deterministic functions, never LLM prompts.
6. Memory is an enhancement, never a dependency — a failed reflection, a failed
   recall or an unreachable embedding API degrades the wording of a reply and
   nothing else. No lead, proposal or customer reply depends on it succeeding.
7. What an agent may do is declared in one place and enforced at the point of
   action — a prohibition is a runtime denial, not a docstring.
8. A prompt instruction is not a control — anything the agent must never say is
   checked in code on the way out, and every block is audited.
9. Memory shapes wording, never decisions — recall reaches the reply prompt and
   stops there. The conversation stage, qualification and pricing stay pure
   functions of the lead's own collected fields.

## Deployment (Google Cloud Run + Cloud SQL)

1. Provision a Cloud SQL for PostgreSQL instance in Mumbai (`asia-south1`), HA
   (regional) configuration.
2. Build and deploy the container to Cloud Run (it listens on the `$PORT` Cloud Run
   injects; the `Dockerfile` falls back to `8000` locally).
3. Connect Cloud Run to the Cloud SQL instance and set `DATABASE_URL` accordingly.
4. Set all environment variables listed above, and `ENVIRONMENT=production`.

Run Alembic migrations after the first deploy:
```bash
alembic -c alembic/alembic.ini upgrade head
```
(`alembic.ini` lives at `alembic/alembic.ini`, not the project root — the bare
`alembic upgrade head` command fails immediately with "No config file 'alembic.ini'
found" unless `-c` points at it, or the command is run from inside `alembic/`.)

### Deploying to a fresh database

This repo has historically relied on `Base.metadata.create_all()` (run automatically
by the app's startup lifespan in `app/main.py`) rather than a true baseline migration —
see the note at the top of `alembic/versions/0001_scheduling_and_fallback.py`. Every
migration from `0001` onward is an incremental `ALTER` that assumes the tables it's
touching already exist, so running `alembic upgrade head` against a genuinely empty
database fails (`NoSuchTableError`), and running it against a database `create_all()`
already brought fully up to date fails a different way (`CircularDependencyError` on
SQLite, "column already exists" on Postgres) — the columns those migrations try to add
are already there.

The correct, current workflow against a brand-new database is therefore:

1. Start the app once (`uvicorn app.main:app`, or just let Cloud Run's first boot do
   it) — the startup lifespan calls `Base.metadata.create_all()` and builds the full
   current schema.
2. Mark migration history as caught up, **without** replaying any migrations:
   ```bash
   alembic -c alembic/alembic.ini stamp head
   ```
3. From then on, `alembic -c alembic/alembic.ini upgrade head` works normally for any
   *new* migration added after this point, since the database will already be at
   `head` in Alembic's bookkeeping.

Writing a real `0000` baseline migration (so `upgrade head` alone works end-to-end
against a genuinely empty database, the way it should) is a known follow-up — tracked,
intentionally not done in this change.
# gridnovabackend
