# BE-14 — Solar estimate engine v2

One calculation of size, generation, subsidy, savings and payback for every caller:
the public calculator (`POST /api/v1/pricing/instant-estimate`), the associate's
indicative estimate, the survey results, `/pricing/estimate` and `/breakdown`, and the
proposal document. Code: `app/estimate/` (`config.py`, `tariff.py`, `subsidy.py`,
`engine.py`, `assumptions.py`). Config: `config/solar_estimate.yaml`,
`config/subsidies.yaml` (validated at startup). Prices still come only from Grid Nova's
price list (`pma_pricing.md`).

All new money, energy and percentage fields are **decimal strings** (`"3240.00"`,
`"360"`, `"100.0"`). Pre-BE-14 fields keep their names, types and meanings (§6).

## 1. Status of every figure

Every value in the two config files has `{value, unit, status, source, as_of}`:

| status | meaning | effect |
|---|---|---|
| `confirmed` | Grid Nova or an official order confirmed it | used; a confirmed subsidy is deducted from `net_cost` |
| `announced` | a government announced it; the order has not been seen | used for information; an announced subsidy is **never deducted** — `announced_state_support`, `net_cost_if_announced_support_received` |
| `unconfirmed` | a developer default, or "not available" (`value: null`) | used as a working default and labelled; never deducted |

Every `announced` / `unconfirmed` value has an `[UNCONFIRMED]` marker and an entry in
`config/placeholder_registry.yaml` (`EST-1…12`, `SUB-1…5`), listed at
`GET /api/v1/dashboard/placeholders`. `estimate_version` (`"1.1+<sha256 of both files, 12 hex>"`)
identifies the exact assumptions behind a saved estimate.

## 2. The rules, exactly as implemented

All arithmetic is `Decimal`, `ROUND_HALF_UP`, rounded only where shown.

**Generation.** `per_kwp` = `generation.units_per_kwp_per_month` for the state:
West Bengal 120 and Bihar 120 (`confirmed`, Grid Nova: 4 units/kWp/day); any other state
the fallback 120 (`unconfirmed`, warning `generation_unconfirmed_for_state`).
`monthly_units = kW × per_kwp` (to the whole kWh), `annual_units = monthly × 12`
(3 kW → 360 / 4,320). System losses are inside the 120; no derate. Shading is not a
loss: `shading: "significant"` sets `note` (site visit needed).

**Consumption** (`consumption_basis`, most accurate first):

| basis | inputs | units used | ₹ per unit for savings |
|---|---|---|---|
| `monthly_units_and_bill` | units + bill | units | the customer's own: `bill ÷ (units − free units)`, to the paisa, when inside `tariffs.implied_rate_sanity_inr_per_kwh` (₹3–20); outside → warning `implied_tariff_outside_range` and the next row applies |
| `monthly_units` | units | units | the DISCOM table when `confirmed`, else Grid Nova's flat ₹9 |
| `monthly_bill_tariff_table` | bill + a DISCOM with a confirmed table | the units whose table bill equals the bill (solved exactly) | the table |
| `monthly_bill_flat_rate` | bill | `free units + bill ÷ ₹9` | ₹9 |
| `null` | roof area only | — | — (no savings) |

**Size.**
`consumption_kw = monthly_units × target_offset% ÷ per_kwp` (= annual units × offset ÷
annual generation per kWp); `area_kw = usable_roof_area_sqft ÷ sqft_per_kwp` (100);
`exact_kw = min(...)` to 0.01 kW (`recommended_kw_exact`). `roof_limited: true` when the
roof is the smaller; `sizing_basis` is then `roof_area`, else the consumption basis (or
`requested_size`). Priced size: the next whole kW in the phase range (unchanged). When the
whole kW below is ≠ the priced size and ≥ the phase minimum, it is also returned in
`size_options` (role `floor`).
`coverage_percent = monthly_generation ÷ monthly_units × 100` (0.1).

**Sanctioned load.** `sanctioned_load_kw` given and `exact_kw` or `priced_kw` above it →
warning `above_sanctioned_load` with the numbers; the size is never capped.

**Bill model** (`app/estimate/tariff.py`). Table: `bill(u) = fixed + energy(u) + duty`;
`energy(u) = E(u) − E(min(u, F))`, E = slab charges on cumulative `up_to_units`, F = free
units; duty `percent_of_energy` (or `…_and_fixed`); fixed `flat_per_month` or
`per_kw_per_month` × sanctioned load. Rate models (implied, flat): `bill(u) = rate ×
max(u − F, 0)`. Bills to the paisa.

**Free units.** `tariffs.free_units.bihar` (125 a month, `announced`) applies to NBPDCL /
SBPDCL, or to Bihar when the DISCOM is not one in config: the first 125 units cost nothing,
so solar saves only on the units above them.

**Savings (marginal, never average).**
`monthly = bill(units) − bill(max(units − monthly_generation, 0))` (fixed charges cancel)
+ export credit (only a `confirmed` `flat_inr_per_kwh` credit on the surplus; every state
is `rule: none` today). `annual = monthly × 12`.

**Subsidy** (`app/estimate/subsidy.py`, `config/subsidies.yaml`, MNRE PM Surya Ghar
guidelines 7 Jun 2024):

| | general | special-category |
|---|---|---|
| first 2 kWp | ₹30,000/kWp | ₹33,000/kWp |
| 2–3 kWp | ₹18,000/kWp | ₹19,800/kWp |
| above 3 kWp | nothing | nothing |
| maximum | ₹78,000 | ₹85,800 |

Pro-rata (1.5 kW → 45,000; 2.5 kW → 69,000). Special-category: Arunachal Pradesh, Assam,
Manipur, Meghalaya, Mizoram, Nagaland, Sikkim, Tripura, Uttarakhand, Himachal Pradesh,
Jammu & Kashmir, Ladakh, Andaman & Nicobar Islands, Lakshadweep (matched ignoring case,
"&"/"and", punctuation). Capacity: `cfa_capacity_basis` `nominal` (default, SUB-1) or
`module_dc` = ceil(nominal W ÷ `module_wattage_wp`) × module Wp (without a wattage →
nominal, note `module_wattage_unknown_nominal_used`). Eligible: segment `residential`,
connection `on_grid` or `hybrid` (battery allowed); otherwise `not_eligible` with
`segment_not_eligible` / `off_grid_not_eligible`.
Housing society (`is_housing_society`): `rate × min(installed kWp, 3 × number_of_houses,
500)` — ₹18,000 (₹19,800 special); without `number_of_houses` → `status:
needs_number_of_houses` (never ₹0).
State top-ups (residential individual only, inside `valid_from…valid_until`, nominal kW):
Bihar `per_kw_with_cap` ₹10,000/kW, cap ₹20,000, until 2027-08-13 (`announced`);
West Bengal `capacity_steps` ≥1 kW ₹5,000, ≥2 kW ₹10,000, ≥3 kW ₹15,000 (`announced`;
SC/ST support is text only — caste is never asked).

**Money.** `net_cost = Assured final − confirmed_total`;
`net_cost_if_announced_support_received = net_cost − announced_total`;
`payback_years = max(net_cost, 0) ÷ annual saving` (0.1), `null` when the saving ≤ 0 or
there is no price.

**Long term** (`savings.long_term_years` = 25, price list life span). Year factor: 1
(year 1), `1 − first_year% − (n − 2) × annual%` after (2.0 / 0.55, `unconfirmed`, EST-2):
98.00 % in year 2, 85.35 % in year 25. Year generation = annual × factor (whole kWh); year
saving = 12 × saving(year generation ÷ 12) × (1 + escalation%)^(n−1) (escalation 0 %,
EST-11). O&M: first year free; later years not modelled and not deducted.

**Confidence.** `high` = units given and a confirmed DISCOM table; `medium` = the
customer's own ₹/unit, or a bill solved through a confirmed table; `low` = the flat ₹9,
or no consumption. A generation figure not confirmed for the state lowers `high` to
`medium`.

## 3. `POST /api/v1/pricing/instant-estimate` (public)

New optional inputs (all others unchanged; see `pma_pricing.md`):

| field | type / bounds | effect |
|---|---|---|
| `monthly_units_kwh` | 1–100,000 (was > 0) | may now be sent **with** `monthly_bill_inr` |
| `monthly_bill_inr` | 1–50,00,000 (was > 0) | with units: the customer's own ₹/unit |
| `discom` | ≤ 60 chars (`WBSEDCL`, `CESC`, `NBPDCL`, `SBPDCL`, other text) | its tariff table when confirmed; unknown → warning `unknown_discom` |
| `tariff_category` | ≤ 60 chars (`domestic`, `domestic_rural`, `domestic_urban`) | picks the table when the DISCOM has several |
| `sanctioned_load_kw` | > 0, ≤ 100,000 | `above_sanctioned_load` warning |
| `number_of_houses` | 1–5,000 | housing society CFA |
| `module_wattage_wp` | 100–1,000 | `module_dc` CFA basis |

Rate limit unchanged (20/min/IP). New response fields:

```
estimate_version            "1.1+ebb9f500bf76"
sizing_basis                monthly_units_and_bill | monthly_units | monthly_bill_tariff_table
                            | monthly_bill_flat_rate | roof_area | requested_size
consumption_basis           as §2, or null (roof area only)
monthly_units_used          "410" | null
roof_limited                bool
figures_for_kw              the size the figures below are for: priced_kw, else the exact size
size_options[]              [{kw, role: priced|floor, coverage_percent, generation{monthly_units, annual_units},
                              subsidy_confirmed_total, announced_state_support_total,
                              packages{P: {basic, gst, final, net_after_subsidy}}, net_cost,
                              monthly_saving, annual_saving, payback_years}]   # [] for a custom quote
generation                  {units_per_kwp_per_month, monthly_units, annual_units, status, source}
coverage_percent            "117.1" | null
subsidy                     {status: eligible|needs_number_of_houses|not_eligible, reason,
                             central: {category: general|special, basis: nominal|module_dc, capacity_kwp,
                                       slabs: [{from_kwp, to_kwp, kwp, rate_per_kwp, amount}], total, max_inr} | null,
                             society: {category, rate_per_kwp, number_of_houses, installed_kwp, eligible_kwp,
                                       limited_by: installed|houses|max_kwp, kwp_per_house, max_kwp, total, basis} | null,
                             state: [{state, amount, status, note, source, as_of, valid_until, rule}],
                             confirmed_total, announced_total, conditions[], notes[]}
subsidy_status              = subsidy.status
announced_state_support     subsidy.state items with status announced
net_cost_if_announced_support_received   "163904.50" | null
savings                     {monthly, annual, method: "marginal", tariff_source: discom_tariff|customer_bill|flat_rate,
                             confidence, monthly_bill_before, monthly_bill_after, rate_inr_per_kwh,
                             tariff_table, free_units_per_month} | null (no consumption)
payback_years               "4.1" | null
long_term                   {years, total_generation, total_savings | null,
                             by_year: [{year, output_percent, generation_units, savings}], assumptions{…}}
assumptions[]               every value used: {name, value, unit, status, source, as_of}
confidence                  high | medium | low
warnings[]                  codes, in order
warning_details             {code: params}
```

Each package also gains `net_if_announced_support_received`.

Warning codes: `above_sanctioned_load` {sanctioned_load_kw, exact_kw, priced_kw} ·
`priced_size_exceeds_roof_area` {priced_kw, max_by_area_kw, sqft_per_kwp} ·
`generation_unconfirmed_for_state` {state, units_per_kwp_per_month} ·
`implied_tariff_outside_range` {implied_tariff_inr_per_kwh, min, max} ·
`implied_rate_unavailable_within_free_units` {monthly_units_kwh, free_units} ·
`unknown_discom` {discom} · `tariff_table_not_available` {discom, category, status} ·
`tariff_category_needed` {discom, categories} ·
`bill_not_solvable_with_tariff_table` / `bill_not_solvable` {reason:
bill_below_fixed_charges | bill_within_free_units | fixed_charge_needs_sanctioned_load}.

### Worked examples (computed by the engine; tests/test_solar_estimate.py)

**A. West Bengal, 360 units, single phase.** exact = 360 × 12 × 0.9 ÷ 1,440 = 2.70 →
priced 3 kW, floor 2 kW. 3 kW: 360 units (100.0 %); 2 kW: 240 (66.7 %). Price 2,01,465 −
78,000 = `net_cost` 1,23,465 (WB announced ₹15,000 → `net_cost_if_announced…` 1,08,465).
Savings at ₹9 flat: 360 × 9 = 3,240.00 a month, 38,880.00 a year, `confidence: low`;
payback 1,23,465 ÷ 38,880 = 3.2 years. 25 years: 99,369 units, ₹8,94,321.00.

**B. Bihar, 410 units, ₹3,700 bill, SBPDCL, sanctioned load 3 kW.** exact = 410 × 0.9 ÷
120 = 3.075 → 3.08 → 4 kW (117.1 %), floor 3 kW (87.8 %); warning `above_sanctioned_load`.
Own rate = 3,700 ÷ (410 − 125 free) = 12.98. 4 kW: bill before 285 × 12.98 = 3,699.30,
after 0 → saving 3,699.30 a month (the 3 kW option saves the same: its 360 units cover all
285 chargeable units). Price 4 kW 2,61,904.50 − 78,000 = 1,83,904.50; Bihar announced
₹20,000 → 1,63,904.50 if received. Payback 1,83,904.50 ÷ 44,391.60 = 4.1 years
(3 kW: 1,23,465 ÷ 44,391.60 = 2.8). `confidence: medium`.

**C. Housing society, 100 kWp, 20 houses.** min(100, 60, 500) = 60 kWp × 18,000 =
₹10,80,000 (`subsidy.society.limited_by: houses`). Without `number_of_houses`:
`subsidy_status: needs_number_of_houses`, `subsidy_inr: null`, nothing deducted.

## 4. `GET /api/v1/pricing/estimate-assumptions` (public)

The engine's current assumptions and subsidy rules, for the "How we calculate" panel —
the same config the calculator applies, nothing secret. `Cache-Control: public,
max-age=300`; built once per process; rate limit 20/min/IP (429).

```
{estimate_version,
 generation: {units_per_kwp_per_month: [{state, name, value, unit, status, source, as_of}, …, {state: "other", …}],
              annual_is_monthly_times: 12, system_losses, shading, degradation: [value, value]},
 roof_area: [sqft_per_kwp, sqft_per_kwp_low],
 sizing: {target_offset, formula, sanctioned_load},
 savings: {method, escalation, long_term_years, export_credit: [{state, rule, …}], o_and_m, payback},
 tariffs: {flat_rate, implied_rate_sanity: {min, max, unit, status, source, as_of},
           discoms: [{state, discom, category, status, source, as_of, effective_from, available,
                      slabs | null, fixed_charge | null, electricity_duty | null}],
           free_units: [{state, discoms[], name, value, unit, status, source, as_of}]},
 confidence_rules: {high, medium, low},
 subsidies: {central: {scheme, status, source, as_of, residential: {general|special: {slabs: [{up_to_kwp,
             rate_per_kwp}], max_inr}}, housing_society: {rate_per_kwp: {general, special}, kwp_per_house,
             max_kwp}, special_category_states[], eligible_segments[], eligible_connection_types[],
             cfa_capacity_basis, conditions[]},
             state_top_ups: [{state, amount_rule, applies_to, note, status, source, as_of, valid_from,
                              valid_until}],
             deduction_rule}}
```

Slabs are returned only for a confirmed (`available: true`) table.

## 5. The other callers

- **`POST /pricing/estimate`** (public) and **`/breakdown`** (staff): a bill is sized by the
  engine (Bihar adds the free units back). `/estimate` gains `sizing_basis` and
  `estimate_version`. `monthly_bill_inr` bounds are now 1–50,00,000 (was ≥ 0).
- **Associate estimate** (`POST /associate/onboardings/{id}/estimates`): the instant
  estimate on the onboarding's data, now with its DISCOM, sanctioned load and (housing
  society) `society_flat_count` as `number_of_houses`; units and bill together use the
  customer's own ₹/unit. The stored `result` carries every new field, including
  `estimate_version`.
- **Survey results** (`…/survey/results`): `max_by_area_kw` uses 100 sq ft/kWp (was 90) and
  `assumptions.area_sqft_per_kw` is 100; the bill-per-unit warning reads the engine's range.
  `estimate` gains `estimate_version`, `subsidy`, `subsidy_status`,
  `announced_state_support`, `net_cost`, `net_cost_if_announced_support_received`,
  `generation`, `coverage_percent`, `savings`, `payback_years`, `long_term`, `confidence`;
  `subsidy_inr` is null for a society without `society_flat_count`, and is the society CFA
  with it.
- **Proposal document** (`figures`): generation per kWp and roof area per kWp come from the
  engine; the subsidy slabs use the site state's rates (special-category: 33,000 /
  19,800). `subsidy.state_lines[{state, amount}]` — a **confirmed** state top-up, printed
  as one more line, only when `config/subsidies.yaml proposal.print_confirmed_state_top_up`
  is true (default false, SUB-4); announced amounts are never printed.
- **Nova**: company facts state the central, special-category and society rules from
  `config/subsidies.yaml`, name the states with announced support without amounts, say it
  is not guaranteed, and point to the calculator.

## 6. Backward compatibility (`instant-estimate`)

Every pre-BE-14 key is present with its meaning: `recommended_size_kw` (raw size to the
nearest 0.5 kW), `sizing_detail`, `base_cost`, `adders`, `component_breakdown`,
`subtotal_before_tax`, `gst_amount`, `subsidy_inr`, `net_cost`, `is_indicative_range`,
`net_cost_range_min/max`, `note`, `price_status`, `custom_quote_reasons`,
`recommended_kw_exact`, `priced_kw`, `priced_kw_differs`, `phase`, `packages`, `gst_rule`,
`price_list_version`, `destination`, `subsidy_conditions`. Changes in meaning, all
deliberate:

- `subsidy_inr` / `net_cost` / `net_after_subsidy`: the **confirmed** subsidy — now
  including a housing society's CFA (was always 0), and `null` (not 0) for a society
  without `number_of_houses`; special-category states get their higher rates; off-grid
  gets none.
- `subsidy_conditions`: the full condition list (it was the first two).
- For a custom quote the subsidy is on the exact size (was the 0.5 kW-rounded size).
- Bihar: a bill-only estimate counts the 125 free units (units = 125 + bill ÷ 9).
- Bounds: units 1–100,000 and bill 1–50,00,000 (were > 0 and up to 10 crore).
