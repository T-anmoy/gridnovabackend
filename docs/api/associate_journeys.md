# Associate journeys — API changes (BE-19)

Written from the code after the BE-19 audit of every associate journey. Everything here is
additive unless marked **changed**. No migration. Staff prefix `/api/v1/dashboard`, associate
prefix `/api/v1/associate`, public prefix `/api/v1/public`.

## 1. Applying

**Field-error codes.** A refused application (422) lists every field error at once, as before;
each item now also carries a stable, language-free `code` for the website to show in English,
Hindi or Bengali (`msg` stays as the English fallback):
```json
{"detail": [{"loc": ["body", "pan"], "msg": "The PAN must be an individual's PAN (4th character 'P').",
             "type": "value_error", "code": "pan_not_individual"}]}
```
| code | field(s) |
|---|---|
| `required` | any missing field or file |
| `invalid` | anything without a more specific code |
| `too_long` | text over its limit |
| `name_required`, `name_too_long` | `full_name` |
| `date_format`, `dob_future`, `underage`, `dob_implausible` | `date_of_birth` |
| `aadhaar_format`, `aadhaar_checksum` | `aadhaar_number` |
| `pan_format`, `pan_holder_type`, `pan_not_individual` | `pan` |
| `bank_account_format`, `ifsc_format` | `bank_account_number`, `bank_ifsc` |
| `mobile_format`, `pin_format`, `email_format`, `gstin_format` | contact fields, `gstin` |
| `not_a_whole_number`, `below_minimum`, `above_maximum`, `not_a_choice` | numbers and choices |
| `consent_required`, `consent_version_stale` | `kyc_consent_accepted`, `kyc_consent_version` |
| `references_same`, `reference_is_applicant` | `reference_{n}_mobile` |
| `invalid_file`, `heic_not_supported` | the four uploads |

**Emails to the applicant** (when `NOTIFY_ASSOCIATES` is on), English:
* on submission — "We have received your application" (application number, what happens next);
* on `needs_info` — the reviewer's `note`, verbatim (so the note is written **to the applicant**);
* on `rejected` — the reviewer's reason, verbatim, as "Reason: …" (the reason field is shown to
  the applicant — label it so in the dashboard).
Approval is told by Clerk's invitation email, as before.

## 2. Review (Admin, Network Manager)

* **Regions on every route.** Every per-application and per-associate route now applies the
  reviewer's regions (`docs/api/access_control.md` §4): document links, status, approve,
  invitation, documents, reassign-customers — not only list and detail. 403
  `"This record is outside the regions you work in."`.
* The new-application staff email links to `/dashboard/associate-network/{type}/{id}`
  (**changed**: it linked to a page that does not exist).

## 3. Invitation and the ID card

* `POST /associates/{id}/clerk-invite` (**changed**): also **re-sends** an invitation that was
  sent but never accepted (expired, lost, address corrected) — the old one is revoked first. 409
  `invalid_transition` once the associate has signed in. (Before, a sent invitation could never
  be sent again.)
* The ID card's QR code (**changed**) opens the website's verification page
  `FRONTEND_BASE_URL/verify/{associate_id}`, not the JSON endpoint. Cards issued before still work
  (the endpoint is unchanged). A card cannot be generated while `FRONTEND_BASE_URL` is empty
  (`documents.error` names it).

### When Clerk cannot send the invitation (BE-21)

The approval still completes (as before) and `clerk_invite_status` is `failed`. The associate
record now carries `manual_sign_up` — the sentence for staff: "Ask the associate to sign up at
{FRONTEND_BASE_URL}/associates/sign-up with {email}." (null in every other case: invitation
sent, or the associate already signed in, or not active). Retrying the invitation
(`POST …/clerk-invite`) still works too.

### `POST /api/v1/associate/me/claim` — new (BE-21)

For the associate who signed up **without** the invitation. Takes the Clerk session token as
it is right after sign-up — **no** associate principal yet (staff dev headers are not a Clerk
token: 401). No body. The caller is linked to an associate only when **all** hold:

1. the caller is not staff (no employee row for this Clerk user) and holds no associate yet;
2. one of the caller's **verified** Clerk addresses (Clerk `GET /v1/users/{id}`; never the
   token's `email` claim alone) equals the associate's email, exactly (trimmed, lower-cased);
3. the associate is `active`, has no Clerk user yet, and `clerk_invite_status` is `failed` or
   `sent` (exactly one such associate).

Then: Clerk `PATCH /v1/users/{id}/metadata` sets `public_metadata {principal: "associate",
associate_id}` (free on every plan), `clerk_user_id` is set (a conditional update — of two
sessions racing for one associate, exactly one wins), a still-pending Clerk invitation is
revoked, and `clerk_user_claimed` is audited.

* **200** `{"associate_id": "GNA-WB-26-00012", "refresh_session": true}` — refresh the Clerk
  session (`getToken({skipCache: true})` / `session.reload()`) so the token carries the
  associate claims; then every associate route works as for an invited associate.
* **403** `{"code": "claim_not_allowed", "message": "…"}` — every refusal, with the same body
  whether or not the address belongs to an associate.
* **429** `rate_limited` — 10 a minute per client IP and per Clerk user.
* **502** `clerk_sync_failed` — Clerk refused the metadata update; nothing was linked, retry.
* **503** `identity_check_unavailable` — Clerk could not be asked for the verified addresses.

**Clerk setting:** sign-up must stay **open** (not "Invite-only") — otherwise a person without an
invitation link cannot create the account to claim with. The API is the gate: an uninvited
staff sign-up gets 403 `not_invited`, an unmatched associate 403 `claim_not_allowed`.

## 4. Suspend, reinstate, deactivate — and moving customers

`POST /associates/{id}/status` answers the associate as before **plus**:
```json
{"…": "…", "access": {"clerk_banned": false, "sessions_revoked": 2, "invitation_revoked": false,
                       "clerk_error": null, "clerk_mode": "revoke_sessions"},
 "open_customers": 3}
```
* Suspending or deactivating (BE-21): the database refuses the associate on their very next
  request, even with a token Clerk still considers valid. Then, for a signed-in associate,
  Clerk by `CLERK_BAN_ENABLED`: `revoke_sessions` (default; Clerk's free plan has no ban) ends
  every active session (`sessions_revoked`: how many; null without a Clerk key); `ban` (Clerk
  Pro) bans the user (`clerk_banned: true`, `sessions_revoked: null`). Before they ever signed
  in, the pending invitation is revoked instead (`invitation_revoked: true`;
  `clerk_invite_status` becomes null; reinstating then means sending a new one).
* Reinstating answers `"access": {"clerk_unbanned": …, "clerk_error": null, "clerk_mode": …}` —
  only `ban` mode unbans; with `revoke_sessions` nothing changes in Clerk. A Clerk failure
  (`clerk_error`, plus the count that did succeed) leaves the status change standing; audited
  `clerk_access_revoked` / `clerk_access_restored`, or `…_failed`. `access` is null when the
  status did not change.
* `open_customers`: customers the associate still owns (not closed).
* **Commission is frozen while suspended**: their approved entries stay out of every payout
  batch (still approved) and are paid in the first batch after reinstatement. A deactivated
  associate's earned commission is settled as usual (Network Terms A11). `POST /payout-batches`
  answers 409 `no_approved_entries` with `"frozen_entries": n` when only frozen commission is
  left; the batch audit lists `frozen_entries`.
* Emails about customers are no longer sent to an associate who is not active.

### `POST /associates/{id}/reassign-customers` — `associates.applications.review` (Admin, Network Manager)
Body `{to_associate_id, reason, onboarding_ids?}` (omit `onboarding_ids` for every open customer).
→ 200 `{"from_associate_id", "to_associate_id", "onboarding_ids": [...], "moved": n}`.
* Only from a **suspended or deactivated** associate: 409 `associate_active` otherwise (an active
  associate's customers are never moved — Terms A7).
* To an **active**, different associate in the caller's regions: 422 `target_not_active` /
  `same_associate`, 404 unknown, 403 outside regions; 422 `reason_required`;
  422 `unknown_customers` (`onboarding_ids`: ids that are not this associate's open customers);
  409 `nothing_to_move`.
* A project whose execution the associate runs moves only to an associate who may run execution
  (a Preferred Associate, or capability `execution`): 409 `target_cannot_execute` with
  `onboarding_ids` — take execution over first (mode switch) or choose another associate.
* The new owner sees and works the customers in their portal at once and is emailed
  ("N customers assigned to you", link to their customer list). Audited per customer
  (`owner_reassigned`, before/after owner, reason) and once on the associate
  (`customers_reassigned`). Commission already recorded stays with the associate who earned it;
  payments verified after the move earn the new owner — an unconfirmed reading, registry
  C5-REASSIGN.

## 5. Customer payments

`config/execution_payments.yaml` `enabled_modes` (**changed**, Grid Nova's decision): cheque,
NEFT, RTGS, IMPS only. Recording `demand_draft` or `upi` answers 422 `mode_not_enabled`; existing
rows keep their mode. The website's Customer Payment Policy 5.2 still lists demand draft and UPI.

## 6. Commissions

**Changed (a defect the scenario test found):** a formal proposal priced through the BE-13
document (adjust-pricing writes there) had no commission basis — every entry stayed
`pending_reason: "basis_unavailable"` and was never paid. The basis now comes from the accepted
proposal's document figures (basic ÷ total incl. GST) when it has a document, else from the older
`content.pricing`. Existing `basis_unavailable` entries are not re-computed automatically. The
serial register's package lookup reads the document's package in the same way.

## 7. Staff alerts

New, role-routed (`config/access_control.yaml`): `proposal_ready_for_review` — Admin and the
lead's assigned employee, when a formal proposal is prepared for an associate's customer or a
site-assessment approval drafts a proposal; link `/dashboard/proposals/{id}`. The overdue
commission-queries email links to `/dashboard/commissions/queries` (**changed**: it linked to a
page that does not exist).

## 8. Customer Information Notice

`GET /associate/onboardings/options` → the notice is now Grid Nova's text (version
`approved-v1`, the drafted notice used as written); onboardings record that version.

---

# BE-20 additions

## 9. Documents and photos

### Rules

- **Site-assessment documents:** one per `document_type`, taken from `GET /associate/onboardings/options`
  → `documents[].key`. Which are required depends on `required_when`:
  - `always`;
  - `scheme_linked` — the site assessment says the project is scheme-linked;
  - `not_in_customer_name` — the connection or the property is not in the customer's name.
- **The checklist:** `checklist: {required, present, missing}` on the onboarding. Submitting
  with a missing document answers 422 `incomplete` with `missing_documents`.
- **Site photos:** one per `photo_slot`; a new upload replaces the slot.
- **Execution photos and documents:** one per slot or type. The latest is kept; a submission keeps
  the files it used.
- **Upload limits:** documents JPEG/PNG/WEBP/PDF, photos JPEG/PNG/WEBP; each ≤ 10 MB.

Upload: `POST /associate/onboardings/{id}/files` (multipart).
- Fields: `kind` (`document` | `photo`), `document_type` or `photo_slot`, and for photos
  `capture_method`, `latitude`/`longitude`, `captured_at`, `accuracy_m`.
- Response: the onboarding.

### Names, as `{code, label}` (additive; the old fields stay)

| Where | Old field (unchanged) | New field |
|---|---|---|
| `GET /associate/onboardings/options` | `documents: [{key, label, required_when}]` | each document also has `code` (= `key`) |
| `GET /associate/onboardings/options` | `photo_slots: ["panel_installation_area", …]` | `photo_slot_items: [{code, label}]` |
| `GET /associate/onboardings/{id}/execution` (and the staff view) | `photo_slots`, `document_types` (lists of codes) | `photo_slot_items`, `document_type_items: [{code, label}]` |

Translate by `code`. `label` is the English text (from `config/associate_onboarding.yaml`
`photo_slot_labels` and `config/execution_payments.yaml` `execution.labels`).

| Site-photo code | Label |
|---|---|
| `panel_installation_area` | Panel installation area |
| `acdb_dcdb_placement` | ACDB / DCDB placement |
| `earthing_location` | Earthing location |
| `inverter_installation_area` | Inverter installation area |
| `wiring_path` | Wiring path |
| `panel_to_inverter_distance` | Panel to inverter distance |
| `inverter_to_lt_panel_distance` | Inverter to LT panel distance |

| Document code | Label |
|---|---|
| `electricity_bill` | Latest electricity bill |
| `id_proof` | Customer ID proof |
| `ownership_proof` | Property ownership proof |
| `subsidy_bank_details` | Bank details for subsidy credit |
| `photo` | Customer / site photo |
| `noc` | No-objection certificate (connection or property not in the customer's name) |

### Response shapes

On the onboarding:
- `documents: [{document_type, file_ref, filename, received_at, latitude, longitude, client_captured_at}]`;
- `photos: [{photo_slot, file_ref, filename, received_at, latitude, longitude, client_captured_at,
  capture_method, …provenance, accuracy_m}]`.

Coordinates are given only to the owner and staff.

**Fixed:** two uploads made at the same time (slow connections) no longer lose one of the
files, for site-assessment documents and photos and for execution files.

### The estimate's `segment`

`segment` is one of `residential`, `commercial`, `industrial`, `government`. They are also served
as `estimate_segments` on `/options`.
- Rooftop-residential and rooftop-commercial sites set it themselves.
- Any other site type needs it chosen (422 `segment_required`).
- Anything else is 422 `invalid_segment`.

## 10. `GET /dashboard/associates/{associate_id}/customers`

Permission: `associates.applications.review` (Admin, Network Manager), the same as
reassign-customers. The associate's state must be in the caller's regions.

Query `include_closed=true` includes closed customers. Response:

```json
{"associate_id": "GNA-WB-26-00007", "open_customers": 2,
 "items": [{"onboarding_id": 31, "client_code": "GNE-WB-RES-2026-00031", "customer_name": "…",
            "status": "verified", "project_stage": "accepted", "open": true}, …]}
```

- **Order:** oldest first.
- **`open`:** "not closed", the same test the move uses.
- **`project_stage`:** the last stage recorded, or null.

## 11. `duplicate_onboarding`

`POST /associate/onboardings` and a `PATCH` that changes the mobile or consumer number answer
**409** `duplicate_onboarding` when **the same associate** already has an open record for that
customer (same mobile or consumer number). **New:** it names that record:

```json
{"code": "duplicate_onboarding", "message": "You have already onboarded this customer (same mobile). Update that record instead.",
 "field": "customer_mobile", "existing_onboarding_id": 31, "client_code": "GNE-WB-RES-2026-00031"}
```

Another associate's match is never refused and never named. The record is created and flagged
for attribution review (warning `attribution_review`), exactly as before.

## 12. `GET /public/verify/{associate_id}` — unchanged

It still returns only `{name, photo_url, type, level, status: "active" | "inactive"}`. No
`status_detail` was added: telling suspended from deactivated would publish a disciplinary state
that the public `inactive` deliberately does not reveal.

