# HF-C — associate equipment catalogue, unmatched units, payment-list names

Migration `0043_unmatched_units`. Code: `app/api/routes/associate_equipment.py`,
`app/execution/register.py`, `app/api/routes/execution_register_admin.py`,
`app/api/routes/payments_admin.py`.

## 1. Associate catalogue — `GET /api/v1/associate/equipment` (associate)

Any active associate (inactive → 403). Staff credentials are not accepted (see
README: 401 in dev-header mode, 403 with a real staff token). Separate from the staff
`GET /api/v1/equipment/`, which associates can never call.

Query: `category?` (`module` | `inverter` | `structure` | `bos_component`; anything else
422), `q?` (≤ 100; case-insensitive match on brand or model name).
Rate limit: 60 requests per minute per associate → 429 `{"code": "rate_limited"}`.

Response:
```json
{"items": [{"id": 12, "category": "module", "brand": "Sova Solar", "model_name": "SS-540M",
            "specification": null, "is_dcr_compliant": true, "is_placeholder": false}]}
```
Active items only, ordered by category, brand, model. Development placeholder rows
(`is_placeholder: true`) are returned only outside production. **No price field of any
kind is ever returned** (tested).

## 2. Unmatched units

When the model on the nameplate is not in the catalogue, a module or inverter may be
registered with `equipment_id: null` and the brand and model **exactly as printed**.

`UnitIn` / `UnitPatch` (associate `POST/PATCH .../execution/units`, bulk, and the same
staff routes) gain:

| Field | Type | Rule |
|---|---|---|
| `brand_as_printed` | string ≤ 100, nullable | required for module/inverter when `equipment_id` is null |
| `model_as_printed` | string ≤ 100, nullable | required for module/inverter when `equipment_id` is null |

- Missing either → `equipment_required` (field `brand_as_printed` or `model_as_printed`);
  inside a create this arrives as 422 `register_rows_invalid` with
  `errors: [{row, code: "equipment_required", field, message}]`. Over 100 characters →
  422 (`printed_too_long` from the service, or FastAPI's validation error from the schema).
- Modules still need `is_dcr` (the associate's declaration; `dcr_required`).
- With an `equipment_id` the printed fields are optional and stored as given.
- A PATCH with `"equipment_id": null` plus the printed fields turns a matched unit back
  into an unmatched one.
- Global serial uniqueness is unchanged.

Every unit object (associate and staff) now carries `brand_as_printed` and
`model_as_printed`. Unmatched units have `equipment: null`.

**Staff only** (never on any associate response):
- unit field `catalogue_unmatched: true|false`;
- register / execution-status `flags` include `catalogue_unmatched` while any active unit
  is unmatched — so the staff execution queue marks it (`flags`, `attention_reasons`) and
  `GET /api/v1/dashboard/execution?needs_attention=true` lists it;
- post-commit email `catalogue_unmatched` to Operations (OPERATIONS_NOTIFY_EMAILS, else
  STAFF_NOTIFY_EMAILS) when units are added unmatched or a unit is changed to unmatched;
- `make_not_in_package` (PM-A) also applies to unmatched units, using `brand_as_printed`.

## 3. Staff match action

`POST /api/v1/dashboard/onboardings/{onboarding_id}/execution/units/{unit_id}/match`
— **admin, operations** (other roles 403).

Body:
```json
{"equipment_id": 34, "reason": null}
```
`reason` (≤ 2000) is required only after the register is verified (then admin only:
409 `register_locked` / 422 `reason_required`, as for every register change).

Rules: the item must exist (422 `unknown_equipment`), be active (422
`equipment_inactive`), be of the unit's category (422 `equipment_category_mismatch`),
and for a module agree with the declared `is_dcr` (422 `dcr_mismatch`). An inverter takes
the item's DCR value. 409 `already_matched`, 409 `unit_voided`, 422 `not_matchable`
(category `other`). The printed brand/model stay on the unit as the record of what was
declared. Audited `installed_unit` / `matched` with before/after.

Response: `{unit: <staff unit>, register: <staff register block>}`.

To match a model that is not in the catalogue yet: create it first with
`POST .../execution/catalogue` (see [pma_pricing.md](pma_pricing.md) §7), then match.

## 4. Register verification

`POST /api/v1/dashboard/onboardings/{onboarding_id}/execution/register/verify` refuses
while any active unit is unmatched:
```json
{"detail": {"code": "units_unmatched", "message": "Match every unit to a catalogue item before verifying the register.", "unit_ids": [17]}}
```
(409; checked before `register_incomplete`).

## 5. Customer name on staff payment lists

Every row of `GET /api/v1/dashboard/payments` and `GET /api/v1/dashboard/payment-milestones`
gains `customer_name` and `project_reference_id` (from the payment's project; also for
payments with no onboarding). Same role scoping as before (PAY_READERS; Sales own or
unassigned; Operations and engineers 403).

## 6. Message keys

`GET /api/v1/associate/payments/instructions` → `message_key`
`payments.instructions.not_ready` (bank details still placeholders; `account: null`) or
`payments.instructions.pay_only_here` (with `account`).
