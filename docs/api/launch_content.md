# BE-22 — Launch content: approved wording, the gates, pricing without ₹0, TDS review

Written from the code. When code and this file disagree, the code wins — fix the file.

## 1. What changed, in one paragraph

Nine texts that customers or associates read are now drafted wording in the config files
(no `[PLACEHOLDER]` left). **None of them is used until an Admin approves it in the
dashboard, exactly as written.** The approval stores the SHA-256 of the text. If anyone
edits the text in git, the approval **lapses** and every document that uses it is held
again. Nothing is sent or printed from unapproved wording. Two texts (the proposal terms
and the customer agreement) need a "Reviewed by Grid Nova's legal adviser" tick.

| Entry | Text | File → key | Legal tick |
|---|---|---|---|
| PRO-2 | Proposal terms and conditions | `config/proposal_content.yaml` → `base_sections.standard_terms` | **yes** |
| PRO-7 | Ground-mounted scope | `… → sections_library.utility_scale_scope` | no |
| PRO-8 | Government / institutional scope | `… → sections_library.government_scope` | no |
| PRO-10 | PM-KUSUM note | `… → sections_library.pm_kusum_note` | no |
| PRO-11 | Government compliance note | `… → sections_library.government_compliance_note` | no |
| PRO-12 | Generic scope | `… → sections_library.fallback_generic_scope` | no |
| ONB-5 | Customer agreement | `config/associate_onboarding.yaml` → `agreement.body` | **yes** |
| P6 | Receipt heading and footer | `config/execution_payments.yaml` → `payments.receipt.wording` | no |
| HW-OM | O&M after the free first year | `config/handover_warranty.yaml` → `warranty.om_after_free_terms` | no |

The hash is over the normalised text: line endings `\n`, trimmed. The receipt wording is
hashed as two lines, `heading: …` and `footer: …`.

## 2. Routes (Admin only; Management may read the list)

### `GET /api/v1/dashboard/placeholders`

Unchanged shape, plus these fields on **every** item (null where they do not apply):

| Field | Type | Meaning |
|---|---|---|
| `probe` | string | `approved_text` for the nine texts above |
| `legal_review_required` | bool | the legal tick is needed to approve |
| `current_text` | string \| null | the text as it stands now (approved_text only) |
| `current_sha256` | string \| null | its hash — send this back to approve |
| `approval` | object \| null | `{by, at, note, legal_review_confirmed, content_sha256}` of the standing approval (also present when `lapsed`) |
| `approval_state` | `approved` \| `not_approved` \| `lapsed` \| null | `lapsed` = approved earlier, but the text has changed since |

`resolved` is true for an approved_text entry only when `approval_state` is `approved`.

### `POST /api/v1/dashboard/placeholders/{entry_id}/approve`

Body: `{"content_sha256": "<64 hex>", "note": "optional", "legal_review_confirmed": false}`

| Status | `detail.code` | When |
|---|---|---|
| 200 | — | the item, now `approval_state: "approved"` |
| 403 | — | not an Admin (`placeholders.resolve`) |
| 404 | — | no such entry |
| 409 | `not_approved_text` | the entry is not wording |
| 409 | `still_placeholder` | the text still has a marker |
| 409 | `text_changed` | the hash is not the current text's (`detail.current_sha256` given): reload and read again |
| 422 | `legal_review_required` | PRO-2 / ONB-5 without `legal_review_confirmed: true` |
| 429 | `rate_limited` | more than 30 approvals/revocations a minute by this Admin |

Approving again after a lapse is a new approval of the new text.

### `POST /api/v1/dashboard/placeholders/{entry_id}/revoke`

Body: `{"note": "why"}` (required). 200 → the item, `approval_state: "not_approved"`.
409 `not_approved` when nothing is approved. Same 403/404/409/429 as approve.

Every approval, revocation and lapse is on record: approvals and revocations are
append-only rows in `placeholder_resolutions` (`action` = `approved` / `revoked`) plus an
audit log entry (`wording_approved` / `wording_revoked`, `entity_type: placeholder`).
A lapse needs no row: it is the text in git no longer matching the approved hash, and git
history shows who changed it. `POST …/{id}/resolve` refuses wording entries
(`409 not_manual`).

## 3. The gates — text → where used → what happens while unapproved

| Text | Where it is used | While not approved |
|---|---|---|
| PRO-2, 7, 8, 10, 11, 12 | Composed proposal sections (each section carries `source_entry`) | Send (`POST /dashboard/proposals/{id}/send`) and in-person acceptance answer **409 `unapproved_sections`** with `unapproved_sections: [section ids]`. The public accept page, accept and PDF download answer **409 `proposal_not_available`** (the customer is never told why or shown the text). |
| PRO-7, 8, 10, 11, 12 | BE-13 documented proposals and Proposal Studio of an uncovered segment (government, utility, agricultural, fallback) | The segment text counts as missing: approval/send **409 `missing_required`** (`field: segment_text`); Studio approve/issue **409 `content_not_ready`**. |
| PRO-12 | A proposal that fell back to the generic scope (`used_fallback_template: true`) | Held **even when approved**: **409 `manual_review_required`**. Staff set the segment / consumer category so the right scope applies. |
| ONB-5 | Customer agreement PDF | `POST /dashboard/onboardings/{id}/agreement` answers **409 `agreement_wording_not_approved`** (no PDF is rendered). An unsigned agreement printed from other wording has no download link (`links: {}`, `needs_regeneration: true`), and its signed copy is refused (**409 `agreement_wording_not_approved`**). Signed agreements stay available. Both agreement GETs add `wording_approved` (and `needs_regeneration` once generated). |
| P6 | Receipt PDF, email, public link | Receipt **held**, `held_reason: "wording_not_approved"` (the existing `placeholder_content` holds stay first). `POST /dashboard/payment-receipts/release-held` answers `reason: "wording_not_approved"` until approved, then releases them as before. The associate "pay only here" bank details are **not** withheld for this reason. |
| HW-OM | Handover pack PDF, email, public link; `warranty.om.terms_after_free_period` | Pack **held**, `held_reason: "wording_not_approved"`. `…/handover/link` and regenerate answer **409 `handover_pdf_held`**. `terms_after_free_period` is `null`. Once approved, staff send the link as before (it renders and emails the pack). |

Every gate checks the exact text it prints. A proposal section is checked against the
text stored in that proposal, so a proposal drafted with older wording stays held.

Boot (staging/production): a warning names the drafted-but-unapproved entries. It is a
warning, not an error, because the holds above keep the system safe.

## 4. Pricing: "Price to be set by Grid Nova" — never ₹0

- `config/proposal_content.yaml` tiers are `rate_per_kw: null`, meaning no automatic price.
  A rate of `0` (or below, or not a number) fails validation at boot.
- A proposal outside the price list has `total_value: null`. Its pricing section reads
  `[PLACEHOLDER: Price to be set by Grid Nova.]` (internal). Send answers 409
  `placeholder_content` with `missing_total_value: true` and the message "Price to be set by
  Grid Nova (use adjust-pricing)". With no total, the proposal is never accepted, so there
  are no payment milestones and no commission.
- `PATCH /dashboard/proposals/{id}/adjust-pricing`: `new_value` must be **> 0** (was ≥ 0).
  0 → 422.
- Registry PRO-13 is resolved ("manual pricing by design"); PRO-14 is resolved (1.0 = no change).

## 5. Payout batches: TDS review

Grid Nova deducts no TDS for now (`tds.rate_percent: "0"`). Every batch response
(create, get, approve, mark-paid, list) has:

```json
"tds_review": [{"associate_id": "GNA-WB-26-00001", "name": "Asha Roy",
                "fy_to_date": "30000.00", "threshold": "20000.00",
                "message": "This associate's commission this financial year is above ₹20,000. Grid Nova currently deducts no TDS. Confirm with your Chartered Accountant before paying."}]
```

An associate is listed when their taxable commission this financial year (April–March,
IST, earlier batches plus this one) is above `fy_threshold_inr` and the line deducted at
0%. Empty for cancelled batches.

- `POST /payout-batches/{id}/approve` body gains `tds_review_acknowledged` (bool). With a
  non-empty review and without it → **409 `tds_review_required`** (`detail.tds_review` lists
  them). This also applies when second approval is on: the check happens before the
  request is queued, and the acknowledgement travels with it.
- `POST /payout-batches/{id}/mark-paid` body gains `tds_review_acknowledged`, with the same rule.
- Each acknowledgement is audited (`payout_batch`, action `tds_review_acknowledged`,
  `{step, associates}`) with the user's id.
- With a TDS rate above 0, the existing deduction applies and nothing is listed.

No rate or section number appears anywhere.

## 6. Commission rates and the query time

- No published rate schedule → commission entries have no amount, and batches refuse with
  `409 placeholder_rates`. An Admin publishes the schedule: Dashboard → Commissions → Rate
  schedules → New version (`POST /api/v1/dashboard/commission-rate-schedules`, every rate
  key required, effective date at least 15 days ahead).
- `queries.response_working_days: 15`, as published in the Commission & Payout Policy 4.8
  (website, effective 23 Oct 2026). Registry C5-8 is resolved.

## 7. Migration 0059

`placeholder_resolutions` is rebuilt without the one-row-per-entry rule (`entry_id` is now
an index) and gains `action`, `content_sha256` and `legal_review_confirmed`. Existing rows
become `action = resolved`. `customer_agreements.wording_sha256` is added (nullable).
Downgrade refuses while any approval or revocation row exists.
