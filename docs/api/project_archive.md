# Project Archive (BE-8)

Grid Nova's completed projects that never went through this system (the 1,200+ PM Surya Ghar
homes, the WBREDA schools, the PM-KUSUM pumps, the street lamps, the Showa India plant),
imported from a CSV and read back in the same shape as live projects. Config:
`config/archive.yaml`. Conventions: see [README.md](README.md) (structured errors
`{"detail": {"code", "message", "field"?}}`, money as decimal strings, UTC ISO times). Live
projects are never imported — they already exist; the shared view covers both.

All routes are under `/api/v1/dashboard/archive`.

| Route | Who | |
|---|---|---|
| `GET /template.csv` | Admin | the header row and one commented example row |
| `POST /imports` | Admin | multipart `file`, `dry_run` (default `true`), `batch_token` |
| `GET /imports` | Admin | batches, newest first (max 200) |
| `GET /imports/{batch_id}` | Admin | one batch with the report it was imported with |
| `POST /imports/{batch_id}/void` | Admin | `{"reason"}` (3–1000 characters) |
| `GET /projects` | every staff role | filtered, paged cards, redacted by role |
| `GET /projects/{project_id}` | every staff role | the dossier, redacted by role |
| `GET /stats` | every staff role | counts and capacity sums, same filters as the list |

Associates are refused on all of them (401).

## The template

`GET /template.csv` — UTF-8 CSV, one header row; a row whose first cell starts with `#` is a
comment and ignored; fully blank rows are ignored. Header names are matched ignoring case,
spaces and punctuation (`Scheme or Segment` = `scheme_or_segment`); order is free; an unknown
column is ignored with a warning. Columns:

`grid_nova_ref, scheme_or_segment, system_type, capacity_kw, pump_hp, street_lamp_count, state,
district, block, address_text, pincode, latitude, longitude, customer_name, customer_phone,
customer_email, discom, consumer_number, contract_value_inr, subsidy_amount_inr, module_make,
module_model, module_wattage_wp, module_count, module_dcr, inverter_make, inverter_model,
inverter_capacity_kw, inverter_count, structure_type, battery_details, other_equipment,
order_date, installation_date, commissioning_date, net_meter_date, warranty_end_modules,
warranty_end_inverter, om_status, notes, documents`

* **Required:** `scheme_or_segment`, `state`, `district`, `commissioning_date`, and at least one
  of `capacity_kw` / `pump_hp` / `street_lamp_count`. Everything else is optional.
* `scheme_or_segment`: `pm_surya_ghar_residential`, `housing_society`, `commercial`,
  `industrial`, `institutional_govt`, `wbreda_school`, `pm_kusum_pump`, `street_lighting`,
  `ground_mounted`, `other` (case, spaces and hyphens are forgiven). `system_type`: `on_grid`,
  `off_grid`, `hybrid`, `pump`, `street_light`, `other`. `module_dcr`: `yes` / `no` / `unknown`.
* **Dates:** `dd/mm/yyyy`, `dd-mm-yyyy`, `yyyy-mm-dd`, or month/year (`03/2025`, `2025-03`) —
  stored as the 1st of the month with a `date_month_only` flag. Years 1990–2100.
* **Numbers:** Indian grouping (`12,34,567.50`), a leading `₹` / `Rs.` / `INR` and a trailing
  `kW` / `kWp` / `HP` / `Wp` are accepted. Counts must be whole numbers.
* **`documents`:** either `label|https://link` lines separated by new lines, or a JSON list
  `[{"label", "url"}]`; http(s) only, at most 20. Links are stored as given and never fetched.
* **Personal data** — `customer_phone`, `customer_email`, `consumer_number` — is optional; the
  report warns when any is present.
* **Every cell is data.** A cell such as `=HYPERLINK(…)` or `@SUM(…)` is stored as that text and
  never evaluated; control characters are stripped. Anything written back out as CSV must go
  through `app/security/csv_cells.py`.
* Limits: 10 MB (`413 file_too_large`), 20,000 rows (`413 too_many_rows`), UTF-8 only (a BOM is
  fine; anything else `422 invalid_encoding`). `row` in a report is the spreadsheet row — the
  header is row 1.

## Import: `POST /imports`

Dry run (`dry_run=true`, the default) writes **nothing**. Commit (`dry_run=false`) needs
`batch_token` — the `batch_token` (the file's SHA-256) the dry run returned; the same file must be
sent again.

```json
{"batch_id": null, "dry_run": true, "filename": "projects.csv", "batch_token": "9f2c…", "sha256": "9f2c…",
 "row_count": 120, "accepted_count": 117, "rejected_count": 3, "error_count": 3, "warning_count": 9,
 "counts_by_code": {"duplicate_grid_nova_ref": 1, "missing_required": 2, "unknown_district": 9},
 "errors":   [{"row": 14, "column": "state", "code": "missing_required", "message": "state is required."}],
 "errors_truncated": false,
 "warnings": [{"row": 7, "column": "district", "code": "unknown_district", "message": "…"}],
 "warnings_truncated": false,
 "notices": [{"code": "personal_data_present", "columns": ["customer_phone"], "rows": 40,
              "message": "personal data imported — visible to Admin/Management only"}],
 "already_imported_batch_id": null}
```
Lists are capped at 500 entries each (`*_truncated`); counts always cover every row. On commit
`batch_id` is set, rows with errors are skipped and listed, and each accepted row keeps its
warnings in `data_quality_flags`. The write is one transaction. Every commit and every void
writes the audit log (`archive_import` / `imported`, `voided`).

**Error codes** (row rejected): `missing_required`, `missing_size`, `invalid_choice`,
`invalid_number`, `out_of_range`, `invalid_date`, `too_long`, `invalid_documents`,
`reserved_ref` (`GNE-…` or `ARC-<number>` are reserved for live projects and archive ids),
`duplicate_grid_nova_ref` (in the file — the first row wins — or already in the archive),
`too_many_cells`. **File-level errors** (nothing is imported): `missing_column`,
`duplicate_column`.

**Warning codes** (row accepted): `unknown_state`, `unknown_district`, `unknown_block` (not in the
territory master; the typed text is kept), `missing_capacity`, `capacity_not_a_number`,
`duplicate_consumer_number` (in the file or the archive), `coordinates_outside_india`,
`incomplete_coordinates`, `future_date` (order / installation / commissioning / net-meter dates),
`date_month_only`, `date_order`, `invalid_pincode`, `invalid_email`, `unknown_column`.
`state`/`district`/`block` are matched to the territory master by name, ISO code or LGD code;
the original text is always kept, the matched ids are stored beside it.

**Other responses:** `422 batch_token_required`, `409 batch_token_mismatch`,
`409 already_imported` (the same file is already an imported batch — void it first),
`422 nothing_to_import`, `409 import_conflict` (a concurrent import took a reference).

## Void

`POST /imports/{id}/void` `{"reason": "…"}` → the batch with `status: "voided"`. Every project of
the batch disappears from every read path (list, detail 404, statistics, search, the shared
view); nothing is deleted. Its references and consumer numbers are free again, and the same
file can be imported again. `409 already_voided` the second time.

## Reading

`GET /projects` — newest commissioning first. Filters (all optional, applied before paging):
`q` (Grid Nova ref, and — only for fields the caller's role may see — customer name, address,
block, district, state), `state`, `district` (case-insensitive; match the typed text or the
territory master name), `scheme`, `capacity_min`, `capacity_max`, `commissioned_from`,
`commissioned_to` (`YYYY-MM-DD`, inclusive), `module_make`, `inverter_make` (contains,
case-insensitive), `page` (≥1), `page_size` (1–100, default 25). Result:
`{"items": [card], "page", "page_size", "total"}`. Bad input is a structured `422`:
`unknown_scheme`, `invalid_number`, `invalid_date`, `invalid_range` (each with `field`).

`GET /stats` (same filters) → `{"total": {"count", "capacity_kw"}, "by_scheme": [{"scheme", "count",
"capacity_kw"}], "by_state": [{"state", …}], "by_district": [{"state", "district", …}],
"by_year": [{"year", …}]}`, computed in SQL. Capacity is a decimal string; places that did not
match the territory master are grouped as typed.

### The project view (card and dossier)

Archived and live projects share one shape (`app/projects/view.py`, `project_card` and
`project_dossier`, which also take a live `Project`):

```json
{"source": "archive", "ref": "GN-0001", "id": 41,
 "fields": {"capacity_kw": {"value": "3.000", "source_table": "archived_projects", "record_id": 41},
            "contract_value_inr": {"value": "250000.00", "source_table": "archived_projects", "record_id": 41}},
 "sections": {"equipment": [{"value": {"category": "module", "make": "…", "model": "…", "rating": "540.00",
                                        "count": 6, "dcr": "yes"},
                              "source_table": "archived_projects", "record_id": 41}],
              "stages": [{"value": {"stage": "commissioned", "date": "2025-03-15"}, "source_table": "…", "record_id": 41}],
              "warranty": [], "service_requests": [], "documents": []}}
```
`source` is `live` or `archive`. `ref` is the Grid Nova reference for an archived project when it
has one, else `ARC-<id>`; `GNE-…` for a live project. **Every value carries `source_table` and
`record_id`** so an answer can cite where it came from; `record_id` is a list where the value is
an aggregate (installed units grouped by make and model). A card holds `scheme_or_segment,
system_type, capacity_kw, pump_hp, street_lamp_count, state, district, block,
commissioning_date, module_make, inverter_make, customer_name` (as the role allows). A live
project's fields add `current_stage`, `amount_received_inr`, `amount_outstanding_inr`,
`open_service_requests`, `associate_id`, `associate_name`; its sections come from installed
units, stage events, the warranty register and open service requests. `GET /projects/{id}` takes an
**archived** project id; a live project is reached through `project_dossier` (BE-9) or the live
endpoints.

### Who sees which fields

One map in `config/archive.yaml` (`visibility`) — **unconfirmed**, registry entry `ARC-2`. A field
or section the role may not see is **absent**, never null. The redaction happens inside
`view.py`.

| Group | Fields | Admin | Mgmt | Accounts | Operations | Engineer | Sales |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| technical | scheme, system type, capacity, pump HP, lamp count, all module / inverter / structure / battery / other equipment fields, `current_stage` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| location_district | `state`, `district` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| dates | order, installation, commissioning, net-meter, warranty-end dates; sections `stages`, `warranty` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| location_detail | `block`, `address_text`, `pincode`, `latitude`, `longitude` | ✔ | ✔ | ✔ | ✔ | ✔ | – |
| customer_identity | `customer_name`, `discom` | ✔ | ✔ | ✔ | ✔ | ✔ | – |
| customer_contact | `customer_phone`, `customer_email`, `consumer_number` | ✔ | ✔ | – | – | – | – |
| money | `contract_value_inr`, `subsidy_amount_inr`, `amount_received_inr`, `amount_outstanding_inr` | ✔ | ✔ | ✔ | – | – | – |
| service | `om_status`, `notes`, `documents`, `data_quality_flags`, `open_service_requests`; sections `service_requests`, `documents` | ✔ | ✔ | ✔ | ✔ | ✔ | – |
| associate | `associate_id`, `associate_name` (live projects) | ✔ | ✔ | ✔ | ✔ | ✔ | – |
| import_meta | `import_batch_id`, `source_row` | ✔ | ✔ | – | – | – | – |

**Scoping of live projects is unchanged:** through the view, Sales reads a live project only for a
lead assigned to them or unassigned, and Operations only a project in execution
(`ViewForbidden` otherwise). Archived projects have no assignee, so Sales and Operations read
them only through the narrow field sets above (no customer, address or money for Sales; no
contact or money for Operations). Aadhaar, full PAN, bank details and identity documents are not
fields of this API at all.
