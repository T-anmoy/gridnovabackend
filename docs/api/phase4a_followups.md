# Phase 4A follow-ups

Commit `7ba7c66`, migration `0041_phase4a_followups`. Builds on [phase4a.md](phase4a.md).

## 1. Payments keyed by project

`payments.project_id` and `payment_milestones.project_id` are required;
`onboarding_id` became nullable (a payment can belong to a project with no onboarding).
Every payment/milestone object still carries `onboarding_id` (may be `null`). Receipts,
exports and alerts work from the project. The staff payment detail's `project` block is
built from the onboarding when there is one, otherwise from the project alone
(`current_stage`, `accepted_total`, `balance` = `null`).

## 2. Admin override on an associate-mode project

`POST /api/v1/dashboard/onboardings/{onboarding_id}/execution/{stage}` (EXECUTION_ACTORS)
body: `{text?: {field: value}, note? ≤2000, admin_override_reason? ≤2000}`.

- `grid_nova` mode: recorded as before (`via: "direct"`).
- `associate` mode: non-admins get 409 `execution_mode_associate`. An Admin must give
  `admin_override_reason` (422 `reason_required`, field `admin_override_reason`): the
  stage is recorded with `via: "admin_override"`, audited as `admin_override`, and any
  submission the associate still had pending for that stage is closed as superseded.
- Response: the staff execution detail.

## 3. Switching execution to Grid Nova

`POST /api/v1/dashboard/onboardings/{onboarding_id}/execution/mode` — **Admin only**
(403 `"Only Admin can change a project's execution mode."`).
Body: `{execution_mode: "grid_nova", reason}` (reason required: 422 `reason_required`).
409 `already_grid_nova`. Stages already recorded and any pending submission are left
untouched. Audited `mode_changed` with before/after. Response: the staff execution detail.

## 4. Voided serials and the `previously_voided` flag

Voiding a unit keeps the raw `serial_number`; its normalised form moves from
`serial_normalized` (set to `null`, freeing the serial) to `voided_serial_normalized`.
When the same serial is later registered on a **different** project, that unit carries
the staff-only, non-blocking `previously_voided: true` and the project's staff `flags`
include `previously_voided`. Associates never see it.

## 5. Segregation of duties on verification

`POST /api/v1/dashboard/payments/{payment_id}/verify` body `{reason?}` (body optional).
Whoever recorded a payment cannot verify it: 409 `self_verification` — except an Admin,
who must then give `reason` (422 `reason_required`); audited as `segregation_override`.

## 6. Bounced-cheque re-presentation

A dishonoured payment releases its instrument reference. Recording the same mode +
reference + bank again on the **same** project is allowed and links the new payment via
`previous_payment_id` (on every payment object). The same instrument dishonoured on a
**different** project is refused: 409 `duplicate_payment_reference`. A live or verified
payment with the reference is still refused by the unique index.

## 7. Registry

P1 at the time stated the 20/60/20 terms awaited Grid Nova's written confirmation —
superseded by PM-A (see [pma_pricing.md](pma_pricing.md)).
