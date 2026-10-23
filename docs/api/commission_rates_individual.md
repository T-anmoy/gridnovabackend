# BE-12 — individual commission rates, and the first real schedule

Written from the code: `app/commissions/rates.py` (`resolve`, `set_individual`,
`scheduled_changes`), `app/commissions/performance.py`, `app/api/routes/commissions_admin.py`,
`app/api/routes/associate_commissions.py`, `app/onboarding/todo.py` (`rate_items`),
`app/notifications/associate_emails.py` (`commission_rate_changed`), migration
`0054_individual_commission_rates`. When code and this file disagree, the code wins.
Conventions (auth, 401/403, error shape) are in [README.md](README.md); the rest of the
commission API is in [commissions.md](commissions.md).

Grid Nova decided on 23 Oct 2026 that commission is variable: Admin publishes the standard
rates (schedules) and can raise or lower one Associate's rate according to their
performance. No rates are written in code or config: an Admin enters them in the
dashboard after deployment.

## The rule, in plain words

1. **Which rate applies.** For an Associate's own customers (`personal_sale`), on a given
   date: the Associate's latest **individual rate** dated on or before that date. If
   there is none, or the latest one is "back to standard" (`rate_percent: null`), the
   **standard rate** for their type and capability or level, from the confirmed schedule
   in force on that date. If there is no confirmed schedule either, there is no rate yet
   (`pending_reason: "placeholder_rates"`). When two individual rates have the same date,
   the one set later wins, so a same-day correction replaces the earlier one.
2. **Which date.** The same date as always (policy 4.1): the date the customer's
   agreement is signed. For an estimate before signing, today. So a change applies to
   customers who sign on or after its date. **Commission already recorded never changes.**
   The ledger's database trigger refuses any change to a recorded rate or amount.
3. **Territory commission** (`territory_state` / `territory_district` / `territory_block`)
   has no individual rates: it is always the standard schedule's rate.
4. **Notice.** A **reduction** needs 15 days' notice (`commissions.rate_change_notice_days`).
   A reduction is a new rate lower than the rate in force for that Associate on the
   change's date, and that includes going back to a lower standard rate. An **increase**
   may take effect today (`commissions.individual_rate_increase_needs_notice: false`,
   [UNCONFIRMED] C5-IND-1). Setting the same rate again is no change and is allowed today.
   With no confirmed rate in force for the Associate on that date, nobody relies on a
   rate, so there is nothing to give notice of. No change can be dated in the past (IST).
5. **Limits.** 0–100 %, at most 4 decimals, like schedules. Optional
   `commissions.individual_rate_min_percent` / `individual_rate_max_percent`: both `null` =
   no limit beyond 0–100 ([UNCONFIRMED] C5-IND-2: Grid Nova has given no limits).
6. **Nothing is edited.** Every change is a new row (`associate_commission_rates`, which
   a database trigger makes append-only). It is audited, and the Associate is told the
   rates and the date, never the reason.
7. **The first real schedule.** While no non-placeholder schedule has *ever* been
   published, the first real one may take effect today. Every later one needs the 15
   days ([commissions.md](commissions.md), `POST /commission-rate-schedules`).

Who: **Admin** sets. **Admin and Management** read (including the internal reason).
**Accounts, Sales, Engineer, Operations**: 403. Accounts still sees each ledger entry's
`rate_percent` and `rate_source`. **The Associate** sees only their own rate.

## Staff: `GET /api/v1/dashboard/associates/{associate_id}/commission-rate`

Admin, Management. `associate_id` is the GNA id. 404 (plain string) `"Associate not found"`.

```json
{
  "associate_id": "GNA-WB-26-00007",
  "full_name": "…",
  "rate_key": "personal_sale.associate.sales",
  "today": "2026-10-23",
  "standard": {"rate_percent": "7.5000", "schedule_id": 1, "schedule_version": 1, "effective_from": "2026-10-23"},
  "individual": {"individual_rate_id": 4, "rate_percent": "8.0000", "effective_from": "2026-10-23"},
  "effective": {"rate_percent": "8.0000", "source": "individual", "effective_from": "2026-10-23",
                "pending_reason": null},
  "scheduled_changes": [{"effective_from": "2026-11-07", "rate_percent": "7.5000", "source": "standard"}],
  "history": [
    {"individual_rate_id": 5, "rate_key": "personal_sale.associate.sales", "rate_percent": null,
     "effective_from": "2026-11-07", "reason": "Quarterly review: back to standard", "set_by": "emp-admin-1",
     "created_at": "2026-10-23T06:10:00+00:00", "status": "scheduled"},
    {"individual_rate_id": 4, "rate_key": "personal_sale.associate.sales", "rate_percent": "8.0000",
     "effective_from": "2026-10-23", "reason": "Twelve installs this quarter, all on time",
     "set_by": "emp-admin-1", "created_at": "2026-10-23T06:00:00+00:00", "status": "in_force"}
  ],
  "notice": {"increase_earliest_effective_from": "2026-10-23", "reduction_earliest_effective_from": "2026-11-07",
             "increase_needs_notice": false, "notice_days": 15},
  "bounds": {"min_percent": null, "max_percent": null}
}
```

| Field | Meaning |
|---|---|
| `rate_key` | The Associate's personal-sale key (by type and capability or level). It never changes, because no route changes an Associate's capability or level. |
| `standard` | The confirmed schedule's rate for `rate_key` today; `null` with no confirmed schedule in force (none, or a placeholder). |
| `individual` | The individual rate in force today; `null` when there is none, or the row in force is "back to standard". |
| `effective` | What applies today (rule 1). `source` `"individual"` / `"standard"`; `effective_from` = the date that row or schedule took effect. All `null` with `pending_reason: "placeholder_rates"` when there is no confirmed rate. |
| `scheduled_changes` | Every future date on which the effective rate will differ from the day before, as things stand now: future individual rows and future confirmed schedules, resolved in date order. A future standard change is listed only if the standard rate would apply on that date. `rate_percent` `null` / `source` `null` = no confirmed rate from that date. |
| `history` | Every individual row for this Associate, newest date first (same date: newest first). `rate_percent: null` = "back to standard". `status`: `scheduled` (dated after today), `in_force` (the row that applies today), `superseded` (an earlier row, or a same-day row replaced by a later one). `reason` is internal: **never show it to the Associate**. |
| `notice` | The earliest date a change may take effect: `increase_earliest_effective_from` (today, or today + `notice_days` when `increase_needs_notice`), `reduction_earliest_effective_from` (today + `notice_days`). The server decides which applies; use these for the date picker's minimum and hint. |
| `bounds` | `individual_rate_min_percent` / `max_percent` (`null` = none). |

## Staff: `POST /api/v1/dashboard/associates/{associate_id}/commission-rate` → 201

Admin only (Management and others: 403 `"Only Admin can do this."`). 404 plain
`"Associate not found"`.

```json
{"rate_percent": "8.5", "effective_from": "2026-10-23", "reason": "Twelve installs this quarter, all on time"}
```

| Field | Rules |
|---|---|
| `rate_percent` | **Required.** A percentage **string**, 0–100, at most 4 decimals, or `null` = "back to the standard schedule". A JSON number is not accepted (FastAPI's standard 422 with a list `detail`), and neither is a missing key. |
| `effective_from` | `YYYY-MM-DD`, today (IST) or later; a reduction needs today + 15 days or later. |
| `reason` | Required, at least 10 characters after trimming (at most 2000). Internal: shown to Admin and Management only. |

Response: `{"change": <history row>, "rate": <the GET response, after the change>}`.

Errors, all `{"detail": {"code", "message", "field", …}}`, 422:

| code | field | extra | when |
|---|---|---|---|
| `notice_period` | `effective_from` | `earliest_effective_from` | A reduction dated before today + 15 days. Message `"A lower rate takes effect only after the notice period."`. With `individual_rate_increase_needs_notice: true`, also an increase, with message `"A rate change takes effect only after the notice period."`. |
| `effective_in_past` | `effective_from` | `earliest_effective_from` (= today) | `effective_from` before today (IST). |
| `invalid_rate` | `rate_percent` | — | Not a number, below 0, above 100, or more than 4 decimals. |
| `out_of_bounds` | `rate_percent` | `min_percent`, `max_percent` (strings or `null`) | Outside the configured limits. |
| `reason_required` | `reason` | — | Missing, or shorter than 10 characters after trimming. |

Checks run in this order: reason, rate, bounds, past, notice.

What happens on success, all in one transaction:
* a new `associate_commission_rates` row;
* an `audit_logs` row: `entity_type: "associate_commission_rate"`, `entity_id` = the row id,
  `action` `individual_rate_set` (or `individual_rate_cleared` for `null`),
  `before_value {rate_percent, rate_percent_day_before}`, `after_value {associate_id,
  rate_key, rate_percent, effective_rate_percent, effective_from, reduction,
  notice_required, reason}`;
* after commit, if `NOTIFY_ASSOCIATES` is on and the Associate has an email, an email
  (event `associate_commission_rate_changed`, subject "Your commission rate is changing"):
  *"Your commission rate changes from 8% to 8.5% from 23 October 2026."* (or *"Your
  commission rate is 8.5% from …"* when there was no confirmed rate before), plus a line
  that the rate follows the agreement date and recorded commission does not change, and a
  link to `/{associates|preferred}/portal/earnings`. **The reason is never sent.**

Entries already pending with `placeholder_rates` are not rated by this call. An Admin's
re-rate (`POST /commission-rate-schedules/{id}/re-rate`) rates them, and an individual
rate on the agreement date wins there too.

## Staff: `GET /api/v1/dashboard/associates/{associate_id}/performance?date_from=&date_to=`

Admin, Management. Period = IST days, inclusive. Default: this month so far. At most
366 days, else 422 `invalid_period` (field `date_from`). This is new: the Network Command
Centre (`/network/summary`) has network-wide totals only, not per Associate.

```json
{"associate_id": "GNA-WB-26-00007",
 "period": {"date_from": "2026-10-01", "date_to": "2026-10-23"},
 "customers": {"onboarded": 4, "verified": 3, "commissioned": 1},
 "verified_payments": {"credits": "87120.00", "reversals": "0.00", "net": "87120.00"},
 "commission": {"earned": "4000.00", "earned_personal_sale": "4000.00", "reversed": "0.00", "pending_count": 0}}
```

| Figure | Definition |
|---|---|
| `customers.onboarded` | Onboardings this Associate owns, created in the period. |
| `customers.verified` | Their onboardings Grid Nova verified in the period (`verified_at`). |
| `customers.commissioned` | Their customers' projects whose `commissioned` stage was recorded in the period. |
| `verified_payments` | The payment ledger for their own customers' projects, written in the period: credits (verified), reversals (dishonoured, negative), net. Same definition as the Network Command Centre's `verified_revenue`. |
| `commission.earned` | Amounts of this Associate's earn entries created in the period, every role (territory included); `earned_personal_sale` = the personal-sale part. |
| `commission.reversed` | Reversal and recovery entries created in the period (negative). |
| `commission.pending_count` | Their earn entries created in the period still without an amount. |

**Not available: installed capacity (kW).** `projects.capacity_kw` is the capacity the
customer asked for at onboarding (`desired_output_kw`), not what was installed. No
installed-kW figure exists.

## Associate: `GET /api/v1/associate/commission-rate`

The caller's own rate (Associate and Preferred Associate alike). Staff tokens: 401 / 403
as everywhere.

```json
{"rate_key": "personal_sale.associate.sales", "today": "2026-10-23",
 "effective": {"rate_percent": "8.0000", "source": "individual", "effective_from": "2026-10-23",
               "pending_reason": null},
 "scheduled_changes": [{"effective_from": "2026-11-07", "rate_percent": "7.5000", "source": "standard"}]}
```

Same meanings as the staff fields of the same names. Never the reason, never who set it,
never another Associate's data. Before any confirmed rate:
`"effective": {"rate_percent": null, "source": null, "effective_from": null, "pending_reason": "placeholder_rates"}`.

## Associate home feed (`GET /api/v1/associate/home`)

A new item kind, priority 3 (for information), `onboarding_id` and `customer_name` `null`:

```json
{"kind": "commission_rate_changed", "message_key": "todo.commission_rate_changed", "priority": 3,
 "onboarding_id": null, "customer_name": null,
 "params": {"rate_percent_from": "8.0000", "rate_percent_to": "7.5000", "effective_from": "2026-11-07",
            "scheduled": true}}
```

There is one item per individual-rate change that was set in the last `payout.recent_days`
(7) days or takes effect after today. For a date with several rows, only the latest is
shown. `rate_percent_from` is the rate the day before `effective_from`, and
`rate_percent_to` the rate from it (`null` = no confirmed rate). `scheduled` is true while
the date is still ahead. Suggested English: *"Your commission rate changes from
{rate_percent_from}% to {rate_percent_to}% from {effective_from}."*

## Estimated commission (BE-10) and the ledger

* `estimated_commission` on `GET /associate/onboardings` and `/associate/onboardings/{id}`
  is computed with the same resolver, so an Associate with an individual rate sees
  estimates at their own rate. New field `rate_source`: `"standard"`, `"individual"` or
  `null` (no rate). See [associate_ease.md](associate_ease.md).
* Ledger entries (`GET /dashboard/commissions`, `/associate/commissions`) gain `rate_source`.
  Staff responses also gain `individual_rate_id` ([commissions.md](commissions.md)). An entry
  rated at an individual rate has `schedule_version: null`.

## Database (migration `0054_individual_commission_rates`)

* `associate_commission_rates (id, associate_id → associates.id, rate_key, rate_percent
  NUMERIC(7,4) NULL, effective_from DATE, reason TEXT, set_by, created_at)`. CHECK
  `rate_percent` NULL or 0–100. CHECK `length(trim(reason)) >= 10`. Index `(associate_id,
  rate_key, effective_from)`. UPDATE and DELETE are refused by triggers (PostgreSQL function
  `associate_commission_rates_append_only`; SQLite `RAISE(ABORT)`).
* `commission_entries.individual_rate_id → associate_commission_rates.id`, nullable. It is
  set once, like the amount: the ledger guard is recreated with it among the set-once
  columns.
* Downgrade drops both and restores the BE-4 guard. It refuses (and changes nothing) while
  any entry was rated at an individual rate.
