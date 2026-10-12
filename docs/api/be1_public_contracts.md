# BE-1 — security, correctness and money-path changes (API contract)

Written from the code. Everything new or changed by BE-1; conventions as in
[README.md](README.md). **Breaking changes** are marked ⚠️ — the frontend must adopt them
(nothing is deployed in between).

---

## 1. Website chat — `POST /api/v1/chat/message` ⚠️

Public, no auth. Rate limit 20 requests / 60 s per client IP (429, plain `detail`).

A conversation is identified **only** by `session_token`. The browser keeps the token
from the previous reply and sends it back; it no longer sends or receives lead/customer
ids.

**Request body**

| Field | Type | Notes |
|---|---|---|
| `message` | string, required | > 2000 chars → 422 `{"detail": "Message exceeds 2000 character limit."}` |
| `session_token` | string ≤ 256, optional | from the previous reply. Missing, unknown or expired → a new conversation |
| `name` | string ≤ 200, optional | |
| `email` | string ≤ 254, optional | must look like an email (same check as `/public/estimate-request`), else 422 |
| `locale` | `en` \| `hi` \| `bn`, optional (BE-6) | reply language only. Case-insensitive; `hi-IN`/`bn_BD` are read as `hi`/`bn`; any other value, or none, is treated as `en` (never an error). It adds one `LANGUAGE:` line to the reply prompt; it never changes the session, identity, merge behaviour, the stage logic or the guardrails. Fixed system texts (e.g. the "something went wrong" apology and booking confirmations) stay in English |
| `lead_id`, `customer_id`, `source` | **deprecated, ignored** | accepted (and ignored) for one release so older widgets still validate |

**Response 200**

```json
{"reply": "…", "session_token": "<opaque, ~43 chars>", "is_qualified": false,
 "missing_fields": ["project_type", "…"], "meeting_booked": null}
```

⚠️ `lead_id` and `customer_id` are **no longer returned**. `session_token` is always
returned (the same one when a session was resumed, a new one when a conversation
started). `meeting_booked` is `{meeting_id, scheduled_at, meet_link}` or `null`.

**Behaviour**

- Only the SHA-256 of the token is stored. A token resumes exactly its own lead and
  customer; it expires 30 days after its last message (`config/sales_policy.yaml`
  `chat.session_ttl_days`, unconfirmed).
- New conversation: a new `website_chat` lead. The lead source can no longer be chosen by
  the client (it used to accept any `LeadSource`, including `associate`).
- An `email` that nobody has yet names the new customer. An `email` (in the body, or typed
  in a message) — or a phone typed in a message — that belongs to **another existing
  customer** is a claim, not a sign-in: the conversation keeps its own customer, the claim
  is kept on the lead's `collected_fields`, staff see a timeline entry
  `possible_returning_customer` ("Possible returning customer #id (unverified — matched
  on email|phone)"), and nothing of the other customer (profile, memories) is ever loaded
  into the reply. Merging customers is now a staff-only action.
- Behaviour change: a message **without** a token never joins an existing open lead (it
  used to fold into "the customer's open lead" by email).
- A reply that would repeat an email address, phone number or PIN code the customer did
  not type in this conversation (Grid Nova's own published contact details excepted) is
  replaced by the safe fallback reply.

**Errors:** 422 (validation, as above); 429 `"Rate limit exceeded. Please wait before
sending more messages."`; 429 `"Duplicate message detected. Please wait before
resending."` (the same message on the same session within 10 s).

---

## 2. Leads list — `GET /api/v1/dashboard/leads` ⚠️

Roles: every staff role, scoped as before (Sales: own + unassigned; Operations/Accounts:
none).

| Query | Notes |
|---|---|
| `source` | a `LeadSource`; unknown → 422 `{"detail": "Unknown lead source: 'x'"}` |
| `assigned` | `me` \| `unassigned` \| `any`; other → 422 (plain) |
| `q` | ≤ 100 chars. Customer name, email or phone (case-insensitive substring; the digits of `q` are also matched against the stored phone as written), or a lead id. **Engineer: lead id only** (a non-numeric `q` returns an empty page) |
| `page` | ≥ 1, default 1 |
| `page_size` | 1–200, default 25 |

⚠️ **Response** is now the standard envelope (it was a bare array):

```json
{"items": [{"id": 12, "customer_id": 7, "pipeline_stage": "qualifying", "lead_score": "low",
            "source": "website_chat", "created_at": "2026-10-10T04:00:00+00:00",
            "assigned_employee_id": null, "project_reference_id": "GNE-…"}],
 "page": 1, "page_size": 25, "total": 1}
```

Newest first. Every filter (scope, source, `q`) and the count run in SQL.

BE-6 added optional `stage`, `min_score`, `max_score`, `created_from`, `created_to` (also on the export) — see [staff_contracts.md](staff_contracts.md) §3.
`GET /dashboard/leads/export` is unchanged in shape; it now loads customers with the leads
and every CSV cell is formula-safe (see §9).

---

## 3. Proposal accept links ⚠️ (codes)

Public, rate-limited per IP (30 / 60 s). A proposal is **on offer** only while its status
is `sent`, no newer version exists on the lead, and no other proposal on the lead has been
accepted. The validity period is `config/sales_policy.yaml` `proposal_validity_days`
(30, unconfirmed) and is also what the PDF footer prints.

| Method | Path | 200 | Errors |
|---|---|---|---|
| GET | `/api/v1/proposals/accept/{token}` | `{proposal_id, version, scope[], price, valid_until, state (acceptable\|expired\|accepted), accepted_at, pdf_available}` | 404 `"Proposal not found."`; 409 `proposal_superseded` / `proposal_not_available` |
| POST | `/api/v1/proposals/accept/{token}` | `{status: "accepted", proposal_id, accepted_at, already_accepted}` | 404; 409 `proposal_superseded` / `proposal_not_available` / `proposal_expired` |
| GET | `/api/v1/public/proposals/{token}/pdf` | 302 to a signed link | 404; 409 `proposal_superseded` / `proposal_not_available`; 409 plain `"The proposal PDF is still being prepared."`; 503 |

- 409 bodies are structured: `{"detail": {"code": "...", "message": "..."}}`.
  ⚠️ An expired proposal used to return a plain string; it is now code `proposal_expired`.
- The customer's **accepted** proposal stays viewable (`state: "accepted"`), and a second
  accept is still `already_accepted: true` (one acceptance, one timeline event, one staff
  notification — also when two accepts race: the lead and proposal rows are locked).
- When staff send a proposal back for revision its token is revoked: the old link answers
  404.

**In-person acceptance** (`POST …/onboardings/{id}/proposal/in-person-acceptance`, associate
and staff): new 409 `proposal_superseded` when another proposal on the customer is
already accepted; same row lock as the online link.

**Request revision** (`POST /api/v1/dashboard/proposals/{id}/request-revision`): new 409
`{"code": "invalid_transition"}` when the proposal cannot move to `revision_requested`
(it accepted any status before — an accepted proposal could be sent back).
Allowed status moves: draft → pending_pricing_validation | pending_approval | approved |
revision_requested; pending_pricing_validation → pending_approval | approved |
revision_requested | draft; pending_approval → approved | revision_requested | draft;
approved → sent | revision_requested; sent → accepted | revision_requested;
revision_requested and accepted are final (a revision is a new version).

---

## 4. Public booking — `/api/v1/public/booking`

**`GET /availability`** — `duration_minutes` above the cap → 422
`{"code": "duration_too_long", "field": "duration_minutes", "message", "max_duration_minutes"}`.
The cap is `config/sales_policy.yaml` `booking.max_duration_minutes` (null = the default
meeting length, 30 minutes today; unconfirmed).

**`POST /api/v1/public/booking`** — body unchanged (`name`, `email`, `phone?`,
`scheduled_at`, `duration_minutes?`, `estimated_total?`, `sizing_method?`,
`sizing_value?`, `source?`), response unchanged. Checks, in order:

| Status | detail |
|---|---|
| 429 | plain — per-IP rate limit (5 / 60 s) |
| 422 | `{"code": "timezone_required", "field": "scheduled_at"}` — a datetime without an offset (it used to be taken as UTC) |
| 422 | plain `"scheduled_at must be in the future."` |
| 422 | `duration_too_long` (as above) |
| 422 | `{"code": "slot_not_offered", "field": "scheduled_at"}` — not one of the slots the availability query offers: business hours, the slot grid, within 30 days |
| 429 | `{"code": "daily_booking_limit"}` — this email already booked `booking.daily_cap_per_email` (3, unconfirmed) calls today (IST day) |
| 429 | plain — the same email within 60 s |
| 409 | `slot_unavailable` (with `alternatives`), unchanged |
| 503 | `no_calendars_connected` / `calendar_not_configured`, unchanged |

`estimated_total` (≤ 10,000,000,000) and `sizing_value` (≤ 100,000,000) must be finite.

**Known risk (unchanged behaviour, for Grid Nova to accept or not — registry BE1-5):** the
Google Calendar invite is sent (`sendUpdates="all"`) to the email the visitor typed, which
is not verified. Anyone can make Grid Nova's calendar email an invite to any address.

---

## 5. Estimate request — `POST /api/v1/public/estimate-request`

New optional, bounded fields (the website Solar Calculator's), stored on the lead's
`collected_fields` under the same names:

| Field | Type |
|---|---|
| `pin_code` | 6 digits |
| `discom` | string ≤ 100 |
| `monthly_units` | number ≥ 0, ≤ 100,000,000, finite |
| `sizing_method` | string ≤ 50 |
| `floors` | integer 1–200 (existing field, now bounded) |
| `access_difficulty` | string ≤ 50 |
| `shading` | string ≤ 50 |
| `subsidy_interest` | `"yes"` \| `"no"` |
| `indicative_total_inr` | number ≥ 0, ≤ 10,000,000,000, finite |

`average_bill` (≤ 100,000,000) and `roof_area` (≤ 10,000,000) must now be finite and
bounded. Response unchanged: `{lead_id, success}`.

---

## 6. Health and docs

- `GET /api/v1/health` → `{"status": "ok"}`. `environment` and `llm_mode` are included only
  with a valid `X-Internal-Key`.
- `/docs`, `/redoc` and `/openapi.json` are switched off when `ENVIRONMENT` is `staging` or
  `production`; `docs/api/openapi.json` (committed) is the contract.

---

## 7. Authentication

- `ENVIRONMENT` must be exactly `development`, `test`, `staging` or `production`; anything
  else stops the boot. The dev headers (`X-Employee-Id`/`X-Employee-Role`,
  `X-Associate-Id`) are accepted only in `development` and `test`; elsewhere 403
  `"Dev auth headers are only accepted in development and test."` (was "…not permitted in
  production." and applied only to the exact value `production`).
- A verified Clerk token whose `azp` claim is present must name one of `ALLOWED_ORIGINS`,
  else 401 `"Invalid or expired token."`.
- A verified Clerk token with no `public_metadata` → 403
  `{"code": "session_token_missing_claims", "message": "…"}` (on staff and associate routes).
  Clerk's session token must be customised to include `public_metadata`.

---

## 8. Staff and associate route changes

| Route | Change |
|---|---|
| `POST /api/v1/dashboard/memory/ask` | Sales: name matches are filtered to customers with a lead the caller may see BEFORE anything is listed or sent to the model (`candidates` never shows another rep's customer). A name that matches only out-of-scope customers resolves to nothing (`resolved: false`, no candidates). By id — and `GET /customers/{id}/memory` — a customer with no lead the caller may see (including a customer with no lead at all) is 403. |
| `GET /api/v1/associate/onboardings/{id}/files/{file_ref}`, `GET …/agreement`, `GET …/execution/files/{file_ref}` | Owner only while `territory_viewers_see_documents` is false (config/associate_onboarding.yaml, unconfirmed default): a territory Preferred Associate gets 403 `"Only the associate who onboarded this customer can open its documents."`. Every signed link issued (these routes, `formal-proposal`'s PDF, and the staff file/agreement routes) is audited (`onboarding_document` / `proposal_inputs_document`, `link_issued`). |
| `GET /api/v1/associate/onboardings/{id}/formal-proposal` | A territory viewer gets `total_value: null` and `pdf: null` (money is owner-only; the PDF follows the documents setting). |
| `GET /api/v1/associate/onboardings` and `…/{id}` | For a territory viewer `customer.mobile` is masked `"******1234"` and `customer.email` `"a***@example.com"`. |
| `POST /api/v1/proposal-inputs/{id}/upload` | The file is typed by its bytes (PDF, JPEG, PNG, WEBP), not the claimed Content-Type, and its filename sanitised. Rejections are now structured: 413/422 `{"code": "invalid_file" \| "heic_not_supported", "message"}`. `document_type` must be one of `electricity_bill, site_plan, govt_scheme_registration, other` or an onboarding checklist key, else 422 `{"code": "unknown_document_type", "field": "document_type"}`. |
| `POST /api/v1/dashboard/proposals/{id}/approve` | With `require_different_approver_for_all_leads: true` (config/sales_policy.yaml; default false, unconfirmed) the drafter cannot approve any proposal: 403 `"You drafted this proposal. A different employee must approve it."`. Associate-sourced proposals: unchanged. |
| `POST /api/v1/dashboard/associate-applications/{type}/{id}/approve` | A refused insert now says which conflict it was: 409 `invalid_transition` (approved meanwhile by someone else) or `seat_full`. The application row is locked during approval. |
| `POST /api/v1/dashboard/execution/submissions/{id}/verify` and `/reject` | The submission row is locked; a simultaneous second review gets 409 `submission_not_pending` (it could 500 before). |
| `GET /api/v1/dashboard/employees/me/calendar/connect` | The OAuth `state` is random, valid 10 minutes and single-use. With neither `CLERK_SECRET_KEY` nor `FERNET_ENCRYPTION_KEY` set → 503 `{"code": "oauth_state_not_configured"}` (it used to fall back to a fixed key). A replayed, expired or forged state ends the callback with `calendar=error&reason=invalid_state`. |
| `POST /api/v1/pricing/estimate`, `GET /api/v1/pricing/packages` | Rate-limited like `/instant-estimate` (20 / 60 s per IP, 429). `/packages` is built once per process. |
| `GET /api/v1/public/verify/{associate_id}` | Rate limit lowered to 10 / 60 s per IP. Still only `{name, photo_url, type, level, status}`; `photo_url` is a signed link valid `STORAGE_SIGNED_URL_TTL_SECONDS` (600 s default). |
| Payments queue and receipts | See [phase4a.md](phase4a.md) §3 (`q` search, `receipt_pending_stale`, void receipts, `release-held` `failed`, `receipt_voided`). |

---

## 9. Validation and CSV

- Public numeric fields reject `Infinity`, `NaN` and absurd values with 422
  (`/pricing/estimate` `size_kw` ≤ 1,000,000 and `monthly_bill_inr` ≤ 100,000,000;
  `/pricing/instant-estimate` units, bill and roof area; the estimate form; the booking
  extras; the staff site-assessment floats). The ceilings are the associate onboarding
  schema's existing ones (`app/api/input_bounds.py`) — sanity limits, not business rules.
  A 422 whose rejected input was non-finite now renders it as a string (`"inf"`) — before,
  the 422 itself crashed into a 500.
- Every CSV export cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is
  prefixed with `'` (numbers like `-1500.00` are left as numbers): leads and proposals
  exports now, as the payments exports already did.

---

## 10. PDFs

Every value in every PDF template is HTML-escaped, and every render uses a URL fetcher
that fetches only `data:` URIs (`app/pdf/fetcher.py`) — no file, http(s) or other URL is
ever read while rendering.
