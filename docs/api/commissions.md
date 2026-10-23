# BE-4 (Phase 5A) — commissions, TDS and payouts

Written from the code: `app/commissions/` (rules), `app/api/routes/commissions_admin.py`,
`app/api/routes/associate_commissions.py`, `app/api/routes/network_admin.py`. The policy
is the Commission & Payout Policy §4 (website `/commission-policy`). Conventions
(errors, money, times, message keys) are in [README.md](README.md).

Values Grid Nova has not confirmed are in `config/commissions.yaml` and the placeholder
registry (C5-RATES, C5-1 … C5-12). **No commission rates have been supplied**: until an
Admin publishes a real rate schedule, entries are recorded without an amount and no
payout can include them.

## How commission works (what the screens should explain)

* **Earned only on verified money (4.2).** When Accounts verifies a customer payment,
  each associate who earns on it gets one `earn` entry for that payment, in proportion to
  the amount verified. A recorded, deposited or cancelled payment earns nothing.
* **Who earns (4.3).** `personal_sale` — the associate the customer is attributed to
  (the onboarding's owner). `territory_state` / `territory_district` / `territory_block` —
  whoever holds that seat for the site's state / district / block on the agreement-signed
  date (config `territory_seat_holder_at`). Two State Heads (or several Block Associates)
  share the territory rate (`multiple_territory_holders: split_equally`; `holders` on the
  entry says how many). A Preferred Associate who brings in a customer in their own
  territory earns both (`territory_on_own_customers`). A deactivated associate earns
  nothing on payments verified after deactivation.
* **Basis.** The verified amount without GST: verified × the accepted proposal's basic
  price ÷ its final price (`basis: basic_pro_rata`). A custom-quoted proposal has no
  stored basic price, so its entries wait with `basis_unavailable`.
* **Rate (4.1).** The rate in force on the date the customer's agreement was signed
  (the day the signed agreement was recorded; config `agreement_date_source`): the
  associate's **individual rate** on that date when an Admin set one (BE-12,
  [commission_rates_individual.md](commission_rates_individual.md); personal sale only),
  otherwise the confirmed schedule in force then. The entry's `rate_source` says which.
* **Waiting entries.** `amount` is `null` and `pending_reason` says why:
  `agreement_not_signed` (rated automatically when the agreement is recorded),
  `placeholder_rates` (no confirmed schedule — rated by an Admin's re-rate),
  `basis_unavailable`. `pending_message_key` = `commission.pending.<reason>`.
* **Status (4.4):** `earned` → `approved` (Accounts checked it) → `paid` (in a paid
  payout; `batch.period`, and the payout's bank reference on `/associate/payouts`).
  `reversed`: an earn entry and its reversal, cancelled out and never paid.
* **Reversal (4.7).** A verified payment dishonoured later reverses every earn entry on
  it, in the same transaction as the ledger reversal and the voided receipt: not yet in a
  payout → a `reversal` entry (both become `reversed`; a draft batch holding the entry is
  cancelled automatically); already in an approved or paid payout → a `recovery` entry
  (status `approved`) netted against the associate's next payouts. An unrecovered
  balance carries forward. Refunds and project cancellations do not exist in the system,
  so only dishonour (and the Admin reversal below) triggers this.
* **Payouts (4.5)** are monthly batches. **TDS (4.6)**, draft rule awaiting a CA: nothing
  while the associate's taxable commission in the financial year (April–March, IST) is at
  or below ₹20,000; once above, 2 % (20 % with no PAN on file) on the whole year-to-date
  amount less TDS already deducted. The taxable amount of a payout is its commission less
  recoveries (and less any balance carried in), never below zero.
* **Queries (4.8)** within 30 days of the entry appearing; answer due in 15 working days
  (Mon–Fri, end of day IST).

## Roles

| | Admin | Management | Accounts | Sales / Engineer / Operations | Associate |
|---|---|---|---|---|---|
| Commission ledger, schedules, batches, queries (read) | ✓ | ✓ | ✓ | 403 | own entries only |
| Approve entries; prepare / approve / pay / cancel batches; answer queries; commission & TDS CSV | ✓ | 403 | ✓ | 403 | — |
| Publish a rate schedule; re-rate; reverse an entry | ✓ | 403 | 403 | 403 | — |
| Set an associate's individual rate (BE-12) | ✓ | 403 | 403 | 403 | — |
| Read an associate's individual rate, its history and performance summary (BE-12) | ✓ | ✓ | 403 | 403 | own rate only, never the reason |
| Payout bank file (full account numbers) | **403** | 403 | ✓ | 403 | — |
| Network summary | ✓ | ✓ | 403 | 403 | — |

Associate tokens on staff routes and staff tokens on associate routes: 401 (dev headers)
/ 403 (Clerk), as everywhere.

## Objects

**Commission entry** (`entry`):

```json
{"entry_id": 12, "associate": {"associate_id": "GNA-WB-26-00007", "full_name": "…"},
 "earning_role": "personal_sale|territory_state|territory_district|territory_block",
 "kind": "earn|reversal|recovery|adjustment", "status": "earned|approved|paid|reversed",
 "customer_name": "…", "project_reference_id": "GNE-WB-RES-2026-00012", "onboarding_id": 5, "payment_id": 9,
 "basis_amount": "20000.00", "rate_percent": "5.0000", "holders": 1, "amount": "1000.00",
 "pending_reason": null, "pending_message_key": null, "schedule_version": 1, "rate_source": "standard",
 "agreement_date": "2026-10-13",
 "reverses_entry_id": null, "reversed_by_entry_id": null, "reason": null,
 "batch": {"batch_id": 3, "period": "2026-10", "status": "approved"},
 "created_at": "…", "approved_at": "…", "paid_at": null}
```

`rate_source` (BE-12): `"standard"` (rated at schedule `schedule_version`), `"individual"`
(rated at the associate's individual rate; `schedule_version` is then `null`), or `null`
(not rated yet). Staff responses add `created_by`, `approved_by`, `rated_by`, `rated_at`,
`individual_rate_id` (BE-12; `null` unless `rate_source` is `individual`). Associate
responses add `query: {"open": bool, "window_closes_at": "<UTC ISO>"}`. A reversal or
recovery has negative `amount`, `basis_amount`/`rate_percent` null, `reverses_entry_id`
set and `reason`. `adjustment` is reserved — nothing creates one yet.

**Payout line** (staff): `line_id, associate {associate_id, full_name}, gross, recoveries,
carried_in, taxable, tds, tds_rate_percent, net, carry_forward, account_holder_name,
bank_account_last4, bank_ifsc, bank_reference, paid_at, pan_on_file, fy_taxable_before,
fy_tds_before`. Associates see the same without `associate`, `account_holder_name`,
`pan_on_file`, `fy_*`. `net = taxable − tds`; `taxable = max(gross − recoveries − carried_in, 0)`;
`carry_forward` = what is still to recover.

**Payout batch:** `batch_id, period ("YYYY-MM"), financial_year ("2026-27"), status
(draft|approved|paid|cancelled), prepared_by, prepared_at, approved_by, approved_at,
approval_override_reason, paid_by, paid_at, cancelled_by, cancelled_at, cancel_reason,
totals {gross, recoveries, carried_in, taxable, tds, net, carry_forward}, line_count`,
plus `lines` on the single-batch responses.

**Query:** `query_id, entry_id, message, status (open|answered), response, responded_at,
due_at, created_at`; staff add `responded_by`, `associate {associate_id, full_name}`.

## Associate endpoints (`/api/v1/associate`)

### `GET /commissions?status=&kind=&page=1&page_size=50` (≤ 200)
The caller's own entries, newest first: `{items: [entry], page, page_size, total, summary}`.
`summary`: `{"earned", "approved", "paid"` (sums of amounts by status, recoveries included,
reversed pairs excluded), `"pending_count"`, `"to_recover"` (carry-forward of the latest
approved/paid payout), `"financial_year"`, `"fy_taxable_commission"`, `"fy_tds_deducted"`
(approved/paid payouts of the current FY), `"next_payout": {"period", "expected_by"}}` —
last month until its batch is paid, then this month; `expected_by` is the 10th of the
following month (config, C5-10).

### `GET /payouts`
`{items: [{batch_id, period, financial_year, status (approved|paid), …payout line}]}`,
newest month first. Draft batches are not shown.

### `POST /commissions/{entry_id}/queries` → 201 query
Body `{"message": "1–2000 chars"}`. Errors: 404 `not_found` (not the caller's entry —
structured detail), 422 `invalid_message`, 409 `query_window_closed` (+ `window_closed_at`),
409 `query_already_open`.

### `GET /commissions/{entry_id}/queries`
`{items: [query]}`, newest first. 404 (plain) when it is not the caller's entry.

### Home feed (`GET /api/v1/associate/home`) — new item kinds, priority 3

| kind / message_key | params | onboarding_id |
|---|---|---|
| `commission_payout_paid` / `todo.commission_payout_paid` | `batch_id, period, net, bank_reference, bank_account_last4` | `null` |
| `commission_query_answered` / `todo.commission_query_answered` | `query_id, entry_id` | the entry's customer (may be `null`) |
| `commission_rate_changed` / `todo.commission_rate_changed` (BE-12) | `rate_percent_from, rate_percent_to, effective_from, scheduled` | `null` |

The first two for `payout.recent_days` (7) days; `commission_rate_changed` while the change
was set in the last 7 days or takes effect later — see
[commission_rates_individual.md](commission_rates_individual.md). `customer_name` is `null`.

### `GET /commission-rate` (BE-12)
The caller's own personal-sale rate today and every scheduled change —
[commission_rates_individual.md](commission_rates_individual.md).

## Staff endpoints (`/api/v1/dashboard`)

### `GET /commissions`
Filters: `associate_id` (GNA id), `status`, `kind`, `earning_role`, `pending` (bool:
amount missing / present), `onboarding_id`, `payment_id`, `batch_id`, `date_from`,
`date_to` (IST days on `created_at`), `page`, `page_size` (≤ 200). Returns `{items: [entry],
page, page_size, total, totals: {earned, approved, paid, pending_count}}` (sums over the
whole filter, not the page).

### `POST /commissions/approve`
Body `{"entry_ids": [1, 2, …]}` (1–500). All or nothing → `{approved: n, items: [entry]}`.
409 `entries_not_approvable` with `entries: [{entry_id, reason}]`, reason one of
`not_found`, `not_an_earn_entry`, `not_earned`, `not_rated`.

### `POST /commissions/{entry_id}/reverse` (Admin)
Body `{"reason": "…"}` — policy 4.7, false information or a breach. → `{entry: <the reversal
or recovery>}`. 404 `not_found`; 409 `not_reversible` (not an earn entry, or already
reversed); 422 `reason_required`.

### `GET /commission-rate-schedules`
`{items: [schedule], in_force_schedule_id, rate_keys: [...], earliest_effective_from,
next_real_schedule_is_first}`. `earliest_effective_from` is for a real schedule: today while
no real schedule has ever been published (`next_real_schedule_is_first: true`, BE-12),
otherwise today IST + 15 days.
Schedule: `{schedule_id, version, effective_from, is_placeholder, note, published_by,
published_at, rates: {rate_key: "5.0000"}, in_effect}`. Rate keys:
`personal_sale.associate.sales`, `personal_sale.associate.execution`,
`personal_sale.preferred_associate.state_head`, `…district_head`, `…block`,
`territory_state`, `territory_district`, `territory_block`.

### `POST /commission-rate-schedules` (Admin) → 201 schedule
Body `{"effective_from": "YYYY-MM-DD", "is_placeholder": false, "rates": {every key: "2.5"},
"note"?: "…"}`. A schedule is never edited; this publishes the next version. Errors:
422 `invalid_rate_keys` (+ `missing`, `unknown`), 422 `invalid_rate` (field `rates.<key>`;
0–100, ≤ 4 decimals), 422 `notice_period` (+ `earliest_effective_from` = today IST + 15
days) for a real schedule other than the first, 422 `effective_in_past` (+
`earliest_effective_from` = today) for a placeholder or the first real one, 409
`concurrent_publish`.

**The first real schedule (BE-12).** While no non-placeholder schedule has ever been
published — in force or future-dated — nobody has relied on a confirmed rate, so the first
real schedule may take effect today. Detected in the same query that picks the next
version number, so two first schedules published at once cannot both skip the notice
(the second gets 409 `concurrent_publish`). Every later real schedule needs the 15 days.
The audit row's `after_value.first_real_schedule` records which case applied.

### `POST /commission-rate-schedules/{schedule_id}/re-rate` (Admin)
Body `{"reason": "…"}`. Rates every entry waiting with `placeholder_rates` — at the
associate's individual rate on its agreement date if they have one (BE-12), else at the
schedule in force on its agreement date when that is a real schedule, otherwise at this
one. Audited. → `{schedule_id, rated, total_amount, still_pending}`. 404 `not_found`,
409 `schedule_is_placeholder`, 422 `reason_required`.

### `GET /payout-batches?status=` → `{items: [batch]}` (no lines), newest month first
### `GET /payout-batches/{batch_id}` → batch with `lines` (404 plain when missing)

### `POST /payout-batches` → 201 batch with lines
Body `{"period": "YYYY-MM"}`. Takes every approved, rated entry created before the end of
that month (IST) and not in another batch. Errors: 422 `invalid_period`; 409
`period_not_ended`, `draft_batch_exists` (+ `batch_id`), `batch_exists`,
`period_out_of_order` (+ `latest_period`), `placeholder_rates` (+ `pending_entries`: entries
of the month still without confirmed rates), `no_approved_entries`.

### `POST /payout-batches/{batch_id}/approve`
Body optional `{"reason": "…"}`. Never the preparer — 409 `self_approval`; an Admin may,
with a reason (422 `reason_required` without one), audited `segregation_override`. 409
`invalid_transition` unless draft.

### `POST /payout-batches/{batch_id}/mark-paid`
Body `{"lines": [{"associate_id": "GNA-…", "bank_reference": "UTR…"}]}` — every line with
`net > 0` needs a reference (lines that net to nothing need none). Entries become `paid`.
Errors: 409 `invalid_transition` (not approved), 422 `unknown_associate` (+ `associate_ids`),
422 `missing_bank_reference` (+ `associate_ids`), 422 `invalid_bank_reference`.

### `POST /payout-batches/{batch_id}/cancel`
Body `{"reason": "…"}`. Draft only (409 `invalid_transition`); its entries return to the pool.

### Exports (UTF-8 BOM, IST + UTC columns, injection-safe cells)
* `GET /exports/commissions.csv?date_from=&date_to=` — every entry (Accounts/Admin).
* `GET /exports/tds.csv?financial_year=2026-27` (default: current FY) — one row per payout
  line of the year's non-cancelled batches, **with the full PAN** (decrypted through the
  audited helper; one audit row per associate, one per export). 422
  `invalid_financial_year`. Accounts/Admin.
* `GET /exports/payout-bank-{batch_id}.csv` — **Accounts only** (Admin 403). Approved or
  paid batches only (409 `batch_not_approved`). Columns `associate_id,
  account_holder_name, account_number, ifsc, amount_inr, narration, bank_reference` — the
  only place a full account number appears; each decryption and the export are audited.
  Spreadsheet programs drop leading zeros of account numbers: import as text.

### `GET /commission-queries?status=&overdue=&page=&page_size=`
`{items: [query + {overdue, entry}], page, page_size, total}`, oldest due first.

### `POST /commission-queries/{query_id}/respond` → query
Body `{"response": "…"}`. 404 `not_found`, 409 `already_answered`, 422 `invalid_response`.

### `POST /internal/check-commission-queries` (Admin or `X-Internal-Key`)
→ `{"overdue_queries": n}` or `{"status": "skipped", "reason": "already_running"}`.
One Accounts email (`commission_queries_overdue`) per run; each query alerted once.
Scheduled in `deploy/scheduler.yaml` (weekdays 10:00 IST, unconfirmed — BE3-1).

### `GET /network/summary?date_from=&date_to=` (Admin, Management)
Period = IST days, default this month so far, at most 366 days (422 `invalid_period`).

```json
{"period": {"date_from": "2026-10-01", "date_to": "2026-10-13"}, "generated_at": "…",
 "associates": [{"associate_type": "preferred_associate", "capability": null, "level": "state_head",
                 "status": "active", "count": 1}],
 "seats": [{"state_lgd_code": 19, "state_name": "West Bengal", "state_heads": {"held": 1, "max": 2},
            "districts": {"with_head": 1, "total": 23}, "blocks": {"with_associate": 1, "total": 345}}],
 "onboardings": {"by_status": {"draft": 0, "submitted": 0, "verified": 1, "closed": 0},
                 "by_current_stage": {"agreement_signed": 1}},
 "verified_revenue": {"credits": "21780.00", "reversals": "0.00", "net": "21780.00"},
 "commissions": {"earned": "1600.00", "reversed": "0.00", "approved": "0.00", "paid_net": "0.00",
                 "paid_tds": "0.00", "pending_count": 0},
 "overdue": {"verification_calls": 0, "survey_reviews": 0, "payments_awaiting_verification": 0,
             "commission_queries": 0}}
```

`verified_revenue` and `commissions` (except `pending_count`) are for the period: ledger
entries, entries created, entries approved, payouts paid within it. Everything else is
now. Overdue payment *milestones* are not included (computed per project, not stored).
`by_current_stage` excludes closed onboardings. Per-Associate figures (BE-12):
`GET /associates/{associate_id}/performance` — [commission_rates_individual.md](commission_rates_individual.md).

### `GET|POST /associates/{associate_id}/commission-rate` (BE-12)
An Associate's individual rate: read (Admin, Management), set or clear (Admin) —
[commission_rates_individual.md](commission_rates_individual.md).

## Changed elsewhere

* `GET /dashboard/associate-applications/{type}/{id}` (and the status / approve
  responses): `pan` is now decrypted on demand for the Admin and every decryption is
  audited (`pii_decrypted`); new field `pan_last4`. The PAN is encrypted at rest
  (`pan_encrypted`), like the bank account.
* `POST /dashboard/payments/{id}/verify` and `/dishonour` now also write and reverse
  commission entries (same transaction); their responses are unchanged.
