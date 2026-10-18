# Nova Records — the internal staff assistant (BE-9)

Written from the code: `app/api/routes/nova_records.py`, `app/agents/records_agent.py`,
`app/records/` (tools, router, compose, store, access), `app/agents/grounding.py`
(`check_records_answer`), `config/nova_records.yaml`. Conventions in [README.md](README.md)
apply (structured errors, money as decimal strings, UTC ISO times).

Nova Records answers a signed-in employee's question about anything on record — live and
archived projects, leads, customer memory, payments, warranties, service requests,
associates, the approved catalogue and Grid Nova's verified company facts — **only from
read-only tools, within that employee's role**. It never changes anything: there are no
write tools, and requests to change, send, assign, approve, book or reveal identity data
are refused before anything is read.

How an answer is made (all of it server-side):

1. **Screen** — refusals are deterministic (`app/records/safety.py`).
2. **Plan** — the model picks at most 3 tools (`records_plan`); one repair attempt on an
   invalid plan, then `needs_clarification`. No model (mock mode) or a failed call → a
   deterministic keyword planner.
3. **Execute** — the tools apply the caller's scoping and field redaction; row cap 50;
   Postgres statement timeout per call.
4. **Compose** — the model writes a short answer from the tool results only
   (`records_answer`, temperature 0). **Tables are never written by the model**: `tables`
   are built straight from tool rows.
5. **Verify** — every cited ref exists in the results; every amount, number, kW, %, date,
   email, phone and reference appears in the results (customer-written free text excluded);
   every total / count / average equals a value a tool computed in SQL; nothing the role
   may not see; no claim that an action was taken. Failure → one retry → else a
   deterministic templated answer (audit `records_answer_unverified`).

---

## Routes

All under `/api/v1/dashboard/nova`. Staff credentials only (an associate token → 401).

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/ask` | all six staff roles | Ask a question |
| GET | `/conversations` | all six | The caller's own conversations |
| GET | `/conversations/{conversation_id}` | all six (own); Admin any | One conversation with every turn |
| POST | `/turns/{turn_id}/feedback` | all six (own turns) | Helpful / not, with a note |
| POST | `/internal/check-nova-records-retention` | Admin or `X-Internal-Key` | Retention job (Cloud Scheduler, daily) |

### POST /ask

Request:

```json
{
  "question": "Show me every 5 kW+ project we commissioned in Howrah in 2025 and the inverters used",
  "conversation_id": "9f2c…32 hex…",          // optional — continue a conversation (own only)
  "context": {                               // optional — the page the question is asked from
    "lead_id": 45, "project_ref": "GNE-WB-RES-2026-00125", "customer_id": 12
  }
}
```

- `question`: 1–1000 characters (blank after trimming → 422 `empty_question`).
- `conversation_id`: 32 lowercase hex characters; omit to start a new conversation.
- `context`: any subset; unknown keys → 422. The tools still apply the caller's scoping to
  anything named here (a lead the caller may not see is simply not on record).

Response 200:

```json
{
  "conversation_id": "4b0e5c…",
  "turn_id": "c71d0a…",
  "status": "answered",
  "answer": "Found 2 projects matching district Howrah, at least 5 kW, commissioned from 2025-01-01, commissioned to 2025-12-31.\n[GN-ARC-0102] 10.000 kW; commercial; Howrah, West Bengal; customer Kabir Das; commissioned 2025-07-01; modules Adani; inverter Sungrow\n[GN-ARC-0101] 5.000 kW; …",
  "citations": [
    {"ref": "GN-ARC-0102", "kind": "archive_project", "label": "10.000 kW, Howrah, West Bengal", "path": "/dashboard/archive/2"},
    {"ref": "GN-ARC-0101", "kind": "archive_project", "label": "5.000 kW, Howrah, West Bengal", "path": "/dashboard/archive/1"}
  ],
  "tables": [
    {"title": "Projects",
     "columns": [{"key": "ref", "label": "Reference", "type": "ref"},
                 {"key": "source", "label": "Source", "type": "text"},
                 {"key": "scheme_or_segment", "label": "Scheme", "type": "text"},
                 {"key": "capacity_kw", "label": "Capacity (kW)", "type": "kw"},
                 {"key": "district", "label": "District", "type": "text"},
                 {"key": "commissioning_date", "label": "Commissioned", "type": "date"},
                 {"key": "inverter_make", "label": "Inverter make", "type": "text"}],
     // abridged: every visible card field (system, state, block, module make, customer…) is a column
     "rows": [{"ref": "GN-ARC-0102", "source": "archive", "scheme_or_segment": "commercial", "capacity_kw": "10.000",
               "district": "Howrah", "commissioning_date": "2025-07-01", "inverter_make": "Sungrow"}]}
  ],
  "searched": [
    {"tool": "search_projects",
     "filters": {"district": "Howrah", "capacity_min_kw": "5", "commissioned_from": "2025-01-01", "commissioned_to": "2025-12-31"},
     "summary": "district Howrah, at least 5 kW, commissioned from 2025-01-01, commissioned to 2025-12-31"}
  ],
  "suggestions": [
    {"label": "Open the first result", "question": null, "path": "/dashboard/archive/2"},
    {"label": "Details of the first result", "question": "Show me everything on record for GN-ARC-0102", "path": null},
    {"label": "Totals by district", "question": "How many of these projects are there by district?", "path": null}
  ],
  "generated_at": "2026-10-17T11:02:44.120371+00:00"
}
```

#### `status`

| status | Meaning | `answer` | `tables` / `citations` |
|---|---|---|---|
| `answered` | Records found and the answer is verified (or the templated answer) | short English text citing refs in brackets | from the tool rows |
| `not_on_record` | Every tool came back empty **for this caller** | `Nothing on record that you can see matches <filters>. Try a broader search — …` | empty |
| `needs_clarification` | Nothing searchable in the question, an unusable plan, or several customers match a name | a question back; for several matches it lists them with refs | candidate rows, when there are any |
| `refused` | A request to change/send/book/assign, identity data, the instructions, or something the role cannot see | a fixed sentence (below) | always empty |

A project or lead the caller may not see is `not_on_record`, exactly like one that does
not exist — the answer never reveals that a hidden record exists.

Fixed refusal texts (`app/records/safety.py`, `app/records/tools.py`):

- change/assign/approve/delete: *"I can only read records — I can't change, add, assign, approve or delete anything. Open the record in the dashboard to make that change."*
- send/email/message/book: *"I can't send messages, emails or invitations, or book anything — I only read records. Use the dashboard to contact the customer or schedule a meeting."*
- Aadhaar / PAN / bank / identity documents / KYC: *"I never show Aadhaar, PAN, bank details or identity documents. Authorised staff can see the masked values on the record itself."*
- instructions / system prompt: *"I can't share my instructions. Ask me about projects, leads, customers, payments or warranties on record."*
- role: *"Your role does not see contract values or payments."*, *"Your role cannot search projects by customer name."* (by block / location / associate), *"Your role does not see leads."*, *"Your role does not read customer memory."*, *"Your role does not see service requests."*, *"Your role does not see associates."*, *"Your role does not see list prices."*

When some tools ran and another was refused or failed, `status` is `answered` and the
refusal sentence is appended to `answer`.

#### `answer`

Plain text (may contain `\n`). Refs are cited in square brackets exactly as they appear in
`citations[].ref`: `[GNE-WB-RES-2026-00125]`, `[GN-ARC-0101]` or `[ARC-17]` (an archived
project without a Grid Nova ref), `[L45]` lead, `[C12]` customer, `[M301]` customer memory,
`[SR-4]` service request, `[GNA-WB-26-00001]` associate, `[PKG-NOVA_ASSURED]` /
`[EQ-7]` / `[PRICE-…]` catalogue, `[COMPANY]` company facts. Render bracketed refs that
appear in `citations` as links to their `path`.

#### `citations[]`

`{ref, kind, label, path}` — the records the answer cites, in the order cited.

Links go only to the four staff pages that exist. `path` is **null** when no page opens that
record, or when the caller's role cannot open it on that page — show the chip without a link.

| kind | ref | path |
|---|---|---|
| `live_project` | `GNE-…` | `/dashboard/operations/{onboarding_id}` when the project has an onboarding and the role opens execution (Admin, Management, Sales, Engineer, Operations — Sales/Operations already scoped). Otherwise `/dashboard/leads/{lead_id}` (the project's lead page, which shows the project) for roles that read leads. Otherwise null — e.g. Accounts |
| `archive_project` | Grid Nova ref or `ARC-<id>` | `/dashboard/archive/{id}` (every staff role) |
| `lead` | `L<id>` | `/dashboard/leads/{lead_id}` (lead refs are only returned to roles that read leads, within their scope) |
| `service_request` | `SR-<id>` | `/dashboard/service-requests/{id}` (returned only to roles that open that page: Admin, Management, Sales, Engineer, Operations) |
| `customer` | `C<id>` | **null** — there is no customer page |
| `memory`, `associate`, `catalogue`, `price_list`, `company` | `M<id>`, `GNA-…`, `PKG-…`/`EQ-…`, `PRICE-…`, `COMPANY` | null |

The patterns live in `config/nova_records.yaml` `paths` (`live_project`, `lead`,
`service_request`, `archive` — exactly these; config validation refuses any other).

#### `tables[]`

`{title, columns: [{key, label, type}], rows: [{key: value}]}`. Built from tool rows; values
are exactly what the tool returned (decimal strings for money and kW, ISO dates, UTC ISO
times). `type` ∈ `text | ref | money | kw | date | number | boolean` (`money` is a rupee
decimal string; `kw` a decimal string). Typical tables: Projects, Warranties ending, Leads,
Payments, Service requests, Associates, Approved makes and equipment, Matching customers,
`<Metric> by <group>` (`group`, `value`, `projects` — plus `associate_name` when grouped by
associate), and for one project `Project <ref>` (field/value) with Equipment / Stages /
Warranty / Service requests tables. Customer-written free text (notes, O&M status,
service-request descriptions, addresses) is not put in tables.

#### `searched[]`

`{tool, filters, summary}` per tool that ran — the filters actually applied (the "what I
searched" line). `summary` is display-ready English.

#### `suggestions[]`

Up to 3, deterministic from the result type: `{label, question?, path?}`. A `question` is a
follow-up to send back to `/ask` (same `conversation_id`); a `path` is a page link. Nova
Records never executes anything from a suggestion. None on `refused`.

#### Errors

| HTTP | `detail.code` | When |
|---|---|---|
| 401 | — | no staff credentials (associate tokens included) |
| 404 | `conversation_not_found` | `conversation_id` unknown or another employee's |
| 422 | `empty_question` | question blank after trimming |
| 422 | (FastAPI list) | length, unknown fields, bad `conversation_id` / `context` |
| 429 | `rate_limited` | over `limits.requests_per_minute` (header `Retry-After: 60`) |
| 429 | `daily_limit_reached` | over `limits.daily_questions` today (IST day) |
| 503 | `assistant_disabled` | the `nova_records` agent is switched off (governance kill switch) |
| 503 | `records_unavailable` | the agent run failed |

### GET /conversations

`{"items": [{conversation_id, title, turn_count, created_at, updated_at}]}` — the caller's own,
most recently used first, at most 50, within retention. `title` is the first question (≤120).

### GET /conversations/{conversation_id}

```json
{"conversation_id": "…", "title": "…", "turn_count": 2, "created_at": "…", "updated_at": "…",
 "employee_id": "emp-sales-1", "role": "sales",
 "turns": [{"turn_id": "…", "question": "…", "context": {"project_ref": "…"} | null,
            "status": "answered", "answer": "…", "citations": […], "tables": […], "searched": […],
            "suggestions": […], "generated_at": "…",
            "feedback": {"helpful": true | false | null, "note": "…" | null, "at": "…" | null}}]}
```

Turns read back exactly as they were shown (the stored response). Own only — anyone else's
is 404, except **Admin**, who may read any conversation for audit; that read is written to
the audit log (`nova_conversation_read_for_audit`, entity `nova_conversation`).

### POST /turns/{turn_id}/feedback

Request `{"helpful": true, "note": "optional, ≤1000"}` → `{turn_id, helpful, note, feedback_at}`.
Own turns only (404 otherwise). Sending again replaces the earlier feedback.

### POST /internal/check-nova-records-retention

Admin or `X-Internal-Key` (Cloud Scheduler `gridnova-check-nova-records-retention`, daily 03:30
IST). Deletes conversations last used more than `retention.days` ago, with their turns:
`{"status": "ok", "conversations_deleted", "turns_deleted", "retention_days", "cutoff"}`, or
`{"status": "skipped", "reason": "already_running"}`.

---

## Role scoping — who sees what

Every answer is built from tools that apply the rules below **before** anything reaches the
model or the response. Nothing is filtered after the fact.

| Area | Admin | Management | Accounts | Operations | Engineer | Sales |
|---|---|---|---|---|---|---|
| Archived projects | all | all | all | all | all | all |
| Live projects | all | all | all | in execution | all | first lead own or unassigned |
| Project fields | everything | everything | no contact details | technical, location, dates, customer name, service, associate | same as Operations | technical, state/district, dates only |
| Money (contract, received, outstanding; money totals) | yes | yes | yes | **no** | **no** | **no** |
| Payments status | yes | yes | yes | no | no | no |
| Leads | all | all | none | none | all, technical fields only | own + unassigned |
| Customer memory | yes | yes | no | no | no | own/unassigned customers (as `/memory/ask`) |
| Service requests | yes | yes | no | yes | yes | own projects |
| Associates | yes | yes | yes | yes | yes | no |
| Commission totals | yes | **no** | yes | no | no | no |
| List prices | yes | no | no | no | no | yes |
| Approved makes, company facts | yes | yes | yes | yes | yes | yes |
| Aadhaar, PAN, bank, ID documents | **never** — for any role, on any path |

Project fields follow `config/archive.yaml` (registry ARC-2, unconfirmed). A filter on a
field the role may not see is refused, not silently dropped (Sales cannot search by customer
name, block, location or associate; free-text search only reads fields the role sees).

Nova Records is deliberately **narrower** than two existing routes: Sales sees no money in
Nova Records (the payments routes show Sales its own leads' payments), and Management sees no
commission totals (the commission routes let Management read). Customer memory follows
`/memory/ask` exactly, so a Sales rep asking about their own customer may see that customer's
proposal values and phone number in the answer, as `/memory/ask` shows today.

## Data notes

- A live project is on record as a project once it has an onboarding, a recorded stage or an
  accepted proposal; before that it is a lead (every lead has an empty project row).
- Live: commissioning date = the UTC date `commissioned` was recorded; makes come from the
  installed-unit register (a project with two makes is grouped under the alphabetically first);
  contract value = the first lead's latest accepted proposal; received = the payment ledger;
  block = the onboarding's territory block, shown and filtered exactly like the archive's block
  (location_detail — not for Sales); a live project whose onboarding has no block shows `null`.
- Archived: received and outstanding are not on record. Archived projects are completed:
  "reached stage" matches commissioned (always), installed, net-meter/inspection and
  agreement signed through their dates.
- "Near" searches use coordinates in the question, or a place matched against project
  addresses, blocks and districts **on record** (projects there become the centres) — places
  are never geocoded and the model may not supply coordinates. Default radius 5 km.
- Totals can be grouped by state, district, block, scheme, month, year (commissioning date),
  associate, stage, module make or inverter make — each only for roles that see that field
  (package is not on record for completed projects and is refused).
- Totals and counts are SQL over the same filters as the rows; money to the paisa, kW to 3
  decimals.

## Limits

| Setting (`config/nova_records.yaml`) | Value | Registry |
|---|---|---|
| `limits.requests_per_minute` (per employee, per app instance) | 10 | NR-1 `[UNCONFIRMED]` tunable |
| `limits.daily_questions` (per employee per IST day, in the database) | 200 | NR-2 `[UNCONFIRMED]` tunable |
| `retention.days` | 365 | NR-3 `[UNCONFIRMED]` tunable |
| `limits.max_tool_calls` / `max_rows` | 3 / 50 | — |
| `limits.near_candidate_cap` / `near_default_km` | 2000 / 5 | — |
| `limits.tool_timeout_seconds` | 10 | — |
| `limits.history_turns` (earlier turns the planner sees) | 4 | — |

## Storage and audit

Tables (`0051_nova_records`): `nova_conversations` (random 32-hex id, owner, role, title,
turn count) and `nova_turns` (question, context, status, answer, the response as returned,
intent, planner `model|keyword`, composer `model|template`, tool calls with timings, refs,
verifier result, model, prompt/completion tokens, model calls, latency, harness run id,
feedback). Audit log: `nova_records_refused` (category, role), `records_answer_unverified`
(attempts and violations), `nova_conversation_read_for_audit`. Every run is also an
`agent_run` row for agent `nova_records` (governance: `app/governance/registry.py`
`NOVA_RECORDS` — capabilities `answer_staff_records_question`, `read_records`; every write,
send, approve, price, book, assign and record-payment action prohibited).
