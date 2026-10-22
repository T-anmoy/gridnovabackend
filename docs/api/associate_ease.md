# BE-10 — Associate ease: referral links and estimated commission

Written from the code: `app/associates/referrals.py`, `app/api/routes/associate_referrals.py`,
`app/api/routes/public_intake.py`, `app/api/routes/public_booking.py`,
`app/api/routes/dashboard.py` (leads), `app/api/lead_filters.py`,
`app/commissions/estimate.py`, `app/api/routes/associate_onboarding.py`. Migration
`0052_associate_referrals`. Conventions (auth, errors, money as decimal strings, UTC ISO
times) are in [README.md](README.md).

---

## 1. Referral links

An Associate shares `/estimate?ref=<their Associate ID>` (WhatsApp, or printed as a QR
code). The website forwards `ref` with the customer's estimate request or call booking.

### 1.1 `GET /api/v1/associate/referral-link` — `ReferralLinkResponse`

Associate-authenticated (Associates and Preferred Associates alike — there is no separate
Preferred route; every `/api/v1/associate/...` route serves both types).

```json
{"associate_id": "GNA-WB-26-00001", "share_path": "/estimate?ref=GNA-WB-26-00001"}
```

`share_path` is relative to the public website: the frontend prefixes its own origin and
never builds the ID itself.

### 1.2 Public requests gain two optional fields

`POST /api/v1/public/estimate-request` (`EstimateRequest`) and `POST /api/v1/public/booking`
(`BookingRequest`):

| Field | Type | Meaning |
|---|---|---|
| `ref` | string, optional | An Associate ID from the link. Case and spaces are ignored (`" gna-wb-26-00001 "` works). |
| `share_with_referrer` | boolean, optional | The consent line **"Share my details with the Grid Nova Associate who referred me"**. Send `true` only when the customer ticks it. |

```json
{"name": "Rina Das", "email": "rina@example.com", "phone": "9830012345",
 "state": "West Bengal", "city": "Siliguri", "pin_code": "734001",
 "ref": "GNA-WB-26-00001", "share_with_referrer": true}
```

Rules:

- When `ref` names an **active** Associate, the lead is stamped with
  `referred_by_associate_id` and `share_with_referrer`, and an audit row is written
  (`entity_type "lead"`, `action "referral_recorded"`,
  `after_value {referred_by_associate_id, share_with_referrer, source}`).
- When `ref` is unknown, malformed, not a string, longer than 64 characters, or names a
  suspended or deactivated Associate, it is **ignored silently**: the request succeeds
  exactly as without it, nothing is recorded, and `share_with_referrer` is not stored.
  A customer never sees an error because of a bad link.
- Responses are unchanged: `{lead_id, success}` (estimate) and the usual booking response.
  Neither carries the referral.
- Rate limits, the per-email cooldown and the daily booking cap are unchanged.
- `POST /api/v1/pricing/instant-estimate` (the calculator) creates no lead, so it takes no `ref`.
- The lead's `source` stays `website_estimate_form` / `website_booking` (not `referral`), so
  conversion-by-source analytics are unchanged; the referral is its own field.

**Referral is information, not attribution.** It does not touch the attribution lock or
the commission rules: the first complete onboarding verified by Grid Nova still decides who
the customer is attributed to (`app/onboarding/service.py`). If the referring Associate later
onboards the customer, the existing rules apply as for anyone else. Open question **REF-1**
(`tunable`, `[UNCONFIRMED]`, `config/associate_onboarding.yaml referrals.auto_attribute`):
*Should a customer who came through an Associate's referral link be automatically attributed
to that Associate (today the first verified submission decides)?* Only `false` is
implemented; the app refuses to start with `true`.

### 1.3 `GET /api/v1/associate/referrals` — `ReferralListResponse`

Associate-authenticated. The caller's **own** referred enquiries only — never another
Associate's, including for a Preferred Associate over their territory. Newest first.
Query: `page` (≥1, default 1), `page_size` (1–100, default 25).

```json
{"items": [
  {"lead_id": 812, "created_at": "2026-10-22T06:14:03.112000+00:00",
   "customer_name": "Rina Das", "location": "Siliguri, West Bengal", "pin_code": "734001",
   "enquiry_kind": "estimate", "status": "in_progress", "share_with_referrer": true,
   "customer_mobile": "9830012345"},
  {"lead_id": 790, "created_at": "2026-10-21T11:02:45.000000+00:00",
   "customer_name": "Amit Roy", "location": null, "pin_code": null,
   "enquiry_kind": "call_booked", "status": "received", "share_with_referrer": false}
 ], "page": 1, "page_size": 25, "total": 2}
```

| Field | Meaning |
|---|---|
| `customer_name` | As entered on the form. |
| `location` | City and state as the customer typed them (`"Siliguri, West Bengal"`), or null. The public forms do not ask for a district, so there is no district field. |
| `pin_code` | From the estimate form when given, else null. |
| `enquiry_kind` | `estimate` (estimate request), `call_booked` (Book-a-Call), `other` (never expected). |
| `status` | A plain key, worded by the frontend: `received` (new), `in_progress` (qualifying / qualified / proposal being drafted), `proposal_sent` (sent or in negotiation), `won`, `closed` (lost). A submitted estimate form usually qualifies the lead at once, so `in_progress` is the common first status. |
| `share_with_referrer` | The customer's consent. |
| `customer_mobile` | **Present only when `share_with_referrer` is true** (the number given on that enquiry). Absent — the key is omitted, not null — otherwise. |

Never included: the customer's email, address, or anything about the lead beyond the above.

### 1.4 Staff leads

`GET /api/v1/dashboard/leads` items and `GET /api/v1/dashboard/leads/{lead_id}` (non-Engineer)
gain:

```json
"referred_by": {"associate_id": "GNA-WB-26-00001", "full_name": "Sourav Ghosh"}
```

or `null`. The Engineer detail view is unchanged.

`GET /dashboard/leads` and `GET /dashboard/leads/export` accept `referred_by_associate_id`
(an Associate ID; case and spaces ignored), applied inside the caller's role scope like the
other lead filters (`staff_contracts.md` §3). An unknown ID matches nothing; a malformed one
is a structured 422 `invalid_associate_id` (field `referred_by_associate_id`). The export's
columns are unchanged.

The admin associate detail (`GET /api/v1/dashboard/associates/{associate_id}`, Admin only)
already returns the Associate's `mobile` — usable for a "share the sign-in link on WhatsApp"
button. No public or associate read path exposes another Associate's mobile.

---

## 2. Estimated commission — "what will I earn?"

`GET /api/v1/associate/onboardings` items and `GET /api/v1/associate/onboardings/{onboarding_id}`
gain `estimated_commission`:

```json
"estimated_commission": {
  "amount": "5000.00", "rate": "5.0000", "basis": "accepted_proposal",
  "value_used": "108900.00", "basis_amount": "100000.00", "pending_reason": null
}
```

or `null`.

**It is an estimate only.** Nothing is written when it is shown. The commission an Associate
is actually paid comes only from the commission ledger (`commissions.md`), entry by entry as
Grid Nova verifies the customer's payments, and can differ (a payment dishonoured, a rate
change before the agreement is signed, paise rounding across instalments, a project closed).
Show it as an estimate.

| Field | Meaning |
|---|---|
| `basis` | Where the value comes from: `accepted_proposal` — the customer's accepted proposal; else `indicative_estimate` — their latest list-priced indicative estimate. With neither, `estimated_commission` is `null`. |
| `value_used` | The customer price used (decimal string, ₹): the accepted proposal's total, or the estimate package's final price (incl. GST). When the estimate shows both packages, the one chosen by `config/commissions.yaml estimate.indicative_package_when_several` (`lowest_final` today — **C5-EST**, `[UNCONFIRMED]`). |
| `basis_amount` | What the rate applies to, by the ledger's own rule (`commissions.basis`): the value **without GST** under `basic_pro_rata` (value × basic ÷ final), or the value as is under `gross`. `null` when it cannot be known (a custom quote has no basic price). |
| `rate` | The Associate's own personal-sale percentage (4 decimals) for their type and capability/level, from the **confirmed** rate schedule in force on the agreement date — today, until the agreement is signed. Rates are published by an Admin (`commissions.md`); none is in config. `null` while no confirmed schedule is in force (registry C5-RATES). |
| `amount` | `basis_amount × rate ÷ 100`, to the paisa — the same rounding the ledger uses. `null` when either is unknown. |
| `pending_reason` | `null`, or why there is no amount — the ledger's own reasons: `placeholder_rates` (no confirmed schedule), `basis_unavailable` (no basic price). |

`null` also when the customer record is closed, or the Associate would not earn
(`commissions.deactivated_associates_earn`). **Money is owner-only**: a Preferred Associate
viewing a customer in their territory (not their own) always gets `null` — territory
commission is not estimated here.

For a customer whose whole accepted value is paid, the ledger's personal-sale entry equals
this estimate (`tests/test_associate_ease.py`).

---

## 3. Registry

| ID | Blocking | Question |
|---|---|---|
| REF-1 | tunable | Should a customer who came through an Associate's referral link be automatically attributed to that Associate (today the first verified submission decides)? |
| C5-EST | tunable | When an Associate's customer has been shown both packages and has not accepted a proposal yet, which package should the Associate's estimated commission be worked out on? |
