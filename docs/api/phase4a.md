# Phase 4A — execution, serial register, payments, receipts, registry, home feed

Commit `5505685` (migrations 0036–0040). Changes made by the follow-ups are in
[phase4a_followups.md](phase4a_followups.md); site location, price list and HF-C changes
to the same routes are in their own files. Conventions (auth, 401/403, errors, money) are
in [README.md](README.md).

Role sets used below (from `tests/test_permission_matrix.py`):

| Name | Roles |
|---|---|
| EXECUTION_READERS | admin, management, sales, engineer, operations |
| EXECUTION_ACTORS | admin, operations |
| PAY_READERS | admin, management, accounts, sales |
| PAY_RECORDERS | admin, accounts, sales |
| PAY_PROCESSORS | admin, accounts |

Sales is further limited to its own or unassigned leads everywhere (`lead_scope`).
Associate routes: "owner" = the associate who onboarded the customer; "territory read" =
a Preferred Associate whose territory contains the site may read but never write; money
routes are owner only.

---

## 1. Execution (stages after `agreement_signed`)

A `project_executions` row exists per associate-sourced project from `agreement_signed`.
`execution_mode`: `associate` (the owner submits stages, Operations verifies) or
`grid_nova` (staff record stages directly). Execution stages, in order:
`feasibility_submitted, material_dispatched, installed, inspection_net_meter,
commissioned, subsidy_claimed, subsidy_received, handover_complete` — applicability per
project from `config/execution_payments.yaml` (E1). Before the agreement every execution
route answers 409 `execution_not_started`.

### Associate (`/api/v1/associate/onboardings/{onboarding_id}/execution`)

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `` | owner, territory read | execution status (below) |
| POST | `/files` | owner | multipart, 201 — one photo or document |
| GET | `/files/{file_ref}` | owner, territory read | `{url, expires_at, filename, file_ref}` |
| POST | `/submissions` | owner | 201 — submit a stage for checking |
| GET | `/submissions` | owner, territory read | `{items: [submission]}` newest first |
| GET | `/units` | owner, territory read | register block (section 2) |
| GET | `/units/check-serial?serial=` | owner, territory read | duplicate check |
| POST | `/units` | owner | 201 |
| POST | `/units/bulk` | owner | 201, all or nothing |
| PATCH | `/units/{unit_id}` | owner | |
| POST | `/units/{unit_id}/void` | owner | |

`POST /files` (multipart): `kind` (`photo`|`document`), `file`, `photo_slot` (from
`photo_slots`) or `document_type` (from `document_types`), `latitude`, `longitude`
(both or neither), `captured_at` (≤40), `capture_method` (`camera`|`gallery`),
`exif_taken_at_client` (≤40). Camera photos need GPS and `captured_at`
(422 `location_required`); gallery photos are flagged (`gallery_upload`, `no_metadata`,
`old_photo`); identical bytes on another project add `duplicate_photo`. Refused on a
`grid_nova`-mode project: 409 `execution_mode_grid_nova`.

`POST /submissions` body: `{stage, text?: {field: value ≤500}, note? ≤2000}`. Returns the
submission. Errors: `not_an_execution_stage`, `stage_not_applicable`,
`stage_already_recorded`, `stage_out_of_order`, `submission_pending`, `evidence_missing`
(with what is missing), `text_too_long`, `execution_mode_grid_nova`.

**Submission object:** `{submission_id, onboarding_id, stage, stage_label, status
(pending|verified|rejected), submitted_by, submitted_at, evidence: {text, note,
documents: {type: file}, photos: {slot: file}, register_verified_at}, reviewed_by,
reviewed_at, review_note}`.

**File object:** `{file_ref, filename, received_at, uploaded_by}` plus, for photos,
`capture_method, location_verified, flags[], latitude, longitude, client_captured_at,
exif_taken_at`.

**Execution status** (associate GET and staff detail):

```
{onboarding_id, project_id, execution_mode, started_at, register_verified_at,
 current_stage, next_stage, next_stage_is_execution,
 aging: {days_in_stage, stuck_threshold_days, is_stuck},
 pending_submission: submission|null,
 stages: [{stage, label, recorded, recorded_at, recorded_by, via,
           submission: submission|null,           # latest, while not recorded
           evidence_required: {text[], documents[], optional_documents[], photos[], register_verified},
           blockers: [{code, message}]}],          # e.g. payment_gate, dcr_mismatch
 photo_slots[], document_types[],
 files: {photos: {slot: file}, documents: {type: file}},
 flags[],                                          # same as register.flags
 register: {...section 2...}}
```

It carries no money. Staff detail adds `customer` (name, project reference, site,
associate; mobile except for engineers), `can_verify`, `can_record`.

### Staff (`/api/v1/dashboard`)

| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/execution` | EXECUTION_READERS | queue; filters `needs_attention, stuck, stage, mode, page, page_size` |
| GET | `/onboardings/{id}/execution` | EXECUTION_READERS | status + customer block |
| GET | `/onboardings/{id}/execution/files/{file_ref}` | EXECUTION_READERS | signed link |
| POST | `/onboardings/{id}/execution/files` | EXECUTION_ACTORS | multipart, 201 — `grid_nova` mode only (409 `execution_mode_associate`) |
| POST | `/execution/submissions/{submission_id}/verify` | EXECUTION_ACTORS | body `{note?}` → staff detail |
| POST | `/execution/submissions/{submission_id}/reject` | EXECUTION_ACTORS | body `{note}` (required: 422 `note_required`) |
| POST | `/onboardings/{id}/execution/{stage}` | EXECUTION_ACTORS | record directly (grid_nova mode; see follow-ups for the admin override) |
| POST | `/internal/check-execution-alerts` | admin or `INTERNAL_API_KEY` | stuck-project emails |

Verify/reject errors: `submission_not_pending`. Recording: `unknown_stage`,
`stage_not_applicable`, `stage_out_of_order`, `stage_already_recorded`,
`evidence_missing`, `execution_mode_associate`.

**Queue row:** `{onboarding_id, project_id, project_reference_id, customer_name,
execution_mode, current_stage, next_stage, next_stage_label, days_in_stage,
stuck_threshold_days, is_stuck, pending_submission: {submission_id, stage,
submitted_at}|null, flags[], needs_attention, attention_reasons[], started_at}`.
`attention_reasons` = `pending_verification`, `stuck`, plus every staff flag.

---

## 2. Serial register (installed equipment)

Staff routes: `/api/v1/dashboard/onboardings/{onboarding_id}/execution/...` — read
EXECUTION_READERS, change EXECUTION_ACTORS, DCR override admin only.

| Method | Path | Notes |
|---|---|---|
| GET | `/units` | register block |
| GET | `/units/check-serial?serial=` | duplicate check |
| POST | `/units` (`?reason=` after verification) | 201 `{unit, warnings, register}` |
| POST | `/units/bulk` | 201 `{units, warnings, register}` |
| PATCH | `/units/{unit_id}` | `{unit, warnings, register}` |
| POST | `/units/{unit_id}/void` | body `{reason}` → `{unit, register}` |
| POST | `/register/verify` | Operations/Admin confirm; locks the register → staff detail |
| POST | `/dcr-override` | admin, body `{reason}` → staff detail |

**Unit body** (`UnitIn`): `category` (`module`|`inverter`|`other`), `serial_number` (≤80),
`equipment_id`, `rating_wp` (modules, required), `rating_kw` (inverters, required),
`is_dcr` (modules, required; must agree with the catalogue item), `warranty_start`,
`warranty_end` (both or neither, YYYY-MM-DD). PATCH takes the same fields, all optional,
plus `reason`. Bulk: `{units: [UnitIn], reason?}` (≤ `bulk_max_rows`). HF-C adds
`brand_as_printed` / `model_as_printed` ([hf_c_equipment.md](hf_c_equipment.md)).

Serials are unique across the whole system after normalisation (NFKC, zero-width characters
and all whitespace removed, upper case; hyphens are kept) — the database refuses a duplicate.

**Unit object:** `{unit_id, category, serial_number, serial_normalized,
equipment: {id, brand, model_name, is_dcr_compliant, is_placeholder}|null, rating_wp,
rating_kw, is_dcr, warranty_start, warranty_end, status (entered|verified|voided),
created_by, created_at, verified_by, verified_at, void_reason}`; staff only adds flags
(see follow-ups, PM-A, HF-C).

**Register block:** `{verified_at, verified_by, units[], totals: {modules, inverters,
other, inverter_kw}, capacity: {installed_module_kw, requested_kw, tolerance_percent,
mismatch}, dcr: {pm_surya_ghar, mismatch, override: {reason, by, at}|null}, flags[]}`.
Flags: `dcr_mismatch` (a non-DCR module on a PM Surya Ghar project, until an admin
override), `capacity_mismatch`; staff-only flags are listed in later files.

**check-serial:** `{serial_normalized, available: true, held_by: null}` or
`{serial_normalized, available: false, on_this_project, message?, held_by}` — staff see
`{onboarding_id, project_reference_id}`; an associate sees `{onboarding_id}` only when
they can view that customer, else `null`.

Errors: `invalid_category`, `invalid_serial`, `unknown_equipment`, `equipment_required`,
`equipment_category_mismatch`, `rating_required`, `dcr_required`, `dcr_mismatch`,
`invalid_number`, `invalid_date`, `warranty_pair`, `warranty_order`,
`warranty_too_long`, `no_rows`, `too_many_rows`, `register_rows_invalid` (422, or 409
when every row is a duplicate; `errors: [{row, code, field, message, held_by?}]`),
`duplicate_serial` (409, `held_by`), `unit_voided`, `register_locked` (after verification
only an admin with a reason may change it), `register_already_verified`,
`register_incomplete` (needs ≥1 module and ≥1 inverter; `missing[]`),
`override_exists`, `reason_required`. The `installed` stage needs a verified register.
A PM Surya Ghar project with `dcr_mismatch` cannot record `subsidy_claimed` until the
override.

---

## 3. Payments (ledger)

Modes: `cheque, demand_draft, neft, rtgs, imps, upi` (enabled set in config; no cash).
Statuses: `recorded → (deposited) → verified`, or `dishonoured`, `cancelled`. Verified
money is written to an append-only ledger (DB triggers refuse UPDATE/DELETE). Milestone
due status is computed against the cumulative verified total.

### Associate (owner only — never a territory Preferred Associate)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/associate/payments/instructions` | Grid Nova's account for "pay only here" |
| GET | `/api/v1/associate/onboardings/{id}/payments` | overview |
| POST | `/api/v1/associate/onboardings/{id}/payments` | 201 `{payment, ...overview}` |
| POST | `/api/v1/associate/onboardings/{id}/payments/{pid}/proof` | multipart `file` (JPEG/PNG/WEBP/PDF ≤10 MB) |
| GET | `/api/v1/associate/onboardings/{id}/payments/{pid}/proof` | signed link |
| POST | `/api/v1/associate/onboardings/{id}/payments/{pid}/cancel` | body `{reason}`, own `recorded` payments only |

**Instructions:** while the bank details are placeholders →
`{available: false, reason: "placeholder_content", account: null, message_key:
"payments.instructions.not_ready"}`; otherwise `{available: true, reason: null,
message_key: "payments.instructions.pay_only_here", account: {payee, bank_name,
account_number, ifsc}, cash_accepted: false}`.

**Overview:** `{onboarding_id, totals, milestones[], payments[] (associate shape),
enabled_modes[], proof_required}`.

**Payment body** (`PaymentIn`): `amount` (decimal string or number), `mode`,
`reference` (≤100; cheque/DD number or UTR), `bank_name` (≤120), `instrument_date`
(YYYY-MM-DD; cheque/DD), `payer_name` (≤200), `milestone_id` (label only).

### Staff (`/api/v1/dashboard`)

| Method | Path | Roles |
|---|---|---|
| GET | `/payments` (filters `status, mode, needs_attention, onboarding_id, date_from, date_to, q, page, page_size`) | PAY_READERS |
| GET | `/payment-milestones` (`needs_attention, page, page_size`) | PAY_READERS |
| GET | `/payments/{pid}` | PAY_READERS |
| GET / POST | `/payments/{pid}/proof` | PAY_READERS / PAY_RECORDERS |
| POST | `/payments/{pid}/deposit` | PAY_PROCESSORS |
| POST | `/payments/{pid}/verify` (body `{reason?}`, see follow-ups) | PAY_PROCESSORS |
| POST | `/payments/{pid}/dishonour` `{reason}` | PAY_PROCESSORS |
| POST | `/payments/{pid}/cancel` `{reason}` | PAY_PROCESSORS |
| POST | `/payments/{pid}/receipt/resend` | PAY_PROCESSORS |
| POST | `/payment-receipts/release-held` | PAY_PROCESSORS |
| GET / POST | `/onboardings/{id}/payments` | PAY_READERS / PAY_RECORDERS |
| GET / PUT | `/onboardings/{id}/payment-schedule` | PAY_READERS / PAY_PROCESSORS |
| GET | `/exports/payments.csv`, `/exports/receipts.csv` (`date_from, date_to`), `/exports/ledger.csv` | PAY_PROCESSORS |
| POST | `/internal/check-payment-alerts` | admin or `INTERNAL_API_KEY` |
| GET | `/work-queues` | every staff role (areas it cannot see are absent) |

Operations and engineers get 403 on every payment route.

**`q` on `/payments`** (BE-1, ≤ 100 chars): matches the customer's name (case-insensitive,
substring), the payment reference (normalised exactly as at entry: spaces removed,
upper-cased), the project reference (`GNE-…`) or the receipt number (`GNR-…`). It is
applied inside the caller's scope (Sales: own or unassigned leads) and before paging;
`total` is the filtered count. Paging and the count run in SQL; with `needs_attention`
set (computed per payment) the filtered rows are checked in Python first.
`/payment-milestones` pages in SQL the same way when `needs_attention` is absent.

**Payment object (staff):** `{payment_id, onboarding_id, milestone_id,
previous_payment_id, amount, mode, reference, bank_name, instrument_date, payer_name,
status, has_proof, proof_filename, recorded_by, recorded_at, deposited_at, verified_at,
dishonoured_at, dishonour_reason, cancelled_at, cancel_reason, receipt|null,
deposited_by, verified_by, dishonoured_by, cancelled_by, needs_attention,
attention_reasons[]}`. Associates get the same without the `*_by` fields and attention.
`attention_reasons`: `awaiting_verification`, `cheque_hold_exceeded`, `proof_missing`,
`receipt_held`, `receipt_pending_email`, `receipt_failed`, `receipt_pending_stale` (BE-1: still `pending` more than
`payments.receipt.pending_alert_minutes` after numbering — 60, unconfirmed). Detail adds `project: {onboarding_id,
customer_name, customer_email, project_reference_id, current_stage, accepted_total,
paid_verified, awaiting_verification, balance}`. HF-C adds `customer_name` and
`project_reference_id` to every list row.

**Milestone object:** `{milestone_id, number, label, percent, amount_due, paid_toward,
outstanding, trigger, due_status (not_due|due|partially_paid|paid), due_since, overdue}`;
the staff list adds `onboarding_id, needs_attention` (= overdue).

**Totals:** `{accepted_total, paid_verified, awaiting_verification, balance}`.

**Schedule:** created from the template the first time it is needed after acceptance.
GET/PUT return `{onboarding_id, totals, milestones[]}`. PUT body `{milestones: [{label
≤200, percent, trigger (a project stage)}]}` — percentages sum to 100; 409 `schedule_locked` once any
live payment has been recorded.

**Receipt object:** `{receipt_number, email_status (pending|sent|skipped|held|failed),
emailed_at, pdf_available, receipt_pending_email, held, voided, url}`; staff add
`email_skipped_reason, held_reason, render_error, voided_at, void_reason`. A receipt with
placeholder content is held, never sent — and (BE-1) never rendered while held: it is
rendered on release. `url` is null while held or void. Dishonouring a verified payment
voids its receipt (`voided: true`; any later render is watermarked VOID). Public download:
`GET /api/v1/public/receipts/{token}/pdf` → 302 to a signed link (404 unknown, not yet
rendered, held, or void; 503 storage unavailable).

**`POST /payment-receipts/release-held`** → `{released, failed, still_held, reason}`. Each
released receipt is rendered and delivered on its own; one that fails is marked
`email_status: failed` (`email_skipped_reason: release_failed: <error>`, or `voided`) and
the rest carry on. Before BE-1 the body had no `failed` key.

**Work queues:** `{execution?: {pending_stage_verifications, stuck_projects,
dcr_mismatch}, payments?: {awaiting_verification, dishonoured, receipts_held,
receipts_without_email, overdue_milestones}, ...}`.

Payment errors: `invalid_amount`, `mode_not_enabled`, `reference_required`,
`instrument_details_required`, `not_an_instrument`, `invalid_date`,
`duplicate_payment_reference`, `exceeds_project_total`, `no_accepted_total`,
`not_accepted`, `unknown_milestone`, `proof_required`, `invalid_transition`,
`payment_not_open`, `not_yours`, `reason_required`, `invalid_schedule`,
`schedule_locked`, `self_verification` (follow-ups), `no_receipt`, `resend_too_soon`,
`receipt_voided` (BE-1: resend of a dishonoured payment's receipt), `storage_not_configured` (503).

---

## 4. Placeholder registry (staff)

| Method | Path | Roles |
|---|---|---|
| GET | `/api/v1/dashboard/placeholders` | admin, management |
| POST | `/api/v1/dashboard/placeholders/{entry_id}/resolve` `{note}` | admin — `manual` entries only |

List: `{summary: {total, unresolved, by_blocking: {blocks_launch|blocks_feature|tunable:
{total, unresolved}}}, areas: [{area, items: [{id, area, location, description,
dummy_value, needed_from_grid_nova, blocking, probe, resolved, status, status_note,
probe_error, resolution}]}]}` (`status`/`status_note` added by PM-A). Resolve errors:
`not_manual` (409), `already_resolved` (409), `note_required` (422), 404.

## 5. Associate home feed

`GET /api/v1/associate/home` (any associate; their OWN customers only):
`{items: [{kind, message_key, onboarding_id, customer_name, priority, params}],
totals: {customers, awaiting_grid_nova, needing_action}}`, sorted by priority. Kinds
(`message_key` = `todo.<kind>` unless noted): `stage_rejected` (1), `payment_due`
(`todo.payment_overdue` priority 1 when overdue, else `todo.payment_due` 2),
`documents_missing` (1), `site_location_missing` / `site_location_unconfirmed` (1, 3C-A),
`ready_to_submit` (1), `survey_revisit_needed` / `survey_resign_needed` (1, S1 — see
site_survey.md §8), `upload_signed_agreement` (2), `formal_proposal_ready` (2),
`execution_next_step` (2), `receipt_available` (3, home feed only). List items in
`GET /associate/onboardings` carry `progress_summary: {current_stage, stage_label,
next_stage, next_stage_label, needs_action, action_kinds[], blocked, site_location}`.

## Addendum
`GET /api/v1/associate/onboardings/{id}/execution` also returns `can_submit` (bool): true only for the owning associate when `execution_mode` is `associate`; the associate screens use it to enable entry, edit and void.
