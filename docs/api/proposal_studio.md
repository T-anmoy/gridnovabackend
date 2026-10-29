# Proposal Studio — API contract (BE-18)

A separate area of the dashboard where the **Proposal Desk** enters a customer's data in a
guided, validated form (or an Excel sheet), sets the price and gets Grid Nova's branded PDF
in seconds; then issues, tracks, revises and closes the proposal. It is separate from the
lead → Nova → proposal flow, which is unchanged.

* Every figure is computed by `app/proposals/document.figures` (the BE-13 engine) — never
  typed twice, never produced by an AI model. The PDF is the BE-13 frame (header band,
  watermark, footer line, QR, letterhead, signatory RAJU PAUL), config only.
* **No AI call** anywhere in drafting, previewing or issuing. The one optional assistant
  (`suggest-from-notes`, off by default) only suggests customer / site / connection fields.
* Base path: `/api/v1/dashboard`. Amounts are **decimal strings** (`"201465.00"`); capacity
  is a string (`"3"`, `"52"`, `"7.5"`). Errors are `{"detail": {"code", "message", "field"?, …}}`.

## 1. Who

| Permission | Proposal Desk | Admin | Management |
|---|---|---|---|
| `studio.view` — open the Studio (own proposals) | ✓ | ✓ | ✓ |
| `studio.view_all` | ✓ | ✓ | ✓ |
| `studio.edit` — customers, documents, issue, deliver, revise, duplicate, accepted/lost | ✓ | ✓ | — |
| `studio.price` — price and discount (sensitive) | ✓ | ✓ | — |
| `studio.approve` — approve / reject (sensitive; never the preparer) | — | ✓ | ✓ |
| `studio.library.manage` | ✓ | ✓ | — |
| `studio.convert` — create a lead | ✓ | ✓ | — |

Every other role gets **403** on every Studio route. `proposal_desk` may be combined with
any role (e.g. Sales) and needs no two-step sign-in. Since BE-21 no role does (Clerk free
plan); `403 two_factor_required` returns only if a role's `requires_two_factor` is turned back
on (access_control.md §7). `GET /employees/me` lists the
permissions — show the Studio nav item when `studio.view` is held.

## 2. Project types and layouts

`GET /studio/options` returns everything the wizard's drop-downs need.

| `project_type` | Layout | Prints | Issue blocked until Grid Nova supplies |
|---|---|---|---|
| `residential_rooftop` | compact ≤ 10 kWp, detailed > 10 | PM Surya Ghar CFA when opted + DCR | — |
| `housing_society` | detailed | GHS/RWA CFA: ₹18,000/kW (special category ₹19,800) on min(installed, 3 kW × houses, 500 kW); needs `project.houses` | — |
| `commercial_rooftop`, `industrial_rooftop` | compact ≤ 10, detailed > 10 | optional savings page | — |
| `ground_mounted` | detailed | "Ground-Mounted" in the plant's own sentences | PRO-7 |
| `government_institutional` | detailed | | PRO-8, PRO-11 |
| `pm_kusum` | detailed | | PRO-10 |
| `street_lighting` | **itemised**: line items qty × rate, GST per line | | STU-6, STU-7, STU-8 (GST rates, warranty) |
| `generic` | detailed | | PRO-12 |

A blocked type can be drafted and previewed; `submit` / `approve` / `issue` answer
**409 `content_not_ready`** with `registry: ["PRO-7"]` and `blockers: [{registry, message}]`.

## 3. Statuses

`draft` → (`pending_approval`) → `ready` → `issued` → `viewed` → `accepted` | `declined` |
`expired`; plus `superseded` (a newer version issued), `converted` (lead created), `lost`.
The proposal's status follows its current version (`converted` / `lost` stay).

**Approval** is needed when the offered basic price is below the price master, or the total
incl. GST is above `PROPOSAL_VALUE_MANAGER_APPROVAL_THRESHOLD` (₹5,00,000). Otherwise a
version with no proof-check errors and every warning acknowledged may be issued directly.
The approver holds `studio.approve` and did **not** prepare the version (anyone who created,
edited, priced or submitted it). An approval is bound to the document revision: any edit after
submission or approval sends the version back to `draft`.

## 4. Customers

* `GET /studio/customers?q=&limit=` — search name, mobile (≥ 4 digits) or GSTIN.
* `POST /studio/customers` `{customer_type: individual|organisation, title?, name, attention?, mobile, email?, address_lines[1–5], gstin?}` → 201 `{customer, duplicates: [{id, name, mobile, gstin, matched_on: ["mobile"|"gstin"]}]}`. A duplicate is a **warning**, never a refusal. 422 `invalid_value` (`field`: mobile 10 digits, GSTIN format + check digit, …).
* `GET /studio/customers/{id}`, `PATCH /studio/customers/{id}` (partial) → `{customer, duplicates}`. Changing a customer does not change a proposal's printed addressee (edit the `addressee` section).

## 5. Proposals

* `GET /studio/proposals?status=&project_type=&owner_id=&customer_id=&date_from=&date_to=&q=&page=&page_size=` → `{items: [ProposalRow], total, page, page_size}`. `q` searches customer name, mobile, reference, code, title. `customer_id` (BE-19): one Studio customer's proposals. Without `studio.view_all`: own only. Newest change first.
* `POST /studio/proposals` `{customer_id, project_type, template_id?}` → 201 **View** (version 1, draft, defaults generated: BOQ, customer scope, payment terms — ≤ 10 kW the price list's 20/60/20, above 25/60/15 — validity 15 days).
* `GET /studio/proposals/{id}?version=` → **View**.

**ProposalRow** (each list item; also `View.proposal`) — written out in full (BE-19):
```json
{"id": 7, "code": "SP-000007", "title": "3 kWp Residential rooftop — Test Kumar Das",
 "project_type": "residential_rooftop", "status": "draft", "current_version": 1,
 "owner_id": "user_…", "owner_name": "Desk Person",
 "capacity_kwp": "3",
 "customer": {"id": 3, "name": "…", "mobile": "98300…"}, "lead_id": null, "lost_reason": null,
 "total": "201465.00", "document_reference": null, "valid_until": null, "issued_at": null,
 "created_at": "2026-10-27T10:00:00+00:00", "updated_at": "2026-10-27T10:05:00+00:00"}
```
`total` and `capacity_kwp` are the current version's (decimal strings or null). `owner_name` is a
display name only — never an email — for anyone with `studio.view` (null when the owner has no
employee row).

**View**
```json
{"proposal": { … ProposalRow … },
 "version": {"version": 1, "status": "draft", "locked": false, "document_reference": null, "document_date": null,
             "valid_until": null, "total": "201465.00", "verification_code": null, "pdf_sha256": null,
             "pdf_filename": null, "submitted_by": null, "approved_by": null, "approval_comment": null,
             "issued_by": null, "issued_at": null, "first_viewed_at": null, "view_count": 0, "accepted_at": null,
             "accepted_via": null, "accepted_name": null, "declined_at": null, "decline_reason": null,
             "supersedes_version": null, "change_summary": [], "spell_ignore": []},
 "revision": 5, "layout": "compact",
 "document": { … schema_version 2 … },
 "figures": { … see §7 … },
 "missing_required": [{"field": "site.town", "message": "the town named in the letter"}],
 "content_blockers": [],
 "checks": [{"id": "capacity_above_sanctioned", "level": "warning", "field": "plant.capacity_kwp",
             "message": "3 kWp is above the sanctioned load of 2 kW.", "fingerprint": "9f0c…"}],
 "errors": 0, "warnings_unacknowledged": ["9f0c…"],
 "approval_required": false, "approval_reasons": [],
 "signatory": {"name": "RAJU PAUL", "title": "Authorised Signatory", "for_line": "For Grid Nova Energy"},
 "versions": [{"version": 1, "status": "draft", "document_reference": null}],
 "public_url": null, "verify_url": null,
 "can": {"edit": true, "price": true, "submit": false, "approve": false, "issue": false, "deliver": false,
         "revise": false, "mark_accepted": false, "mark_lost": true, "convert": false}}
```
`public_url` / `verify_url` (BE-19): set whenever the shown version is issued — the customer's
page (`FRONTEND_BASE_URL/proposal/{token}`) and the verification page the QR opens; null before
issue (and `public_url` null if the stored link cannot be read, e.g. a changed encryption key).

**The schema-2 document** (`View.document`, also what a converted lead's proposal carries in
`content.document`) — the whole object; every section is replaced as a unit by the guided edit:
```json
{"schema_version": 2, "revision": 0, "segment": "residential", "coverage": null, "housing_society": false,
 "mount_type": "rooftop",
 "addressee": {"customer_type": "individual", "title": "Mr.", "name": "Test Kumar Das", "attention": null,
               "address_lines": ["12 Example Lane, Habra", "North 24 Parganas, West Bengal – 743263"],
               "mobile": "9830000001", "aadhaar_last4": "1234", "salutation": "Dear Sir,"},
 "connection": {"discom": "WBSEDCL", "consumer_number": "100000001", "sub_division": "Habra CCC",
                "sanctioned_load_kw": "3.52", "category": "Residential", "phase": "single"},
 "site": {"address_lines": ["…"], "town": "Habra", "state": "West Bengal"},
 "pm_surya_ghar": {"subsidy_opted": true, "portal_registered": true, "approved_capacity_kwp": "3"},
 "plant": {"capacity_kwp": "3", "package": "NOVA_ASSURED", "dcr": true},
 "system": {"module_technology": "Mono PERC", "inverter": null, "mounting": null},
 "boq": [{"description": "Solar PV Modules – {module_technology}, {dcr_label}", "quantity": "{capacity_kwp} kWp",
          "standard": null, "make": "Sova Solar / HR Solar / V-Sole / Equivalent", "kind": "module"}, …],
 "customer_scope": ["Provide shadow-free roof area, …", …],
 "payment_terms": [{"percent": "20", "label": "On Order", "short_label": null}, …],
 "notes": [],
 "price": {"mode": "price_list", "basic": "185000.00", "rate_per_kwp": null, "total": null,
           "price_list_version": 1, "list_basic": "185000.00", "discount": null, "reason": null},
 "defaults": {"boq": "v2|compact|pmsg=True|up5=True|package=NOVA_ASSURED", "customer_scope": "…",
              "payment_terms": "v2|compact|le10=True"},
 "edited": [],
 "project": {"project_type": "residential_rooftop", "houses": null, "roof_area_sqft": null},
 "savings": null,
 "items": [],
 "display": {"show_discount_line": false, "show_savings": false, "show_package_comparison": false},
 "notes_free": [], "cover_highlight": null, "validity_days": 15}
```
* Money and sizes are decimal strings. `boq[].kind`: `module` | `inverter` | `cable` | null; `{…}`
  placeholders in BOQ text are filled when printed. `price.mode`: `price_list` | `staff_basic` |
  `staff_total` | `none`; `price.discount` `{amount, reason}`. `savings` (inputs) `{monthly_units,
  monthly_bill, tariff_per_unit, fixed_charges, export_rate}`. `items[]` (itemised layout)
  `{description, specification, make, quantity, unit, unit_rate, category}`.
* `edited`: the sections staff own (never regenerated); `defaults`: the key each defaulted section
  (`boq`, `customer_scope`, `payment_terms`) was generated from.

### `GET /studio/options` (studio.view) — the whole object
```json
{"project_types": [{"id": "residential_rooftop", "label": "Residential rooftop", "layout": "compact_or_detailed"}, …],
 "packages": [{"id": "NOVA_ASSURED", "label": "Nova Assured"}, {"id": "NOVA_SIGNATURE", "label": "Nova Signature"}],
 "phases": ["single", "three"], "categories": ["Residential", …], "discoms": ["WBSEDCL", …],
 "titles": ["Mr.", "Ms.", …], "salutations": ["Dear Sir,", …], "module_technologies": ["Mono PERC", "TOPCon"],
 "item_units": ["Nos", "Sets", …],
 "item_categories": [{"id": "solar_street_light", "label": "Solar street light", "gst_rate": "0.05"}, …],
 "sections": ["addressee", "connection", "site", "pm_surya_ghar", "plant", "system", "boq", "customer_scope",
              "payment_terms", "notes", "project", "items", "display", "savings", "notes_free", "cover_highlight",
              "validity_days", "price"],
 "validity_days_default": 15, "price_modes": ["price_list", "staff_basic", "staff_total", "none"],
 "assistant_enabled": false,
 "payment_term_presets": [
   {"id": "up_to_10_kw", "label": "Up to 10 kW — price list (20 / 60 / 20)",
    "terms": [{"percent": "20", "label": "On Order"}, {"percent": "60", "label": "Against Material Bill"},
              {"percent": "20", "label": "Before Commissioning"}]},
   {"id": "above_10_kw", "label": "Above 10 kW (25 / 60 / 15)",
    "terms": [{"percent": "25", "label": "Advance along with the order"}, …]}],
 "resettable_sections": ["boq", "customer_scope", "payment_terms", "system"],
 "boq_makes": {"module": ["Adani", "Equivalent", "HR Solar", …], "inverter": ["Deye", …],
               "cable": ["Finolex", "Havells", …], "conduit": ["Branded", "Supreme"], "other": [ … every allowed make … ]},
 "email_message_max": 1500}
```
* `payment_term_presets` (BE-19, `config/studio.yaml`): each preset names its source, so its
  figures always match it — the price list's schedule, and `config/proposal_document.yaml`
  `detailed.payment_terms`. Only presets Grid Nova supplied; apply one with the ordinary
  `payment_terms` edit.
* `boq_makes` (BE-19): per BOQ row `kind` — the price list's makes for that component (both
  packages, plus "Equivalent" where the price list allows one), the default rows' makes, and the
  active catalogue brands (modules, inverters). `conduit` is the price list's conduit/flex pipe
  makes. `other` = every make the BOQ accepts (`make_not_in_catalogue` refuses anything else),
  for rows with no kind.

## 6. The guided edit — `PATCH /studio/proposals/{id}/document`

`{base_revision, section, value}` replaces **one section** of the current draft and returns
the View plus `changes: [{field, before, after}]`, `regenerated_sections`, `approval_cleared`.
Wizard order: customer → site & connection → plant & system → BOQ → price → terms & notes → review.

| section | value |
|---|---|
| `addressee` | `{customer_type, title, name, attention, address_lines, mobile, aadhaar_last4, salutation}` |
| `site` | `{address_lines, town, state}` |
| `connection` | `{discom, consumer_number, sub_division, sanctioned_load_kw, category, phase: single\|three}` |
| `plant` | `{capacity_kwp, package: NOVA_ASSURED\|NOVA_SIGNATURE\|null, dcr}` |
| `system` | `{module_technology: "Mono PERC"\|"TOPCon", inverter, mounting}` |
| `pm_surya_ghar` | `{subsidy_opted, portal_registered, approved_capacity_kwp}` |
| `project` | `{project_type?, houses?, roof_area_sqft?}` |
| `boq` | `[{description, quantity, standard, make, kind}]` — makes from the catalogue / price list (`make_not_in_catalogue`) |
| `items` (itemised) | `[{description, specification, make, quantity, unit, unit_rate, category}]` |
| `customer_scope`, `notes`, `notes_free` | `[string]` (`notes_free` prints under "Special Notes") |
| `payment_terms` | `[{percent, label, short_label}]` — must total 100 |
| `display` | `{show_discount_line, show_savings, show_package_comparison}` |
| `savings` | `{monthly_units?, monthly_bill?, tariff_per_unit, fixed_charges?, export_rate?}` or `null` |
| `cover_highlight` | string ≤ 90 or null — one-line subtitle under the title |
| `validity_days` | 1–90 |
| `price` (**studio.price**) | `{mode, basic?, rate_per_kwp?, total?, discount?: {amount, reason}, reason?}` |

**Price modes**: `price_list` (the price master's basic for the package, phase and capacity;
409 `not_list_priceable` otherwise), `staff_basic` (`basic` or `rate_per_kwp`), `staff_total`
(total incl. GST; no discount), `none`. A **discount** comes off the basic price before GST.
`list_basic` (the price master's basic, when list-priceable) is computed. An offered basic
below the price master needs `reason` (or a discount with its reason): **422
`reason_required`** with `below_price_master: {list_basic, basic, percent}`. Every price change
is an event (`priced`) with totals before and after.

Errors: 403 `not_permitted` / `two_factor_required`, 409 `proposal_locked` / `stale_version`
(`current_revision`), 422 `invalid_value` / `payment_terms_total` / `make_not_in_catalogue`.

### `POST /studio/proposals/{id}/sections/{section}/reset` (studio.edit) — BE-19
Body `{base_revision}`. `section`: `boq` | `customer_scope` | `payment_terms` | `system`. Puts the
section back to its generated default (BOQ, scope and terms from the layout, size and package;
`system` to `{module_technology: "Mono PERC", inverter: null, mounting: null}`) and stops staff
owning it (it follows the plant again). Same rules as the guided edit: 409 `proposal_locked` /
`stale_version` (`current_revision`), 422 `field_not_resettable`; a submitted/approved version
goes back to draft; recorded as an `edited` event with `detail.reset: true`. Returns the View
plus `changes`, `regenerated_sections`, `approval_cleared`.

## 7. Figures (schema 2)

The BE-13 figures (`layout, capacity_kwp, price_mode, basic, gst, total, gst_rate, words{basic,
gst,total}, payments[{percent,label,short_label,amount}], whole_rupees, subsidy, generation,
space_required_sqft, validity_days`) plus:

* `schema_version: 2`, `project_type`, `list_basic`, `below_price_master: {list_basic, basic, percent}|null`
* `discount: {price_before_discount, amount, reason}|null`
* `subsidy` for a housing society: `{applies, kind: "housing_society", slabs: [{kw, rate, amount}], total,
  effective_cost, houses, eligible_kw, limited_by: installed|houses|max_kwp, kw_per_house, max_kw}`
* `savings` (when inputs present): `{monthly_consumption_units, tariff_per_unit, year1_generation_units,
  year1_savings, year1_monthly_savings, total_savings_25_years, payback_years, effective_cost,
  table: [{year, generation_units, self_consumed_units, exported_units, savings, cumulative}] (years 1, 5, 10, 15, 20, 25),
  assumptions: {degradation_first_year, degradation_annual_after, tariff_escalation, export_rate, module_technology},
  note, depreciation_line}` — printed only with `display.show_savings`.
* `package_comparison` (compact, list-priceable): `[{package, name, panels, inverters, basic, gst, total, chosen}]`
* itemised: `items: [{no, amount, gst_rate, gst, gst_percent}]`, `gst_groups: [{rate, percent, taxable, gst}]`.

## 8. The proof-check

Runs on every save (and again on the exact PDF that is issued). `checks[]` items are
`{id, level, field, message, fingerprint}` (+ `word` for spelling).

**Errors** (block submit, approve, issue): `missing_required`, `content_not_ready`,
`total_mismatch`, `milestones_mismatch`, `payment_percent`, `words_mismatch` (the amount in
words is parsed back), `subsidy_ineligible`, `subsidy_above_cap`, `mobile_invalid`,
`gstin_invalid`, `validity_past`, `make_not_in_catalogue`, and from the rendered PDF:
`page_count`, `leftover_text` (`{token}`, `[PLACEHOLDER`, `[UNCONFIRMED`, `DUMMY`, `None`,
`nan`, `undefined`), `footer_missing`, `footer_on_last_page`, `letterhead_missing`,
`qr_missing`, `letterhead_misplaced`, `capacity_mismatch` (every "n kWp" in the text),
`render_failed`, `render_unavailable`.

**Warnings** (acknowledge each): `capacity_above_sanctioned`, `single_phase_large` (> 7 kW),
`below_price_master` (with the %), `price_per_kwp_band` (only once Grid Nova supplies bands,
STU-5), `roof_area_short`, `state_outside_service` (not West Bengal / Bihar), `spelling`
(per word; staff-typed text only).

* `POST /studio/proposals/{id}/acknowledge-warnings` `{fingerprints: [...]}` → View; recorded as
  event `warnings_acknowledged`. 409 `unknown_warning` when one no longer applies.
* `POST /studio/proposals/{id}/spelling/ignore` `{word}` → View.

## 9. Approval and issue

* `POST …/submit` → View + `result: "pending_approval"|"ready"`. 409 `proof_check_failed`
  (`checks`), `warnings_unacknowledged` (`warnings`), `content_not_ready`, `invalid_status`.
  Approvers are alerted (`studio_approval_requested`).
* `POST …/approve` `{comment?}` / `POST …/reject` `{comment}` (studio.approve) → View. 403 `maker_checker`.
* `POST …/issue` → View + **`public_url`** (the customer's link). Allocates the reference from
  the counter lead proposals use (`GN/DD/MM/YYYY/NN`; a revision `…-R1`, `-R2`), the date and
  `valid_until`, renders **PDF/A-3b** with metadata (title, author "Grid Nova Energy", subject,
  keywords), re-runs the proof-check on the bytes, stores it with its SHA-256, locks the
  version, supersedes earlier issued versions, records `change_summary` against the previous
  issued version. File name `GridNova_Proposal_GN-27-10-2026-01_3kWp_TestKumarDas.pdf`.
  409 `approval_required` / `proof_check_failed` / …; 503 `storage_unavailable`.
* The last-page QR of a Studio proposal opens `https://www.gridnova.in/verify/p/<verification_code>`
  (12 characters, non-guessable). Lead proposals keep the website QR.
* `GET …/preview.pdf?version=` — rendered now, never stored, stamped "DRAFT — NOT FOR CUSTOMER"
  until issued; 20 a minute per employee (429 `rate_limited`). 409 `missing_required` without a price.
* `GET …/pdf?version=` — the issued file (302 to a signed link).

## 10. Delivery, tracking, closing

* `POST …/email` `{to?, subject?, message?}` → `{sent, to}` — PDF attached, online link, from
  `NOTIFY_FROM_EMAIL`, reply-to the owner. BE-19: `message` is staff's own plain text (≤ 1,500
  characters; one paragraph per line; HTML-escaped) placed after the greeting, **above** the fixed
  lines and the link; `subject` (≤ 200) replaces the configured subject. Both are recorded on the
  `emailed` event. 422 `no_customer_email` / `invalid_value` (message too long or not plain text);
  503 `email_not_configured`; 502 `send_failed`.
* `GET …/pdf-link?version=` (studio.view) — BE-19 → `{url, expires_at, filename}`: a short-lived
  signed link to the issued PDF, as JSON (no bucket CORS needed); recorded as a staff
  `downloaded` event. 409 `not_issued`; 503 `storage_unavailable`. `GET …/pdf` (302 redirect)
  stays.
* `POST …/whatsapp-link` → `{url: "https://wa.me/91XXXXXXXXXX?text=…", text, link}` (no API, no cost).
* `POST …/revise` → View of the next version (draft, everything carried over).
* `POST …/duplicate` `{version?, customer_id?}` → 201 View of a new proposal.
* `POST …/mark-accepted` (multipart: `accepted_on` date, `signed_copy` PDF/JPEG/PNG ≤ 10 MB) →
  View + `convert_offered`. 409 `proposal_expired`.
* `POST …/mark-lost` `{reason}` → View.
* `POST …/convert` (studio.convert) → `{lead_id, proposal_id, created}` — a Lead (source
  `proposal_studio`, segment from the type, **unassigned** since BE-19 — Sales sees it through
  "assigned to you or nobody"; a `studio_converted` alert goes to the Sales role; the owner stays
  on the Studio proposal and in the `converted` event `detail.owner_id`) and a Proposal on it with the
  **same document, reference, PDF and status** (`accepted` or `sent`); idempotent. A later online
  acceptance also accepts the lead's proposal. 409 `not_issued`.
* `GET …/events` → `{items: [{id, version, type, actor_type, actor, detail, at}]}` — created, edited,
  priced, warnings_acknowledged, spelling_ignored, submitted_for_approval, ready, approved, rejected,
  issued, superseded, emailed, whatsapp_link_created, viewed, downloaded, reminder_sent, accepted,
  declined, expired, revised, duplicated, imported, converted, lost, assistant_suggested.
* `GET …/versions/{a}/diff/{b}` → `{from, to, changes: [{field, before, after}]}`.

## 11. The customer's page (public, no sign-in, noindex, rate-limited per IP)

Link: `FRONTEND_BASE_URL + "/proposal/" + token` (config/studio.yaml `render.public_page_path`).
Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`, `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`. Unknown token → 404.

* `GET /api/v1/public/studio/{token}` → `{reference, date, title, customer_name, capacity_kwp, layout,
  basic, gst, total, total_in_words, discount, payments, subsidy, valid_until, state:
  open|accepted|declined|superseded|expired, accepted_at, pdf_sha256, pdf_filename, mobile_hint:
  "XXXXXX0001", pdf_path}`. Each call is a `viewed` event (IP, user agent); the first alerts the owner.
* `GET /api/v1/public/studio/{token}/pdf` → 302 signed link; event `downloaded`.
* `POST /api/v1/public/studio/{token}/accept` `{full_name, mobile_last4, accept_terms: true}` →
  `{status: "accepted", accepted_at, already_accepted}`. Records name, time, IP, user agent and the
  accepted PDF's SHA-256; confirmation email to the customer, alert to the owner.
  422 `mobile_mismatch` / `terms_not_accepted`; 409 `proposal_expired` / `proposal_superseded` /
  `proposal_declined`.
* `POST /api/v1/public/studio/{token}/decline` `{reason?}` → `{status: "declined", already_declined}`.
* `GET /api/v1/public/proposals/verify/{code}` → `{reference, date, capacity_kwp, title,
  total_incl_gst, status: valid|superseded|expired|accepted|declined, valid_until, customer:
  "Test K.", issuer}`. 404 for an unknown or unissued code.

## 12. Excel import

* `GET /studio/import-template.xlsx` — "Read me", "Proposal" (labelled cells B4–B48, drop-downs for
  project type, phase, package, technology, category, DISCOM, yes/no; item table from row 52),
  "Example".
* `POST /studio/import` (multipart `file`, `.xlsx` ≤ 2 MB, no macros) → 201 `{created: [{proposal_id,
  code, title, duplicate_customers}]}` — one draft per filled sheet named `Proposal…`. All or
  nothing: **422** `{code: "import_failed", errors: [{sheet, cell, field, message}]}`.

## 13. Optional assistant

`POST …/suggest-from-notes` `{text}` → `{suggestions: [{section: addressee|site|connection, field,
value, status: "check"}], dropped, model, available}` — nothing saved. 409 `assistant_disabled`
while `STUDIO_AI_FILL_ENABLED=false` (default; no model call). Never returns price, BOQ, items,
terms or any figure.

## 14. Library and dashboard

* `GET /studio/library?kind=&project_type=&include_retired=`; `POST /studio/library` `{kind:
  note|customer_scope_item|boq_preset|template, name, project_type?, body}` (body: `{text}`,
  `{rows: [BOQ rows]}`, `{document}`); `PATCH /studio/library/{id}` `{name?, body?, active?}`.
* `POST …/save-as-template` `{name}` → the current document minus customer data.
* `GET /studio/summary?owner_id=` → `{drafts, awaiting_approval, issued_this_month: {count, value},
  viewed_not_accepted, accepted: {count, value}, win_rate_90_days, expiring_in_7_days,
  median_days_issued_to_accepted, owner_id}`.

## 15. The daily job

`POST /api/v1/dashboard/internal/check-studio-reminders` (INTERNAL_API_KEY or Admin; scheduler
`gridnova-check-studio-reminders`, 09:00 IST) → `{not_viewed, expiring, customer_emails, expired}`:
not opened after 3 days → owner; expiring within 3 days → owner (and the customer when
`reminders.customer_expiry_reminder` is on — off by default); past `valid_until` → `expired`.
Each reminder is sent once.
