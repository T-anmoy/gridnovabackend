# Direct customers' payments — API contract (BE-20)

A **direct customer** is a lead with no associate: a website enquiry, a staff-entered lead, or a
Proposal Studio proposal converted to a lead. Their payments use the same machinery as associate
customers': the same ledger, verification (second pair of eyes), dishonour and re-presentation,
cancel, receipts (numbering, PDF, the customer's token link) and the same payment JSON. Rows are
keyed by the lead's project, with `lead_id` set and `onboarding_id` null (migration
`0058_direct_customer_payments`). An associate customer's payments are unchanged.

Money is a decimal string (`"54450.00"`). `{lead_id}` is the lead's id.

## Who

| | Record | Verify / deposit / dishonour / cancel / proof upload / receipt resend | Read (schedule, payments, receipt) |
|---|---|---|---|
| Accounts, Admin | yes | yes (not a payment you recorded — Admin only, with `reason`) | yes |
| Management | — | — | yes |
| Sales — the lead's owner, or an unassigned lead | — | — | yes |
| Sales — someone else's lead | — | — | 403 |
| Operations, Engineer, Network Manager, Proposal Desk | — | — | 403 |
| Associate credential, no credential | 401 | 401 | 401 |

The lead's region applies (`employees.regions`, BE-19): 403 `"This record is outside the regions
you work in."` when the lead's state is known and outside them. Accounts, Admin and Management
need two-step sign-in (`two_factor_required`), as everywhere.

A lead brought by an associate answers **409** `{"code": "associate_customer", "onboarding_id": N}`
on every route below: its payments stay on `/dashboard/onboardings/{N}/payments`.

## When the schedule exists

It is created once, when the lead's proposal becomes **accepted**:
- online on the accept link (`POST /api/v1/proposals/accept/{token}`);
- on the Proposal Studio page after the proposal was converted;
- by converting a Studio proposal the customer already accepted.

It is also created lazily the first time a route below is read. Creation is idempotent in every order.
Before acceptance, `milestones` is `[]` and every total is null except `paid_verified`.

**Terms.** The schedule is the accepted proposal's own payment terms:
- the document's `payment_terms` (label and percent);
- the amounts from its `figures.payments`, which sum to the document total exactly. A ≤ 10 kWp
  proposal is usually 20/60/20 (`On Order` · `Against Material Bill` · `Before Commissioning`); a
  larger one 25/60/15 (`Advance along with the order` · `Against delivery of active materials …` ·
  `On execution …`).

A proposal drafted before the document existed falls back to the price-list template (20/60/20)
against its total. The **accepted total is the schedule's sum**, fixed when the schedule was made.

**Due status** (`due_status`): `not_due | due | partially_paid | paid`, against the cumulative
verified amount, applied in milestone order.
- The advance is `due` from the proposal's acceptance, and `overdue` after
  `payments.thresholds.milestone_overdue_days`.
- Later milestones stay `not_due` until paid, because direct projects have no execution stage
  log (registry DP-2).
- A milestone is `paid` when the verified total covers it. `milestone_id` on a payment is a
  label, not an allocation.

## `GET /api/v1/dashboard/leads/{lead_id}/payments`

The shape of `GET /dashboard/onboardings/{id}/payments`, keyed by lead:

```json
{
  "onboarding_id": null, "lead_id": 12, "proposal_id": 40,
  "totals": {"accepted_total": "272250.00", "paid_verified": "54450.00",
             "awaiting_verification": "0.00", "balance": "217800.00"},
  "milestones": [{"milestone_id": 7, "number": 1, "label": "On Order", "percent": "20.00",
                  "amount_due": "54450.00", "paid_toward": "54450.00", "outstanding": "0.00",
                  "trigger": "accepted", "due_status": "paid", "due_since": "…", "overdue": false}, …],
  "payments": [<payment JSON, staff view>, …],
  "enabled_modes": ["cheque", "neft", "rtgs", "imps"],
  "proof_required": false,
  "project": {"onboarding_id": null, "lead_id": 12, "customer_name": "…", "customer_email": "…",
              "project_reference_id": "GNE-WB-RES-2026-00012", "current_stage": null,
              "accepted_total": "…", "paid_verified": "…", "awaiting_verification": "…", "balance": "…"}
}
```

Payments are newest first. **Payment JSON** is the existing one plus `lead_id`, which is null on
an associate customer's payment and set on a direct one; `onboarding_id` is null here.

## `POST /api/v1/dashboard/leads/{lead_id}/payments` → 201

Accounts / Admin. Body (the associate customer's `PaymentIn`):
`{amount, mode, reference, bank_name?, instrument_date?, payer_name?, milestone_id?}`.

| Rule | Error |
|---|---|
| The customer has accepted the proposal | 409 `not_accepted` |
| The accepted proposal has priced terms | 409 `no_accepted_total` |
| `amount` > 0, at most two decimals | 422 `invalid_amount` |
| `mode` ∈ cheque, neft, rtgs, imps (no cash, UPI or demand draft) | 422 `mode_not_enabled` |
| `reference`: the UTR for NEFT/RTGS/IMPS, the cheque number for a cheque (3–100 characters) | 422 `reference_required` |
| A cheque needs `bank_name` and `instrument_date` | 422 `instrument_details_required` |
| `instrument_date` not after today (IST) | 422 `date_in_future` |
| `milestone_id` on this customer's schedule | 422 `unknown_milestone` |
| Total recorded (recorded + deposited + verified, not dishonoured or cancelled) ≤ the accepted total | 409 `exceeds_outstanding` with `accepted_total`, `already_recorded`, `outstanding` |
| Not the same live instrument (mode + reference + bank) | 409 `duplicate_payment_reference` |

- **Partial payments:** allowed and cumulative.
- **Over-payment check:** the project row is locked first, so two simultaneous payments cannot
  both pass it.
- **Re-presented cheque:** the same cheque presented again after a bounce on this customer is
  accepted and linked through `previous_payment_id`.
- **Response:** the payment JSON plus `project`.
- **Associate customers:** their over-payment code is still `exceeds_project_total` (unchanged).

## `GET /api/v1/dashboard/leads/{lead_id}/payment-schedule`

`{"onboarding_id": null, "lead_id", "totals", "milestones"}`, the same shapes as above. There is no
`PUT`: the schedule is what the customer signed.

## `GET /api/v1/dashboard/leads/{lead_id}/payments/{payment_id}/receipt`

The receipt of a verified payment, staff view: `{payment_id, receipt_number, email_status,
emailed_at, pdf_available, receipt_pending_email, held, voided, url, email_skipped_reason,
held_reason, render_error, voided_at, void_reason, pdf_url, pdf_url_expires_at}`.
- `pdf_url` is a short-lived signed link to the PDF. It is null while the receipt is held or
  not rendered yet.
- `url` is the customer's own link (`GET /api/v1/public/receipts/{token}/pdf`). It is null while
  the receipt is held or void.
- 404 when the payment is not this lead's, or not verified yet.

**Receipt wording (registry P6) is still DUMMY.** Every receipt is numbered at verification but
**held**: not rendered, not emailed, and the public link answers 404. Once Grid Nova supplies the
wording, the held receipts are released (`POST /dashboard/payment-receipts/release-held`). The
footer says it is not a GST tax invoice; tax invoices are the open P6 question.

## Acting on one payment — the shared routes

`/api/v1/dashboard/payments/{payment_id}` and its `/proof`, `/deposit`, `/verify`, `/dishonour`,
`/cancel`, `/receipt/resend` work on a direct customer's payment exactly as on an associate
customer's. Who may act is in the table above: Sales may not upload proof on a direct customer's
payment.

- **Verify:** writes one ledger credit and numbers the receipt. Nobody verifies a payment they
  recorded (409 `self_verification`); only an Admin may, with `reason`. A second verification is
  409 `invalid_transition`, including two at the same moment.
- **Dishonour** (a verified, recorded or deposited payment, with `reason`): a verified payment
  gets a ledger reversal and its receipt becomes void.
- **No commission** is ever earned on a direct customer's payment.

**Lists:**
- `GET /dashboard/payments` accepts `lead_id`. Sales sees its own and unassigned leads' direct
  payments there.
- `GET /dashboard/payment-milestones` lists direct customers' milestones after associate
  customers', with `lead_id` set and `onboarding_id` null.
- `GET /dashboard/work-queues` counts their overdue advance.

## Events and alerts

Every record, verify and dishonour is audited (`payment` / `payment_schedule` entries). It also
lands on the lead's timeline as `payment_recorded`, `payment_verified` or `payment_dishonoured`
(new timeline event types).

Staff alerts (`config/access_control.yaml`):

| Alert | Goes to |
|---|---|
| `payment_recorded` | Accounts and the lead's assigned employee |
| `payment_verified` (new; direct customers only) | Accounts and the lead's assigned employee |
| `payment_dishonoured` | Accounts, Admin and the lead's assigned employee |

The email links to `/dashboard/leads/{lead_id}`. An associate customer's payment alerts are
unchanged: they name no lead, so they never went to an assigned employee and still do not.

## Not built (registry)

- **DP-1** (`blocks_feature`): refund and cancellation of a direct customer's order.
- **P6:** receipt wording and tax invoices.
- **DP-2:** when later milestones fall due.
- **C5-11** (TDS) and **C5-8** (commission query windows) stay open; they do not apply to
  direct customers.
