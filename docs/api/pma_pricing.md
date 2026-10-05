# PM-A — Grid Nova's price list

Commit `32b02f6`; no migration. Code: `app/pricing/price_list.py` (load, validate, quote),
`app/pricing/list_pricing.py` (applicability, size snapping), `app/pricing/policy.py`,
`app/proposals/price_list_content.py`. Source data: `config/price_list.yaml` (verbatim
from Grid Nova's PDF, sha256 recorded); developer defaults: `config/pricing_policy.yaml`
(all `[UNCONFIRMED]`, registered PRC-17…22, A2).

**Customer-facing prices come only from the price list.** Packages `NOVA_ASSURED`
(equipment_tier `standard`) and `NOVA_SIGNATURE` (`premium`). Single phase 1–10 kW, three
phase 3–10 kW, whole kW, destination West Bengal. Signature basic = Assured basic × 1.12;
GST = basic × 0.089 (5% on 70% + 18% on 30%); final = basic + GST; ROUND_HALF_UP to the
paisa. Nothing interpolates or extrapolates.

All price-list amounts are **decimal strings**.

## 1. Public calculator — `POST /api/v1/pricing/instant-estimate`

Request (unchanged fields plus two): `segment`, `state`, `sizing_method`
(`by_units`|`by_bill`|`by_roof_area`), `monthly_units_kwh?`, `monthly_bill_inr?`,
`usable_roof_area_sqft?`, `connection_phase?` (`single_phase`|`three_phase`),
`roof_type?`, `floors?`, `shading?`, `access_difficulty?`, `is_remote`,
`is_housing_society`, **`connection_type?`** (default `on_grid`), **`battery_storage`**
(default false). Unknown segment → 422. Rate limit 20/min per IP → 429.

Response:
```
{recommended_size_kw,
 sizing_detail: {consumption_based_kw, area_based_kw, phase_capped},   # phase_capped always false now
 base_cost, adders, component_breakdown, subtotal_before_tax, gst_amount,
 subsidy_inr, net_cost, is_indicative_range, net_cost_range_min, net_cost_range_max, note,
 price_status,                     # "list_price" | "custom_quote"
 custom_quote_reasons[],           # outside_west_bengal | segment_not_in_price_list | battery_or_off_grid
                                   # | phase_unknown | size_above_max | three_phase_below_min | size_below_min
 recommended_kw_exact,             # the calculated size, 2 dp
 priced_kw,                        # next whole kW within the phase range; null for a custom quote
 priced_kw_differs,
 phase,                            # "SINGLE" | "THREE" | null
 packages: {NOVA_ASSURED: {basic, gst, final, net_after_subsidy, display_name},
            NOVA_SIGNATURE: {...}},                  # {} for a custom quote
 gst_rule: {effective_rate: "0.089", components: [{share_of_basic, rate}], description},
 price_list_version, destination,
 subsidy_conditions}               # ["dcr_modules_required","scheme_eligibility"] when a subsidy applies, else []
```

Backward compatibility: `base_cost` and `subtotal_before_tax` = Nova Assured basic,
`gst_amount` = Assured GST, `net_cost` = Assured final − subsidy (JSON numbers); all four
are **null for a custom quote**. `adders` and `component_breakdown` are `{}`;
`is_indicative_range` is always false and the ranges null (list prices are fixed: no
shading/roof/floor/access/state modifiers). Significant shading keeps the list price and
sets `note` (site visit). The PM Surya Ghar subsidy is computed on `priced_kw` (or the
recommended size for a custom quote) and shown separately.

Applicability (policy defaults): West Bengal; segment `residential` (housing society
allowed); grid-connected without battery; the connection phase must be known
(`phase_unknown` otherwise — the frontend must ask for it); size within the phase range.
Three-phase needs that round up to 1–2 kW → `three_phase_below_min` (never priced).

## 2. `POST /api/v1/pricing/estimate` (public)

New optional inputs: `connection_phase`, `connection_type`, `battery_storage`,
`is_housing_society`. Size = `size_kw`, or the size the monthly bill implies, rounded up
to a whole kW. Response: `{indicative_estimate: true, total_inr (Nova Assured final as a
number, or null), categories: null, price_status, custom_quote_reasons,
recommended_kw_exact, priced_kw, priced_kw_differs, phase, packages: {P: {basic, gst,
final, display_name}}, gst_rule, price_list_version, destination}`.
`POST /api/v1/pricing/breakdown` (admin, sales) still returns the internal placeholder
engine breakdown — never shown to customers.

## 3. `GET /api/v1/pricing/packages` (public, new)

```
{price_list_version, destination, currency,
 packages: [{package, display_name, equipment_tier,
             equipment: {solar_panel: {makes[], or_equivalent, technology[]},
                         inverter: {makes[], or_equivalent},
                         cables?: {makes[], or_equivalent}}}],      # cables: Nova Signature only
 phase_ranges: {SINGLE: {min_kw, max_kw}, THREE: {min_kw, max_kw}},
 gst_rule,
 terms: {payment_schedule: [{number, percent, label}], standard_warranty,
         solar_panel_life_span, o_and_m, price_change_clause},
 price_display,                    # "exact" | "hidden" (policy public_price_display)
 prices?: [{package, phase, kw, basic, gst, final}]}               # 36 rows, only when "exact"
```
No model numbers and no DCR status (not supplied by Grid Nova).

## 4. Associate indicative estimates

`POST /api/v1/associate/onboardings/{id}/estimates` uses the same engine. A requested
size (`desired_output_kw`) is never rounded: when the configuration is otherwise
list-priceable it must be a whole kW in the phase range, else **422 `unsupported_size`**
with `reason` (`non_integer_size` | `size_above_max` | `size_below_min` |
`three_phase_below_min`). Otherwise outside the list → custom quote.

The stored `result` adds every field of section 1 plus `chosen_packages`
(`["NOVA_ASSURED"]` for the Nova Assured package, `["NOVA_SIGNATURE"]` for Nova Signature,
both for "as recommended" or none). List/GET items add top-level `price_status`,
`price_list_version`, `priced_kw`, `chosen_packages` — `null` on estimates made before
PM-A (their stored `result` is returned unchanged).

## 5. Formal proposal content

Staff `POST /api/v1/dashboard/onboardings/{id}/formal-proposal` (and every draft) prices
from the list when the lead's site assessment is list-priceable **and** names one
package **and** the requested size is a whole kW in range.

`content.pricing` adds:
`{price_status, package, package_name, phase, kw, basic, gst, final, gst_rule,
price_list_version, source_sha256, destination, custom_quote_reasons}` (list price), or
`{price_status: "custom_quote", custom_quote_reasons, package, kw, phase}`. Extra reasons
here: `package_not_chosen`, `size_unknown`, `non_integer_size`.
`content.price_list = {version, source_sha256}`. `total_value` = the GST-inclusive final
(a number). After adjust-pricing: `pricing.adjusted = true`, `list_price_final`,
`variance` (= new − list, string).

Sections (ids, in `content.sections`): `pricing` (list body — package, kW, phase, basic,
GST rule, final in "Rs 1,23,456.78"; or `[PLACEHOLDER: Price to be confirmed by Grid
Nova.]`), `equipment_makes` (package known), `electrical_specification` (only ≤ 5 kW; omitted
above), `payment_terms`, `warranty_and_service`, `price_terms` (list-priced only:
destination + price-change clause). The send-guard still blocks on unsupplied wording
(PRO-1…12) and on a missing price.

`PATCH /api/v1/dashboard/proposals/{proposal_id}/adjust-pricing` (admin, sales) body
`{new_value ≥ 0, reason}` → adds `list_price_final`, `variance` (null when the proposal
was not list-priced).

Generated documents (proposal PDF, agreement, receipts) print amounts as
`Rs 1,23,456.78`; the website keeps ₹.

## 6. Payment schedule

Template (labels and percentages from Grid Nova): `On Order` 20 · `Against Material
Bill` 60 · `Before Commissioning` 20. Triggers (registry P9, unconfirmed): `accepted`;
`material_dispatched`; `inspection_net_meter` where the project has it, else `installed`
(resolved per project when the schedule is created; stored as a concrete stage).
Optional gate `execution.gates.commissioned` (off): when on, recording `commissioned`
returns the stage blocker `commissioning_payment_gate` until milestone 3 is verified. The
dispatch gate stays off (it would contradict "against material bill").

## 7. Serial register additions (staff)

`POST /api/v1/dashboard/onboardings/{id}/execution/catalogue` (admin, operations), 201:
body `{category: module|inverter|structure|bos_component, brand ≤100, model_name ≤100
(as printed), specification? ≤2000, is_dcr_compliant (from the OEM declaration),
equivalent_reason? ≤2000}` → `{equipment: {id, category, brand, model_name,
specification, is_dcr_compliant, is_active}, package, approved_makes, in_package}`.
A brand outside the project's package (or an unknown package) needs `equivalent_reason`
(≥ 5): 422 `make_not_approved` + `approved_makes`, `package`. Components the package
lists no makes for are not checked (`in_package: null`).

Staff-only, non-blocking flag `make_not_in_package` on units and in register `flags`.

## 8. Nova (website assistant)

Nova may describe the packages, makes ("or equivalent"), panel technology, the 5-year
standard warranty from handover, first-year free O&M and the 20–25 year panel life span.
It never states model numbers (`equipment_model_number`), DCR compliance
(`unverified_dcr_claim`), makes outside the list, other warranty terms, or package
prices (it points to the calculator or the associate).

## 9. Registry

Resolved: ONB-3, PRC-8, P1 (percentages/labels). Superseded (list-priced paths): PRC-1…7,
PRC-12, PRC-13, PRO-13, PRO-14. Partially resolved: D1. New: PRC-17…22, P9, P10, D2…D5,
A2 (PRC-16 = effective date). Registry items carry `status` / `status_note`.
