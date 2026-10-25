# S1 — On-site site survey (Steps 1–8)

Steps 1–4: migration `0044_site_survey`. Steps 5–8 (BE-2): migration `0046_survey_review`.
Code: `app/survey/` (`config.py`, `validation.py`, `status.py`, `results.py`, `signatures.py`,
`review.py`, `corrections.py`, `changes.py`, `report.py`, `queue.py`) and
`app/jobs/survey_reviews.py`; routes `app/api/routes/associate_survey.py` (associate) and
`app/api/routes/survey_admin.py` (staff). Config: `config/site_survey.yaml` (registry
SRV-1…SRV-16, all `[UNCONFIRMED]`).

Grid Nova's paper "Checklist to Assess Potential Solar Rooftop System" as a guided survey.
It **extends the site assessment** (`ProposalInputs`) — there is no separate survey record.
The client code is the existing `project_reference_id` (`GNE-…`). Steps 5–8 (§8–§11): the
Engineer's feasibility review, change control with re-verification of proposals, the
survey report PDF, and the review queue with its overdue alert.

## 1. Writing survey fields (associate)

Survey fields are written through the existing onboarding endpoints, under the existing
edit rules (owner only; draft or submitted; an edit to a submitted customer returns it to
draft; verified/closed → 409 `not_editable`):

- `POST /api/v1/associate/onboardings` (create)
- `PATCH /api/v1/associate/onboardings/{onboarding_id}` (partial update)

They come back in `site_assessment` of every onboarding response.

| Field | Type / values | Notes |
|---|---|---|
| `survey_variant` | `individual_house` \| `housing_society` \| `commercial_roof` | default `individual_house` |
| `consumption_history` | list (≤ 12) of `{month: "YYYY-MM", units_kwh, bill_inr?}` | units required, both ≥ 0; stored sorted, numbers (not strings — the staff `/proposal-inputs` API reads this list) |
| `roof_accessible` | bool | |
| `roof_access_note` | string ≤ 1000 | blank → `null` |
| `roof_material` | `concrete_flat` \| `tin_shed` \| `asbestos` \| `other` | |
| `roof_material_other` | string ≤ 200 | required (survey-complete) when material is `other` |
| `roof_orientation` | `south` \| `east` \| `west` \| `north` \| `mixed` \| `unknown` | |
| `roof_obstacles` | list of `water_tank`, `stair_room`, `trees`, `nearby_building`, `overhead_line`, `antenna_dish`, `other` | `[]` = surveyed, none; stored de-duplicated in this order |
| `roof_sections` | list (≤ 6) of `{label ≤ 40, length_m, width_m, obstacle_area_sqm?}` | metres > 0 and ≤ 500; returned as decimal strings (`"10.00"`); `[]`/`null` clears |
| `shadow_free_area_sqft` | number ≥ 0 | **server-derived** from `roof_sections` when there are sections (Σ(L×W − obstacle) m² × 10.7639, 1 decimal); entered directly otherwise |
| `roofs_available_count`, `roofs_considered_count` | int 0–100 | considered ≤ available |
| `future_roof_expansion_planned` | bool | |
| `water_available`, `indoor_wall_available`, `tenant_noc_available` | bool | |
| `inverter_location` | `terrace` \| `indoor_wall` \| `outdoor_wall` \| `other` | |
| `distance_panel_to_inverter_m`, `distance_inverter_to_lt_panel_m` | decimal ≥ 0 (number or string) | ≤ `distance_max_m` (300); returned as decimal strings (`"12.35"`) |
| `distance_method` | `tape` \| `laser` \| `estimated` | |
| `society_flat_count` | int ≥ 1 | |
| `society_noc_status` | `obtained` \| `in_progress` \| `not_required` \| `not_available` | |
| `contract_demand_kva` | number > 0 | |

The existing site-assessment fields the checklist also uses (`discom`, `consumer_number`,
`connection_phase`, `sanctioned_load_kw`, `roof_area_sqft`, `floors`, `shading_condition`,
`roof_ownership_confirmed`, `roof_age_years`, …) are unchanged. `discom` stays free text;
`GET /options` → `survey.discom_options` lists the suggested names.

**Derived averages.** When a request sends `consumption_history` without
`avg_monthly_units_kwh` / `avg_monthly_bill_inr`, the server sets them to the mean of the
months that have a value (Decimal, 2 decimals). An average sent in the same request is
kept as sent.

**422 codes** (structured detail, `field` set; `index` = list position where relevant):

| code | field | when |
|---|---|---|
| `invalid_month` | `consumption_history` | month not `YYYY-MM` (or before 2000) |
| `future_month` | `consumption_history` | after the current month (IST) |
| `duplicate_month` | `consumption_history` | a month twice |
| `units_required` | `consumption_history` | a month without `units_kwh` |
| `too_many_months` | `consumption_history` | more than `max_consumption_months` (+ `max_months`) |
| `obstacle_exceeds_section` | `roof_sections` | obstacle area > length × width |
| `derived_field` | `shadow_free_area_sqft` | sent together with sections, or while sections are stored |
| `out_of_range` | the distance | above `distance_max_m` (+ `max_m`) |
| `roofs_considered_exceeds_available` | `roofs_considered_count` | |

Types, enums, negatives and list lengths are FastAPI's standard 422 (`detail` is a list).

**Signatures are cleared by edits.** With `resign_after_edit: true` (SRV-11), changing any
survey field (every field named in the config sections/variants/conditions, plus the
fields above and the two averages) after a signature clears **both** signatures and the
declaration. The `updated` audit row then carries `before.signatures` and
`after.signatures = "cleared_after_edit"`.

## 2. `GET /api/v1/associate/onboardings/options` → `survey`

```json
{"variants": {"individual_house": [{"key": "connection", "fields": ["discom", "consumer_number", "connection_phase", "sanctioned_load_kw"]}, …],
              "housing_society": […], "commercial_roof": […]},
 "conditional": [{"section": "roof", "when": {"roof_material": "other"}, "require": ["roof_material_other"]},
                 {"section": "roof", "when": {"roof_ownership_confirmed": false}, "require": ["tenant_noc_available"]}],
 "discom_options": ["WBSEDCL", "CESC", "NBPDCL", "SBPDCL", "Other"],
 "roof_materials": […], "roof_orientations": […], "roof_obstacles": […], "inverter_locations": […],
 "distance_methods": […], "society_noc_statuses": […], "max_roof_sections": 6,
 "requested_consumption_months": 6, "min_consumption_months": 3, "max_consumption_months": 12,
 "distance_max_m": 300, "signature_max_kb": 500, "required_to_submit": true, "resign_after_edit": true}
```

## 3. Survey status

- Associate: `GET /api/v1/associate/onboardings/{onboarding_id}/survey` — owner, or a
  Preferred Associate whose territory holds the site (read-only). Others 403.
- Staff: `GET /api/v1/dashboard/onboardings/{onboarding_id}/survey` — admin, management,
  sales (own or unassigned leads), engineer. Operations/accounts 403. Adds
  `review.reviewed_by` (employee id).

```json
{"onboarding_id": 12, "variant": "individual_house",
 "sections": [
   {"key": "connection", "complete": false, "missing": ["discom", "connection_phase"]},
   {"key": "bills", "complete": false, "missing": ["consumption_history"],
    "months_recorded": 0, "months_required": 3, "months_requested": 6},
   {"key": "roof", "complete": false, "missing": ["roof_accessible", …]},
   {"key": "roof_area", "complete": false, "missing": ["roof_area_sqft", "shadow_free_area_sqft"]},
   {"key": "layout", "complete": false, "missing": ["inverter_location", …]}],
 "complete": false,
 "signatures": {"customer": {"signed": false, "signed_at": null}, "associate": {"signed": false, "signed_at": null}},
 "declaration": {"name": null, "declared_at": null},
 "required_to_submit": true, "grandfathered": false,
 "review": {"outcome": null, "notes": null, "conditions": [], "revisit_items": [], "reviewed_at": null,
            "revised_after_review": false},
 "report": {"generated_at": null}}
```

`review` is filled by §8 (`outcome` one of `feasible`, `feasible_with_conditions`,
`revisit_needed`, `not_feasible`; `revisit_items` = `[{section, field|null, note}]`;
`revised_after_review` = a survey field changed after that review, so it waits for a new
one). `report.generated_at` is set by §10.

A field is complete when it has a value (`false`, `0` and `[]` count). `bills` is complete
with `min_consumption_months` (3) months. Variant extras: `housing_society` →
`connection` also needs `society_flat_count`, `society_noc_status`; `commercial_roof` →
`contract_demand_kva`. Conditional extras as in `options.conditional`.

## 4. Survey results (live, never stored)

- Associate: `GET /api/v1/associate/onboardings/{onboarding_id}/survey/results` — same
  visibility as §3.
- Staff: `GET /api/v1/dashboard/onboardings/{onboarding_id}/survey/results` — same roles
  as §3. **Engineers get no `estimate` key.**

```json
{"onboarding_id": 12, "variant": "individual_house",
 "max_by_area_kw": 7.18, "max_by_load_kw": 5.0, "recommended_by_usage_kw": 3.0,
 "suggested_kw": 3, "limiting_factor": "usage", "requested_kw": 3.0,
 "bills": {"months_recorded": 3, "average_monthly_units_kwh": 400.0, "average_monthly_bill_inr": "3200.00",
           "implied_tariff_inr_per_kwh": "8.00"},
 "assumptions": {"area_sqft_per_kw": 100, "load_cap_ratio": 1.0, "target_offset_percent": 90.0},
 "warnings": [],
 "estimate": {"indicative": true, "size_kw": 3, "price_status": "list_price", "custom_quote_reasons": [],
              "priced_kw": 3, "phase": "SINGLE",
              "packages": {"NOVA_ASSURED": {"basic": "…", "gst": "…", "final": "…", "display_name": "Nova Assured",
                                            "net_after_subsidy": "…"},
                           "NOVA_SIGNATURE": {…}},
              "subsidy_inr": "78000.00", "subsidy_conditions": ["dcr_modules_required", "scheme_eligibility"],
              "price_list_version": 1}}
```

- BE-14: `area_sqft_per_kw` (100) and the bill-per-unit range are the solar estimate
  engine's (`config/solar_estimate.yaml`), and `estimate` carries the engine's subsidy,
  generation, savings and payback fields — see [solar_estimate.md](solar_estimate.md) §5.
- `max_by_area_kw` = `shadow_free_area_sqft` ÷ `area_sqft_per_kw`; `max_by_load_kw` =
  `sanctioned_load_kw` × `load_cap_ratio`; `recommended_by_usage_kw` = the existing sizing
  engine on the average monthly units (else the bill), at its target offset. Each is `null`
  when its input is missing.
- `suggested_kw` = the smallest of the three, rounded **down** to a whole kW (may be 0);
  `limiting_factor` = `area` | `load` | `usage` (ties in that order); both `null` when
  nothing can be sized.
- `estimate` prices `suggested_kw` with Grid Nova's price list (PM-A): segment from the
  variant (`individual_house` residential, `housing_society` residential society,
  `commercial_roof` commercial). Outside the list → `price_status: "custom_quote"`,
  `custom_quote_reasons` (PM-A codes), `packages: {}`. No size →
  `price_status: "not_enough_to_size"`. Money is decimal strings.
- `kW` values are JSON numbers (2 decimals).

**Warnings** — `{"code", "params"}` only, never English:

| code | params | when |
|---|---|---|
| `requested_above_suggested` | `requested_kw`, `suggested_kw`, `limiting_factor` | `desired_output_kw` > `suggested_kw` |
| `load_enhancement_needed` | `required_kw`, `sanctioned_load_kw`, `max_by_load_kw` | requested (else usage) size > `max_by_load_kw` |
| `implied_tariff_outside_range` | `implied_tariff_inr_per_kwh` (string), `min`, `max` | Σbill ÷ Σunits outside 3–20 ₹/unit |
| `units_bill_mismatch` | `field`, `entered`, `from_bills` (strings) | a stored average differs from the bills' mean by > 25 % |
| `three_phase_below_min` | `kw`, `min_kw` | three phase and requested (else suggested) kW < the price list's three-phase minimum (3) |
| `fragile_roof` | `roof_material` | `tin_shed` or `asbestos` |
| `overhead_line` | — | `overhead_line` in `roof_obstacles` |
| `roof_not_owned_no_noc` | — | `roof_ownership_confirmed` false and `tenant_noc_available` not true |
| `society_noc_missing` | `society_noc_status` | housing society and status not `obtained`/`not_required` |
| `area_implausible` | `reason` (`shadow_free_exceeds_total` \| `roof_area_large`), `shadow_free_area_sqft`, `roof_area_sqft`, `max_plausible_sqft` | |
| `duplicate_consumer_number` | — | another associate's open onboarding has the same consumer number (never named) |

## 5. Signatures (associate, owner only)

`POST /api/v1/associate/onboardings/{onboarding_id}/survey/signatures/{party}` —
`party` = `customer` | `associate`. Multipart:

| form field | |
|---|---|
| `file` | PNG (typed by its bytes), ≤ `signature_max_kb` (500 KB) |
| `declaration_name` | customer only, required, ≤ 200 (the name the customer typed) |
| `customer_confirms_data` | customer only, must be `true` |

201 → the survey status (§3). Server timestamp; stored privately at
`proposal-inputs/{id}/signature/{party}_{uuid}.png`; a new signature replaces the
reference (the old file is kept; the audit `survey_signed` has before/after keys).
Draft or submitted — signing a submitted customer returns it to draft. A **verified**
customer can be signed only for a party whose signature an applied correction cleared
(§9); it stays verified. Otherwise verified or closed → 409 `not_editable`. Errors: 422 `declaration_name_required`,
`confirmation_required`, `signature_not_png`; 413 `{"code": "file_too_large", "max_kb": 500}`
(the upload middleware refuses a body over 500 KB + 64 KB framing with a plain 413 first);
503 `storage_not_configured`. A Preferred Associate in the territory, a sibling or an
unrelated associate gets 403.

## 6. Submit gate

`POST /api/v1/associate/onboardings/{onboarding_id}/submit` additionally requires (when
`survey_required_to_submit` is true and the customer is not grandfathered) a complete
survey and both signatures. The 422 `incomplete` detail now always carries two more keys:

```json
{"detail": {"code": "incomplete", "message": "Before submitting, complete the site survey (bills, layout); get the site survey signed (associate).",
            "missing_documents": [], "missing_site_location": false, "site_location_unconfirmed": false,
            "missing_survey_sections": ["bills", "layout"], "survey_unsigned": ["associate"]}}
```

Both lists are empty when the survey is not required. Grandfathered: onboardings that were
submitted, verified or closed when migration 0044 ran (`survey_grandfathered`); drafts were
not. The home feed's `ready_to_submit` now also waits for the survey.

## 7. Roles

| Route | Staff roles | Associate |
|---|---|---|
| `GET /dashboard/onboardings/{id}/survey` | admin, management, sales (own/unassigned), engineer | 401 |
| `GET /dashboard/onboardings/{id}/survey/results` | same (engineer: no `estimate`) | 401 |
| `POST /dashboard/onboardings/{id}/survey/review` | admin, engineer | 401 |
| `POST /dashboard/onboardings/{id}/survey/report` | admin, engineer | 401 |
| `GET /dashboard/onboardings/{id}/survey/report` | admin, management, sales (own/unassigned), engineer | 401 |
| `GET /dashboard/survey-corrections` | admin, management, sales (own/unassigned), engineer | 401 |
| `POST /dashboard/survey-corrections/{id}/apply` | admin, engineer (never the requester) | 401 |
| `POST /dashboard/survey-corrections/{id}/reject` | admin, engineer (never the requester) | 401 |
| `POST /dashboard/proposals/{id}/reverification/clear` | admin, management, engineer (never the correction's applier) | 401 |
| `GET /dashboard/survey-reviews` | admin, management, sales (own/unassigned), engineer | 401 |
| `POST /dashboard/internal/check-survey-reviews` | admin, or `X-Internal-Key` | 401 |
| `GET /associate/onboardings/{id}/survey` | 401 | owner, territory Preferred (read) |
| `GET /associate/onboardings/{id}/survey/results` | 401 | owner, territory Preferred (read) |
| `POST /associate/onboardings/{id}/survey/signatures/{party}` | 401 | owner only |
| `POST /associate/onboardings/{id}/survey/corrections` | 401 | owner only |
| `GET /associate/onboardings/{id}/survey/corrections` | 401 | owner, territory Preferred (read) |
| `GET /associate/onboardings/{id}/survey/report` | 401 | owner; territory Preferred only when `territory_viewers_see_documents` is true |

Staff role refusals are a plain 403 (`detail` a string), as elsewhere.

## 8. Engineer's feasibility review (Step 5)

`POST /api/v1/dashboard/onboardings/{onboarding_id}/survey/review` — admin, engineer.
Management and sales read the review through §3 only.

```json
{"outcome": "revisit_needed",
 "notes": "Terrace area looks too large for the photos.",
 "conditions": null,
 "revisit_items": [{"section": "roof_area", "field": "roof_area_sqft", "note": "Measure the terrace again"}]}
```

| field | rule |
|---|---|
| `outcome` | `feasible` \| `feasible_with_conditions` \| `revisit_needed` \| `not_feasible` |
| `notes` | ≤ 2000; **required** for `revisit_needed` and `not_feasible` (trimmed; blank = missing) |
| `conditions` | list (≤ 20) of strings ≤ 500; **required, non-empty** for `feasible_with_conditions`; refused with any other outcome |
| `revisit_items` | list (≤ 20) of `{section ≤ 40, field? ≤ 60, note ≤ 500}`; **required** for `revisit_needed`, refused otherwise. `section` = a key of `options.survey.variants.*[].key`; `field` (optional) = a field that section can hold in any variant or condition |

200 → the staff survey status (§3) with the new `review`. A new review replaces the
previous one (the audit row `survey_reviewed` keeps before/after) and sets
`revised_after_review` back to `false`. Timeline `survey_reviewed`. The owning associate is
emailed (`associate_survey_reviewed`, only when `NOTIFY_ASSOCIATES` is on).

Errors (structured, `field` where shown):

| status | code | when |
|---|---|---|
| 409 | `survey_not_submitted` | customer not submitted or verified (draft, closed) |
| 409 | `survey_incomplete` | + `missing_survey_sections`, `survey_unsigned` (lists) |
| 422 | `notes_required` | `notes` |
| 422 | `conditions_required` | `conditions` |
| 422 | `revisit_items_required` | `revisit_items` |
| 422 | `not_applicable` | `conditions` or `revisit_items` sent with another outcome |
| 422 | `unknown_section` | `revisit_items` + `index`, `sections` |
| 422 | `unknown_field` | `revisit_items` + `index`, `fields` |
| 422 | `note_required` | `revisit_items` + `index` |
| 422 | `too_long` | `conditions` (an item over 500) |

**How the associate revisits** (existing edit rules): a *submitted* customer is edited
directly (`PATCH`, which returns it to draft and clears the signatures), signed again and
submitted again. A *verified* customer cannot be edited, so the change goes through a
correction (§9). Any change to a survey field after a review — an edit or an applied
correction, whatever the outcome — sets `revised_after_review = true`; the survey is back
in the queue (§11) and needs a new review.

**Associate home to-dos** (`GET /api/v1/associate/home` → `items`, message keys only):

| kind / `message_key` | priority | params |
|---|---|---|
| `survey_revisit_needed` / `todo.survey_revisit_needed` | 1 | `revisit_items`, `notes`, `via` (`"edit"` draft/submitted, `"correction"` verified) — until the survey changes |
| `survey_resign_needed` / `todo.survey_resign_needed` | 1 | `unsigned` (`["customer","associate"]`) — a verified customer whose signatures a correction cleared |

**Formal-proposal gate.** With `review_required_for_formal_proposal: true` (SRV-15) and a
survey-required customer (not grandfathered), these need a current review with outcome
`feasible` or `feasible_with_conditions`:

- `POST /api/v1/associate/onboardings/{id}/request-formal-proposal`
- `POST /api/v1/dashboard/onboardings/{id}/formal-proposal`
- `POST /api/v1/dashboard/proposals/draft` for a lead that has an associate onboarding

Otherwise 409 `survey_review_required` (+ `review_outcome`: `null` | `revisit_needed` | …,
`revised_after_review`: bool) — also when the survey changed after a passing review.
`not_feasible` always blocks, whatever the config or grandfathering:
409 `survey_not_feasible`.

## 9. Corrections and re-verification (Step 6)

### Associate

`POST /api/v1/associate/onboardings/{onboarding_id}/survey/corrections` — owner only.

```json
{"changes": {"roof_area_sqft": 950, "roof_sections": [{"label": "Main", "length_m": 10, "width_m": 8}]},
 "reason": "Measured the terrace again"}
```

- Only for a **verified** customer. Draft/submitted → 409 `edit_directly` (use `PATCH`);
  closed → 409 `not_editable`.
- `changes` keys: survey fields only (§1 table, the checklist's site-assessment fields and
  the two averages) — **never `consumer_number`** (identity for attribution) →
  422 `field_not_correctable`; anything else → 422 `not_a_survey_field` (+ `fields`).
  Values go through the same schema and rules as `PATCH`: wrong type/enum/range →
  422 `invalid_value` (+ `changes_field`); the §1 survey codes (`future_month`,
  `derived_field`, `out_of_range`, …) as there. Empty → 422 `no_changes`.
- `reason` ≤ 1000, required → 422 `reason_required`.
- One pending request per customer → 409 `correction_pending`.

201 →

```json
{"correction_id": 4, "onboarding_id": 12, "changes": {"roof_area_sqft": 950}, "reason": "…",
 "status": "pending", "requested_at": "2026-10-12T05:01:02+00:00", "reviewed_at": null, "review_note": null}
```

`changes` are echoed as sent (JSON). Timeline `survey_correction_requested`.

`GET /api/v1/associate/onboardings/{onboarding_id}/survey/corrections` — owner and
territory Preferred (read) → `{"items": [<as above>, …]}` newest first, every status.

### Staff

`GET /api/v1/dashboard/survey-corrections?status=pending|applied|rejected&page=1&page_size=25`
(page_size ≤ 100) — newest first; sales only its own or unassigned leads →
`{"items": [{…as above, "requested_by": "associate:GNA-…", "reviewed_by": null,
"client_code": "GNE-…", "customer_name": "…"}], "page", "page_size", "total"}`.

`POST /api/v1/dashboard/survey-corrections/{correction_id}/apply` — admin, engineer.
Body `{"note": "optional, ≤ 2000"}` (or none). Writes the changes (same derivations as an
edit: shadow-free area from sections, bill averages), clears both signatures when
`resign_after_edit` is on, sets `revised_after_review` if reviewed, audit
`survey_corrected` (before/after + `correction_id`), timeline `survey_corrected`, and
flags proposals (below). The customer stays verified. 200 →

```json
{"correction": {…, "status": "applied", "reviewed_by": "emp-eng-1", "reviewed_at": "…", "review_note": "…"},
 "flagged_proposal_ids": [31]}
```

`POST /api/v1/dashboard/survey-corrections/{correction_id}/reject` — admin, engineer.
Body `{"note": "required, ≤ 2000"}` → 200 `{"correction": {…, "status": "rejected"}}`.
Nothing on the survey changes.

Errors: 404 `correction_not_found`; 409 `correction_not_pending` (already decided — two
reviewers can never both act: the row is locked and re-checked); 403
`segregation_of_duties` (the requester deciding); 409 `not_editable` (customer closed
since; apply only); 422 `note_required` (reject); and the request's own validation codes
if the survey changed since it was asked (apply).

### Re-verification of proposals

Fields that change size, price or design: `reverification_fields` (SRV-16) —
`roof_sections`, `roof_area_sqft`, `shadow_free_area_sqft`, `sanctioned_load_kw`,
`connection_phase`, `survey_variant`, `consumption_history`, `avg_monthly_units_kwh`,
`avg_monthly_bill_inr`, `desired_output_kw`. When an applied correction or an associate's
edit changes one, every **open** proposal of the customer (draft, pending pricing/approval,
approved, or sent and not accepted) gets:

```json
"needs_reverification": true,
"reverification_reason": {"fields": ["roof_area_sqft"], "source": "correction" | "revisit" | "edit",
                          "correction_id": 4 | null, "at": "2026-10-12T05:03:00+00:00"}
```

(`revisit` = an edit while the review outcome is `revisit_needed`.) A second flag before
clearing adds its fields. Audit `reverification_flagged` on the proposal. Accepted
proposals are never flagged. Both keys are on every dashboard proposal response
(`GET /dashboard/proposals` items and `GET /dashboard/proposals/{id}`, Engineers included).

**A flagged proposal cannot reach the customer:**

| path | refusal |
|---|---|
| `POST /dashboard/proposals/{id}/send` | 409 `{"code": "needs_reverification", "message", "fields"}` |
| `POST /associate/…/proposal/in-person-acceptance` and `POST /dashboard/onboardings/{id}/proposal/in-person-acceptance` | 409 `{"code": "needs_reverification", "message", "fields"}` |
| `GET` and `POST /api/v1/proposals/accept/{token}` (public) | 409 `{"code": "needs_reverification", "message"}` — no `fields` for the customer |

`POST /api/v1/dashboard/proposals/{proposal_id}/reverification/clear` — admin,
management, engineer; sales scoping as for proposals. Body `{"note": "required, ≤ 2000"}`.
200 → `{"proposal_id", "needs_reverification": false, "reverification_reason": {…kept},
"reverification_cleared_by", "reverification_cleared_at"}`. Audit
`reverification_cleared` with the note. Errors: 404; 409 `not_flagged`; 422
`note_required`; 403 `segregation_of_duties` (the employee who applied the correction).

## 10. Survey report PDF (Step 7)

`POST /api/v1/dashboard/onboardings/{onboarding_id}/survey/report` — admin, engineer.
No body. Renders the report (WeasyPrint, data:-only fetcher, every typed value escaped)
and stores it privately at `proposal-inputs/{inputs_id}/survey_report/{uuid}.pdf`; each
call stores a new file (the old one is kept). 200 →
`{"onboarding_id": 12, "generated_at": "…", "filename": "Survey-Report-GNE-WB-RES-2026-00012.pdf"}`.
Errors: 409 `review_required` (no review yet); 503 `storage_not_configured`.

Signed link (latest report), every link audited (`link_issued`, kind `survey_report`):

- Staff `GET /api/v1/dashboard/onboardings/{onboarding_id}/survey/report` — §3 readers.
- Associate `GET /api/v1/associate/onboardings/{onboarding_id}/survey/report` — owner; a
  territory Preferred only when `territory_viewers_see_documents` is true (otherwise 403
  with BE-1's documents message).

200 → `{"url", "expires_at", "filename", "generated_at"}`. 404 `report_not_generated`;
503 `storage_not_configured`.

Contents (English): letterhead (company name, address, email, website — any still a
placeholder is left out), client code, customer, site address, coordinates with source
and confirmation time, survey type, connection, bills table and averages, roof (with
sections table, total and shadow-free area), layout and distances, sizing (suggested kW,
limiting factor) and every warning in plain English, the review (outcome, reviewer, time,
notes, conditions, revisit items), both signature images with times, and the customer's
declaration. **No prices.**

## 11. Review queue and SLA (Step 8)

`GET /api/v1/dashboard/survey-reviews?state=awaiting|overdue|reviewed|revised&page=1&page_size=25`
— §3 readers (sales: own or unassigned leads). Default `awaiting`.

In the queue: complete, signed surveys of **submitted or verified** customers.

| state | surveys |
|---|---|
| `awaiting` | no review, or changed after its review (includes `overdue` and `revised`) |
| `overdue` | awaiting for `review_target_hours` (SRV-12: 48) or more |
| `reviewed` | reviewed and unchanged since |
| `revised` | changed after the review |

```json
{"items": [{"onboarding_id": 12, "client_code": "GNE-WB-RES-2026-00012", "customer_name": "Asha Roy",
            "associate": {"associate_id": "GNA-WB-26-00001", "name": "…"}, "onboarding_status": "verified",
            "submitted_at": "…", "signed_at": "…", "waiting_since": "…", "hours_waiting": 60, "overdue": true,
            "revised_after_review": false,
            "review": {"outcome": null, "reviewed_at": null, "reviewed_by": null}}],
 "state": "overdue", "review_target_hours": 48, "page": 1, "page_size": 25, "total": 1}
```

`waiting_since` = the latest of `submitted_at`, both signatures and — for a revised survey —
the last review. `hours_waiting` (whole hours) is `null` unless awaiting. Newest wait
first.

**`GET /api/v1/dashboard/work-queues`** adds, for **admin and engineer** only (absent for
other roles): `"survey_reviews": {"awaiting": n, "overdue": m}` (overdue ⊆ awaiting) and
`"survey_corrections": {"pending": k}`.

**`POST /api/v1/dashboard/internal/check-survey-reviews`** — admin JWT or `X-Internal-Key`
(Cloud Scheduler). Emails staff (`STAFF_NOTIFY_EMAILS`, event `survey_review_overdue`) one
list of the surveys that became overdue since the last run → `{"overdue": n,
"newly_alerted": k}`. A wait is reported once: each survey is claimed by a conditional
update of `proposal_inputs.survey_review_alerted_for`, so overlapping runs never repeat
it; a survey that re-enters the queue is reported again.
