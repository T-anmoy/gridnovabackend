# BE-11 — Attribution lock until the project is completed or closed

Written from the code: `app/onboarding/service.py` (`attribution()`, `record_verification_call`,
`_flag_conflicts`, `resolve_flag`), `app/onboarding/serialize.py`,
`app/api/routes/associate_onboarding.py` (`/options`). Migration `0053_attribution_until_completed`.

## The rule

Associate Terms A7, approved by Grid Nova on 21 Oct 2026: *"A customer is attributed to the
first Associate whose complete submission is verified by Grid Nova. The attribution stays with
that Associate until the project is completed or closed."* The 90-day lock period is gone.

A **verified** onboarding is locked from the staff verification call until whichever comes first:

| Released by | `released_reason` | Notes |
|---|---|---|
| Handover complete recorded (`ProjectStage.handover_complete`) | `project_completed` | Stage events are append-only, so this is final. |
| The customer's lead marked `lost` (`PATCH /dashboard/leads/{id}/stage`, reason required) | `lead_lost` | Re-opening the lead (any other stage, reason required) locks it again. |
| The onboarding closed by an Admin resolving an attribution conflict against it | `closed` | Final. |

Nothing stores an end date: the state is derived on every read.

Unchanged: a new onboarding from another associate that matches an existing, not-closed one
(same consumer number or mobile) is saved and raises an attribution flag for an Admin,
**whether or not the existing one is locked**; neither can be verified while the flag is open,
and the Admin decides (`keep_existing` / `award_new`). The flag's `existing_was_locked` records
the lock state at the moment of the match — after a release it is `false`, exactly as after the
old 90-day expiry.

## Contract changes

### Customer detail and list — `attribution` object

`GET /api/v1/associate/onboardings`, `GET /api/v1/associate/onboardings/{id}`, the staff
`GET /api/v1/dashboard/onboardings[/{id}]`, and every other response built by the onboarding
serializer:

```json
"attribution": {"locked": true, "released_reason": null, "under_review": false}
```

| Field | Meaning |
|---|---|
| `locked` | `true`: this customer is attributed to the owner until the project is completed or closed. `false`: not locked — see `released_reason`. |
| `released_reason` | `null` while locked, and also before verification (the lock has not started: `locked` false, reason null). Otherwise `project_completed`, `lead_lost` or `closed`. |
| `under_review` | Unchanged: an open attribution flag involves this customer. |

Staff responses also carry `open_flags` as before.

**Removed:** `attribution.locked_until` (an ISO date). Suggested associate wording when
`locked` is true: *"Attributed to you until the project is completed or closed."*

### `GET /api/v1/associate/onboardings/options`

**Removed:** `attribution_lock_days`. There is no replacement field — the lock has no length.

### Config

`config/associate_onboarding.yaml attribution_lock_days` is removed; the app refuses to start if
it is put back.

## What the frontend must change (`grid-nova-energy-wip`)

| File | Change |
|---|---|
| `lib/associate/onboarding-types.ts` (line ~175) | In the attribution type, replace `locked_until: string \| null` with `locked: boolean` and `released_reason: 'project_completed' \| 'lead_lost' \| 'closed' \| null`. |
| `lib/associate/onboarding-types.ts` (line ~276) | Remove `attribution_lock_days: number` from the options type. |
| `tests/unit/fixtures/survey-contract.ts` (lines ~55, ~171) | Drop `attribution_lock_days`; use `attribution: { locked: false, released_reason: null, under_review: false }`. |
| `tests/unit/staff-onboarding-detail-fixes.test.tsx` (line ~58) | Same attribution fixture change. |
| Any screen that showed "locked until <date>" or "N days" | Show the A7 wording when `locked` is true. (A read-only search found no component reading either field, only the types and test fixtures.) |

## Migration `0053_attribution_until_completed`

- Drops `customer_onboardings.attribution_locked_until`. Every onboarding verified before the
  upgrade stays locked — including those whose 90 days had run out — because the lock follows
  from `status = verified`.
- `ck_customer_onboardings_verified_after_call` now requires `verified_at` (when the lock
  started) instead of `attribution_locked_until`.
- Downgrade restores the column as the old code wrote it (`verified_at + 90 days` for verified
  rows) and the old constraint.

## Commissions

Unaffected. The commission engine never read the lock or its end date: personal-sale commission
goes to the onboarding's owner, and a closed onboarding earns nothing (`commissions.md`).
