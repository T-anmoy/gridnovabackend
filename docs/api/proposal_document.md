# BE-13 — the proposal document: two fixed layouts, structured data, staff editing

Written from the code: `app/proposals/document.py` (model, `build`, `figures`,
`missing_required`, `apply_edit`), `app/proposals/document_service.py`,
`app/proposals/document_render.py`, `app/proposals/reference.py`,
`app/proposals/document_config.py` + `config/proposal_document.yaml`,
`app/api/routes/proposal_document.py`, the changed `adjust-pricing`, `approve`,
`manager-sign-off`, proposal detail and public accept routes, migration
`0055_proposal_document_reference`. When code and this file disagree, the code wins.
Conventions (auth, 401/403, error shape) are in [README.md](README.md).

## What it is

Every proposal drafted since BE-13 carries a **structured document** in
`Proposal.content.document`: everything that varies on the customer PDF. The PDF is one
of **two fixed layouts** taken from Grid Nova's sample proposals:

| Layout | When | Pages (sample data) |
|---|---|---|
| `compact` | plant capacity **≤ 10 kWp** | 3 |
| `detailed` | capacity **> 10 kWp** | 8 |

The layout is chosen automatically from `plant.capacity_kwp` (`layout_rule.compact_max_kwp`
in config) and is never a staff choice. Changing the capacity across 10 kWp switches it.

The frame is constant on every proposal: header band and faint watermark on every page,
the page-footer line `GRID NOVA ENERGY | <N> kWp Grid-Tied Rooftop Solar Proposal | Page x of y`
on every page **except the last**, and on the last page the QR code (www.gridnova.in) and
the contact / offices letterhead band. The signatory is **RAJU PAUL, Authorised Signatory**
on every proposal (config `signatory`; nobody can change it through the API).

All draft paths build it the same way (`ProposalAgent.draft_proposal` →
`document_service.attach`): staff draft (`POST /dashboard/proposals/draft`), the Proposal
Agent, the associate formal proposal (`POST /dashboard/onboardings/{id}/formal-proposal`)
and the site-assessment approve-and-draft. A **revision** (request-revision, then a new
draft) is built from what the system holds now, with every section staff edited on the
previous version carried over (`edited`) and a price staff set kept; a price-list price is
re-quoted.

A proposal drafted before BE-13 has no document (`has_document: false`); it keeps its
stored PDF and the old generic template, and cannot be edited here.

## Document fields

Decimals travel as **strings**. "Source" is where `build` takes the value from; "Edit" is
the section that changes it (see the role map below); "—" means computed or fixed.

| Field | Type | Source | Edit |
|---|---|---|---|
| `schema_version` | `1` | — | — |
| `revision` | int | starts at 0, +1 per saved edit or price change | — (optimistic concurrency) |
| `segment` | `residential` / `commercial` / `industrial` / `government` / `agricultural` / null | lead `segment`, else the site assessment's site type; a fallback document takes it from `connection.category` | via `connection.category` |
| `coverage` | null / `government` / `utility` / `agricultural` / `fallback` | null = a segment the templates cover | via `connection.category` (fallback only) |
| `housing_society` | bool | survey variant `housing_society` (no residential subsidy) | — |
| `mount_type` | `rooftop` / `ground_mounted` | lead / site type | — |
| `addressee.customer_type` | `individual` / `organisation` | site assessment `customer_type` | addressee |
| `addressee.title` | null / `Mr.` `Ms.` `Mrs.` `Dr.` `M/s` | organisation → `M/s`; individuals none (never guessed from a name) | addressee |
| `addressee.name` | string ≤ 120 | customer name / organisation name | addressee |
| `addressee.attention` | string ≤ 120 | organisation: the contact person | addressee |
| `addressee.address_lines` | 0–5 strings ≤ 120 | site assessment full address + district, state – PIN | addressee |
| `addressee.mobile` | 10 digits | customer phone / onboarding mobile | addressee |
| `addressee.aadhaar_last4` | 4 digits / null | **not held by the system** — staff may enter the last 4 only; printed `XXXX XXXX 1234`; null → row omitted | addressee |
| `addressee.salutation` | one of config `salutations` | config `default_salutation` (`Dear Sir / Madam,`) | addressee |
| `connection.discom` | string ≤ 60 | site assessment / survey | connection |
| `connection.consumer_number` | string ≤ 40 | site assessment | connection |
| `connection.sub_division` | string ≤ 80 | **not held** | connection |
| `connection.sanctioned_load_kw` | decimal | site assessment | connection |
| `connection.category` | `Residential` `Commercial` `Industrial` `Government` `Agricultural` | segment | connection |
| `connection.phase` | `single` / `three` | site assessment `connection_phase` | connection |
| `site.address_lines` | 0–5 strings | site assessment | site |
| `site.town` | string ≤ 80 | site assessment district (staff refine to the town, e.g. "Habra") | site |
| `site.state` | string ≤ 60 | site assessment state — printed as Destination "Within <state>" | site |
| `pm_surya_ghar.subsidy_opted` | bool | scheme `pm_surya_ghar` | pm_surya_ghar |
| `pm_surya_ghar.portal_registered` | bool | **not held** (default false) | pm_surya_ghar |
| `pm_surya_ghar.approved_capacity_kwp` | decimal | **not held** | pm_surya_ghar |
| `plant.capacity_kwp` | decimal > 0, ≤ 3 dp | price-list size / desired output / lead capacity | plant |
| `plant.package` | `NOVA_ASSURED` / `NOVA_SIGNATURE` / null | onboarding equipment package | plant |
| `plant.dcr` | bool | site assessment `dcr_panels_required` | plant |
| `system.module_technology` | `Mono PERC` / `TOPCon` (price list) | `Mono PERC` | system |
| `system.inverter` | string ≤ 160 / null | null = layout default ("3 kW single-phase grid-tied inverter") | system |
| `system.mounting` | string ≤ 160 / null | null = "Rooftop module mounting structure, designed for the roof type" | system |
| `boq[]` | ≤ 30 rows `{description, quantity, standard?, make, kind?}` | layout defaults (config) | boq |
| `customer_scope[]` | 1–20 strings ≤ 400 | layout defaults (config) | customer_scope |
| `payment_terms[]` | 1–6 `{percent, label, short_label?}`, total 100 | compact: price list 20/60/20; detailed: 25/60/15 | payment_terms |
| `notes[]` | 0–10 strings ≤ 400 | empty | notes |
| `price` | `{mode, basic, rate_per_kwp, total, price_list_version}` | price list when list-priceable | **adjust-pricing only** |
| `defaults` | internal | what each defaulted section was generated from | — |
| `edited` | list of section names | sections staff changed: never regenerated, carried into a revision | — |

Text staff type may not contain `[PLACEHOLDER`, `[UNCONFIRMED` or `DUMMY`, and may use
only these `{tokens}`, filled when the PDF is rendered so they follow the plant:
`{capacity_kwp}`, `{module_technology}`, `{dcr_label}`, `{phase_cap}`, `{discom}`,
`{space_required_sqft}`, `{validity_days}`.

**Defaults follow the data; staff edits stick.** `boq`, `customer_scope` and
`payment_terms` are generated from config for the layout (and, for the compact BOQ, the
package, ≤ 5 kW and PM Surya Ghar). When what they depend on changes (e.g. a capacity edit
switches the layout) a section staff have **not** edited is regenerated; one they edited
is never overwritten. The edit response lists `regenerated_sections`.

`price.mode`: `price_list` (basic from `config/price_list.yaml`, re-quoted when capacity,
package or phase change; a size the list does not price leaves no price), `staff_basic`
(adjust-pricing `basic_price`, or `rate_per_kwp` × capacity), `staff_total` (adjust-pricing
`new_value`, or a configured per-kW tier: the total incl. GST is kept exactly, basic =
total / 1.089), `none`.

## Computed figures (never typed)

`document.figures(doc)` is the one calculation; the PDF, the dashboard and the accept page
all read it.

| Field | Rule |
|---|---|
| `layout` | ≤ 10 kWp compact, else detailed |
| `basic`, `gst`, `total` | GST = basic × 8.9% (5% on 70% + 18% on 30%), ROUND_HALF_UP to the paisa; total = basic + GST |
| `words.basic/gst/total` | Indian system, "Rupees Two Lakh One Thousand Four Hundred Sixty-Five Only" (crore, lakh, thousand; "and N Paise") |
| `payments[]` | `{percent, label, short_label, amount}`: amount = total × percent, rounded; the last milestone takes the remainder so they add up to the total exactly |
| `whole_rupees` | true when every amount is whole → the PDF prints `2,01,465`; otherwise all amounts with paise |
| `subsidy` | `{applies, slabs[{kw, rate, amount}], total, effective_cost}` — PM Surya Ghar CFA (app/pricing/subsidy.py): residential, not a housing society, subsidy opted, DCR modules; 30,000/kW first 2 kW + 18,000 3rd kW, cap 78,000; effective cost = total − subsidy |
| `generation` | `units_per_kwp_per_month` (120, config) × capacity per month, × 12 per year |
| `space_required_sqft` | `{low, high, single}` = capacity × 83 / 100 sq ft (config, [UNCONFIRMED] PRO-17); compact prints low–high rounded to 10, detailed prints `single` (low rounded to 100) |
| `validity_days` | `sales_policy.yaml proposal_validity_days` (15) |

Amounts print as the samples do: `Rs. 2,01,465` (Indian grouping).

## Reference and date

Allocated **when a version is approved** (standard approval or manager sign-off), in the
approval's own transaction, and never changed afterwards:

- `GN/DD/MM/YYYY/NN` — the IST day of approval and a per-day sequence (2 digits, more when
  needed), from `proposal_reference_sequences` with one atomic INSERT … ON CONFLICT …
  RETURNING; the unique index `uq_proposals_document_reference` is the last wall. Example
  `GN/23/10/2026/01`. (The samples' `GN/23/10/2026` repeats when two proposals go out on
  one day.)
- A later approved version on the same lead: the first issued reference + `-R1`, `-R2`, …
  (`GN/23/10/2026/01-R1`).
- `document_date` = the IST day of approval; printed as `23.10.2026`.

Before approval the preview prints "Allocated on approval".

## Who may change what

Server-enforced; the section → roles map is `edit_permissions` in
`config/proposal_document.yaml` (Grid Nova can change it without code; only `admin`,
`sales`, `management`, `engineer` may appear).

| Section | Roles (default) |
|---|---|
| `addressee`, `connection`, `site`, `pm_surya_ghar`, `plant`, `customer_scope`, `payment_terms`, `notes` | Admin, Sales |
| `system`, `boq` | Admin, Sales, Engineer |
| price | Admin, Sales — only through `PATCH /proposals/{id}/adjust-pricing` |
| header, footer, watermark, letterhead, signatory, the company letter paragraphs, quality policy, legal / subsidy notes, warranty rows, GST rule, every computed figure | **nobody** through the API (config only) |

Management reads, approves through manager sign-off and — as today — cannot request a
revision (`request-revision` is Admin / Sales in the code). Sales is limited to its own or
unassigned leads; Operations and Accounts never see proposals.

Editing is possible only while the proposal is `draft`, `pending_pricing_validation` or
`pending_approval`. An approved or sent version is locked; a change after approval is
`request-revision` → a new draft (new version, the document carried forward, a new
`-R` reference on approval) → approval again.

## `GET /api/v1/dashboard/proposals/{id}/document`

Admin, Management, Sales (own / unassigned leads), Engineer (technical part). 404 plain.

```json
{
  "proposal_id": 12, "has_document": true, "version": 1, "status": "draft",
  "revision": 3, "layout": "compact", "locked": false,
  "editable_sections": ["addressee", "connection", "site", "pm_surya_ghar", "plant", "system",
                        "boq", "customer_scope", "payment_terms", "notes"],
  "document": { "...": "the fields above" },
  "figures": { "layout": "compact", "capacity_kwp": "3", "price_mode": "price_list",
               "basic": "185000.00", "gst": "16465.00", "total": "201465.00", "gst_rate": "0.089",
               "words": {"basic": "Rupees One Lakh Eighty-Five Thousand Only", "gst": "…", "total": "…"},
               "payments": [{"percent": "20", "label": "On Order", "short_label": null, "amount": "40293.00"}],
               "whole_rupees": true,
               "subsidy": {"applies": true, "slabs": [{"kw": "2", "rate": "30000.00", "amount": "60000.00"},
                                                      {"kw": "1", "rate": "18000.00", "amount": "18000.00"}],
                           "total": "78000.00", "effective_cost": "123465.00"},
               "generation": {"units_per_kwp_per_month": 120, "monthly_units": 360, "annual_units": 4320},
               "space_required_sqft": {"low": 250, "high": 300, "single": 200}, "validity_days": 15 },
  "missing_required": [{"field": "connection.discom", "message": "the DISCOM"}],
  "document_reference": null, "document_date": null,
  "signatory": {"name": "RAJU PAUL", "title": "Authorised Signatory", "for_line": "For Grid Nova Energy"}
}
```

`locked: true` → `editable_sections: []`. A proposal without a document:
`{"proposal_id": 7, "has_document": false, "editable_sections": []}` (other keys null).

**Engineer**: `document` is `{"view": "technical", "revision", "plant": {"capacity_kwp", "dcr"}, "system", "boq"}`;
`figures`, `missing_required`, reference and signatory are null — no customer, contact or
price. `editable_sections`: `["system", "boq"]`.

The same object is in `GET /dashboard/proposals/{id}` as `document`.

## `PATCH /api/v1/dashboard/proposals/{id}/document`

Admin, Sales, Engineer (a role with no editable section: 403 `field_not_editable`). Replaces
**one section**:

```json
{ "base_revision": 3, "section": "boq",
  "value": [{"description": "Solar PV Modules – {module_technology}, {dcr_label}", "quantity": "{capacity_kwp} kWp",
             "standard": null, "make": "Sova Solar / HR Solar / V-Sole / Equivalent", "kind": "module"}] }
```

`value` is the section's object (`addressee`, `connection`, `site`, `pm_surya_ghar`,
`plant`, `system` — send the whole object; omitted fields take their defaults) or list
(`boq`, `customer_scope`, `payment_terms`, `notes`).

200: the `GET` object plus

```json
{ "changes": [{"field": "boq[0].make", "before": "…", "after": "…"}],
  "regenerated_sections": [], "total_value": 201465.0, "requires_manager_signoff": false }
```

For an Engineer `total_value` and `requires_manager_signoff` are null (no pricing), like
the rest of the Engineer view.

Every change is audited (`audit_logs` action `document_edited`: section, before/after per
field, revision, who, when). A change that moves the price (plant, phase, PM Surya Ghar)
updates `total_value` and **re-evaluates the approval tier** with the same rule as at draft
time (value threshold, capacity); a flip to manager sign-off is audited
(`document_repriced`) and emailed like adjust-pricing.

Errors (`{"detail": {"code", "message", "field"?}}`):

| Status | code | When |
|---|---|---|
| 403 | `field_not_editable` | the role may not change this section, or the section is fixed / unknown (`header`, `signatory`, `price`, `layout`, …) |
| 409 | `proposal_locked` | approved, sent, accepted or revision_requested |
| 409 | `stale_version` | `base_revision` ≠ current `revision`; also `current_revision` |
| 409 | `no_document` | a proposal drafted before BE-13 |
| 422 | `payment_terms_total` | payment terms do not total 100 % (or are empty) |
| 422 | `make_not_in_catalogue` | a BOQ make (each `/`-separated name) is not in the equipment catalogue, the price list's makes, the default rows, or `Equivalent` / `Branded` / `Liaison with DISCOM` / `–`; `field: "boq[i].make"` |
| 422 | `invalid_value` | any other validation failure (`field` = the section; the message names the sub-field) |
| 403 / 404 | plain | not your lead / not found |

## `GET /api/v1/dashboard/proposals/{id}/preview.pdf`

Admin, Management, Sales (own / unassigned leads). Engineers 403 (it carries pricing and
contact). Renders the **current** document now — never stored — `application/pdf`,
`Content-Disposition: inline`, `Cache-Control: no-store`. Until the proposal is approved
it carries a diagonal **"DRAFT — NOT FOR CUSTOMER"** stamp on every page and the reference
reads "Allocated on approval"; after approval it is the same as the customer PDF.

Rate-limited: 20 renders per employee per minute (per instance) → 429 `rate_limited`.
409 `no_document`; 409 `missing_required` when there is no price yet; 409
`render_failed` if the document could not be rendered.

## Approval and sending

- `POST /proposals/{id}/approve` and `/manager-sign-off`: **409 `missing_required`** while
  anything required is missing:
  `{"code": "missing_required", "message": "This proposal cannot be approved or sent yet; it still needs: …", "missing": [{"field", "message"}]}`.
  Required: `addressee.name`, `addressee.address_lines`, `plant.capacity_kwp`,
  `connection.phase`, `connection.category`, `price`, `boq`, `customer_scope`,
  `payment_terms`, `site.town`, `site.state`; compact also `connection.discom`,
  `connection.consumer_number`; detailed also `site.address_lines`;
  `pm_surya_ghar.approved_capacity_kwp` when `portal_registered`; `plant.mount_type`
  (rooftop); `segment_text` while an uncovered segment's text (government, utility,
  PM-KUSUM, generic fallback) is still a placeholder in `config/proposal_content.yaml`.
  On success the reference and date are allocated and the final PDF is rendered from the
  document and stored as before.
- `POST /proposals/{id}/send` and in-person acceptance: the same 409 `missing_required`
  (with `missing`); otherwise the earlier `placeholder_content` / `needs_reverification`
  rules.
- No customer PDF can contain a `{token}`, a placeholder marker or "None": the renderer
  refuses.

## `PATCH /api/v1/dashboard/proposals/{id}/adjust-pricing` (changed)

Exactly **one** of:

| Field | Meaning |
|---|---|
| `new_value` | the total **including GST** (as before) |
| `basic_price` | before GST — GST 8.9 % and the total are computed (needs a document) |
| `rate_per_kwp` | × the document's capacity = the basic price (needs a document; 409 `missing_required` without a capacity) |

plus `reason`. 422 when none or several are given; 409 `no_document` for `basic_price` /
`rate_per_kwp` on an older proposal. `total_value` becomes the computed total; the
response adds `figures` (null without a document). Same roles and statuses as before.

## Accept page

`GET /api/v1/proposals/accept/{token}` adds `document` (null for an older proposal):
`{layout, capacity_kwp, reference, date, basic, gst, total, words, payments, subsidy,
generation, customer_scope}` — the figures the PDF prints.

## Segments the samples do not cover

Utility-scale, government, PM-KUSUM and unmatched (generic) proposals use the Detailed
layout, keep the "manual review required" flag (`used_fallback_template` for the generic
case) and cannot be approved while their scope text (PRO-7, PRO-8, PRO-10, PRO-11, PRO-12)
is still a placeholder. A rooftop lead the system could not classify becomes covered when
staff set `connection.category` to Residential, Commercial or Industrial.
