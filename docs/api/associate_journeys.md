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

## 4. Suspend, reinstate, deactivate — and moving customers

`POST /associates/{id}/status` answers the associate as before **plus**:
```json
{"…": "…", "access": {"clerk_banned": true, "invitation_revoked": false, "clerk_error": null},
 "open_customers": 3}
```
* Suspending or deactivating bans the associate's Clerk user (every session ends, sign-in
  refused) or, before they ever signed in, revokes the invitation (`clerk_invite_status` becomes
  null; reinstating then means sending a new one). Reinstating answers
  `"access": {"clerk_unbanned": true, "clerk_error": null}`. The database refuses a non-active
  associate on every request regardless, so a Clerk failure (`clerk_error`) leaves the change
  standing. `access` is null when nothing changed for Clerk.
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
