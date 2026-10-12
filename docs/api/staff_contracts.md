# Staff contracts (BE-6)

Response models for the staff lead and customer-memory endpoints, written from the code
(`app/api/staff_schemas.py`, `app/api/routes/dashboard.py`). The models describe what the
routes already returned — **no behaviour changed** except the new optional lead filters (§3).
Base path `/api/v1/dashboard`. Times are UTC ISO 8601 strings.

**Error format.** These routes still use a **plain-string** `detail` for role refusals,
404s, 409s and most 422s (they predate the structured `{code,message}` format). The strings
below are exact and are what the frontend can match on. Only the new lead-filter errors
(§3) are structured: `{"detail": {"code", "message", "field"}}`. Pydantic body/query
failures are FastAPI's standard 422 (`detail` is a list). Unauthenticated: 401.

**Visibility rule** (one rule, `app/auth/scoping.py`): Admin, Management, Engineer see
every lead (Engineer through technical-only views); **Sales sees leads assigned to them or
unassigned**; Operations and Accounts see none. A single lead outside scope → 403
`"This lead is not assigned to you."`.

---

## 1. Meetings, memory and reflection

### `POST /leads/{lead_id}/schedule-meeting`
Roles: Admin, Sales (else 403 `"Your role cannot schedule meetings."`). Sales: lead must be own or unassigned.
Body `{scheduled_at: datetime, duration_minutes?: int}` (naive datetimes are read as UTC; duration defaults to config `scheduling_default_duration_minutes`).
Books on the caller's connected Google Calendar, else the lead's assignee's.

200 `ScheduleMeetingResponse`:
```json
{"meeting_id": 5, "lead_id": 12, "employee_id": "emp-1", "scheduled_at": "2026-10-20T05:00:00+00:00",
 "duration_minutes": 30, "meet_link": "https://meet.google.com/…" , "status": "scheduled"}
```
`meet_link` may be null; `status` ∈ `scheduled|cancelled|completed`; `employee_id` is whose calendar was used.

Errors: 403 role / 403 `"This lead is not assigned to you."`; 404 `"Lead not found"`;
409 `"No connected Google Calendar available to book this meeting on."`; 409 slot-unavailable
(message from the booking layer); 409 no-connected-calendar (message from availability layer);
503 Google Calendar not configured (message from the layer).

### `GET /customers/{customer_id}/memory?kind=&limit=`
Roles: Admin, Management, Sales (Engineer/Operations/Accounts → 403 `"Your role cannot query customer memory."`).
`kind`: a memory kind (`episode|preference|objection|commitment|outcome|…` — the `MemoryKind` enum); `limit` 1–500, default 100.

200 `CustomerMemoryResponse`:
```json
{"customer": {"customer_id": 7, "name": "Asha", "email": "a@x.com", "company": null},
 "profile": {"summary": "…", "traits": [], "open_questions": [], "agent_notes": null,
             "next_best_action": null, "last_reflected_at": "…", "reflection_count": 2},
 "memory_count": 14,
 "memories": [{"id": 1, "kind": "preference", "source": "reflection", "summary": "…", "detail": null,
               "tags": [], "salience": 3, "lead_id": 12, "occurred_at": "…", "created_by": "sales_agent"}]}
```
`profile` is null if none yet. `memory_count` is the customer's total, `memories` the filtered/limited list.
Errors: 403 role; 404 `"Customer not found"`; 403 `"This customer's leads are assigned to another rep."` (**Sales only**: a customer with no lead the caller may see — including a customer with no lead at all); 422 `"Unknown memory kind: 'x'"`.

### `POST /memory/ask`
Roles: as above. Body `{question: 1–500 chars, customer_id?: int}`. Read-only; the answer comes from stored memory only.
200 `MemoryAskResponse`:
```json
{"answer": "…", "resolved": true, "customer_id": 7, "candidates": [], "citations": [
  {"id": 1, "kind": "preference", "summary": "…", "occurred_at": "…"}], "retrieval_mode": "semantic"}
```
Not resolved / ambiguous: `resolved:false`, `customer_id:null`, `citations:[]`, and `candidates` = `[{customer_id, name, email, company}]` when several match. `retrieval_mode` is a string or null (null when nothing was retrieved). `answer` is always English prose.
Scoping (Sales): with `customer_id`, a customer with no visible lead → 403 (above). Without it, name matches are **filtered to visible customers before anything is listed or sent to the model**, so `candidates` never reveals another rep's customer; a name that only matches out-of-scope customers resolves to nothing; a resolved customer is re-checked (403) as a backstop.
Errors: 403 role; 404 `"Customer not found"` (given `customer_id`); 403 scope; 503 with the agent's error text when the recall run fails.

### `POST /leads/{lead_id}/reflect`
Roles: Admin, Management, Sales (403 `"Your role cannot trigger reflection."`). No body. Synchronous.
200 `ReflectResponse` (always, once access is granted — reflection never raises):
```json
{"trigger": "manual", "lead_id": 12, "memories_written": 3, "skipped": null}
```
`skipped` is null when it ran, else a human string: `"lead not found"`, `"no new messages since last reflection"`, `"reflection returned nothing"`, or `"error: …"`. Treat any non-null `skipped` as "nothing written".
Errors: 404 `"Lead not found"`; 403 `"This customer's leads are assigned to another rep."` (Sales; scope is checked on the lead's customer).

---

## 2. Lead endpoints

### `GET /leads` — `LeadListResponse`
```json
{"items": [{"id": 12, "customer_id": 7, "pipeline_stage": "qualified", "lead_score": "high",
            "source": "referral", "created_at": "…", "assigned_employee_id": null,
            "project_reference_id": "GNE-…"}], "page": 1, "page_size": 25, "total": 1}
```
`lead_score` is `high|medium|low` or null. Other params: `source`, `assigned` (`me|unassigned|any`), `q`, `page`, `page_size` (≤200) — see be1_public_contracts.md §2.

### `GET /leads/{lead_id}` — `LeadDetailResponse` | `EngineerLeadDetailResponse`
Non-Engineer: `{lead_id, customer:{id,name,email}|null, pipeline_stage, lead_score, source, assigned_employee_id, project:{id,reference_id,project_type,project_family,state,location,capacity_kw,roof_type,monthly_electricity_bill,budget,timeline,created_at}|null, collected_fields, proposals:[{id,version,status,total_value}], timeline:[{event_type,description,created_at}], messages:[{role,content,created_at}]}`.
**Engineer** gets only `{lead_id, pipeline_stage, collected_fields}` (technical fields: `project_type, capacity_kw, location, roof_type`). Distinguish by the absence of `customer`.
Errors: 404 `"Lead not found"`; 403 scope.

### `PATCH /leads/{lead_id}/fields` — `LeadFieldsUpdateResponse`
Roles Admin, Sales (403 `"Your role cannot edit lead fields."`). Body `{fields: {...}}`.
200 `{lead_id, collected_fields, pipeline_stage, lead_score, is_qualified, missing_fields}`.
Errors: 422 `"fields must not be empty."`, 422 `"Unknown field(s): a, b"`, 404, 403 scope.

### `PATCH /leads/{lead_id}/stage` — `LeadStageUpdateResponse`
Roles Admin, Management, Sales (403 `"Your role cannot change a lead's stage."`). Body `{stage, reason?≤1000}`.
200 `{lead_id, previous_stage, pipeline_stage, reason}`.
Errors: 404; 403 scope; 409 `"Lead is already in stage 'x'."`; 422 `"A reason is required to mark a lead as lost."`; 422 `"A reason is required to re-open a lead that is 'won'."` (or `'lost'`).

### `PATCH /leads/{lead_id}/assign` — `LeadAssignResponse`
Roles Admin, Management (403 `"Only Admin or Management can assign leads."`). Body `{employee_id}`.
200 `{lead_id, previous_assigned_employee_id, assigned_employee_id}`.
Errors: 404 `"Lead not found"`; 422 `"Unknown employee: 'x'"`; 422 `"That employee is deactivated."`; 422 `"Leads can only be assigned to Admin or Sales employees."`; 409 `"The lead is already assigned to that employee."`.

### `GET /leads/export` — CSV
Admin, Management only (403 `"Only Admin or Management can export leads."`). `text/csv` attachment, header
`lead_id,customer_name,customer_email,source,pipeline_stage,lead_score,assigned_employee_id,created_at`. Every cell goes through the formula-safe helper. Accepts `source` and the §3 filters.

---

## 3. Lead filters (new) — `GET /leads` and `GET /leads/export`

All optional, combined with AND, applied **inside the role scope and before pagination** (so `total` is the filtered count).

| Query | Meaning |
|---|---|
| `stage` | one or more pipeline stages: repeat (`?stage=new&stage=won`) or comma-separate (`?stage=new,won`). Values: `new, qualifying, qualified, proposal_draft, proposal_sent, negotiation, won, lost` |
| `min_score`, `max_score` | `low` < `medium` < `high`, inclusive. Scores are labels, not numbers. Either may be used alone. **A lead with no score never matches a score filter** |
| `created_from`, `created_to` | `YYYY-MM-DD`, **IST calendar days, inclusive** (`created_to=2026-09-03` includes everything up to 23:59:59 IST on the 3rd) |

Structured 422s `{"detail": {"code", "message", "field"}}`:

| code | field | when |
|---|---|---|
| `unknown_stage` | `stage` | a value is not a pipeline stage |
| `unknown_score` | `min_score` / `max_score` | not low/medium/high |
| `invalid_score_range` | `min_score` | min above max |
| `invalid_date` | `created_from` / `created_to` | not `YYYY-MM-DD` |
| `invalid_date_range` | `created_from` | from after to |

The export with the same filters returns exactly the rows the table shows (it has no `q`/`assigned`/paging).
