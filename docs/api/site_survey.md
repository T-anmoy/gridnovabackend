# S1 — On-site site survey (Steps 1–4)

Uncommitted, migration `0044_site_survey`. Code: `app/survey/` (`config.py`, `validation.py`,
`status.py`, `results.py`, `signatures.py`), routes `app/api/routes/associate_survey.py`
(associate) and `app/api/routes/survey_admin.py` (staff). Config: `config/site_survey.yaml`
(registry SRV-1…SRV-14, all `[UNCONFIRMED]`).

Grid Nova's paper "Checklist to Assess Potential Solar Rooftop System" as a guided survey.
It **extends the site assessment** (`ProposalInputs`) — there is no separate survey record.
The client code is the existing `project_reference_id` (`GNE-…`). Steps 5–8 (engineer
review, change control, report PDF) are not built yet.

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
  `review.reviewed_by`.

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
            "revised_after_review": false}}
```

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
 "assumptions": {"area_sqft_per_kw": 90, "load_cap_ratio": 1.0, "target_offset_percent": 90.0},
 "warnings": [],
 "estimate": {"indicative": true, "size_kw": 3, "price_status": "list_price", "custom_quote_reasons": [],
              "priced_kw": 3, "phase": "SINGLE",
              "packages": {"NOVA_ASSURED": {"basic": "…", "gst": "…", "final": "…", "display_name": "Nova Assured",
                                            "net_after_subsidy": "…"},
                           "NOVA_SIGNATURE": {…}},
              "subsidy_inr": "78000.00", "subsidy_conditions": ["dcr_modules_required", "scheme_eligibility"],
              "price_list_version": 1}}
```

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
Draft or submitted only — signing a submitted customer returns it to draft; verified or
closed → 409 `not_editable`. Errors: 422 `declaration_name_required`,
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
| `GET /associate/onboardings/{id}/survey` | 401 | owner, territory Preferred (read) |
| `GET /associate/onboardings/{id}/survey/results` | 401 | owner, territory Preferred (read) |
| `POST /associate/onboardings/{id}/survey/signatures/{party}` | 401 | owner only |
