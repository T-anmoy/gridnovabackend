# Handover, warranty and service requests (BE-5, Phase 6A)

Config: `config/handover_warranty.yaml`. Conventions: see [README.md](README.md) (structured
errors `{"detail": {"code", "message", "field"?, ...}}`, UTC ISO times, `message_key` + `params`
for associate to-dos). No response here carries a price, payment or commission; the pack's
`final_payment` item is a yes/no.

**Roles.** Read (pack, warranty, requests): Admin, Management, Engineer, Operations (every
project in execution), Sales (own or unassigned leads); Accounts 403. Act on the pack (ticks,
acknowledgement, customer link, PDF): Admin, Operations. Act on service requests (raise,
assign, notes, status): Admin, Operations, Engineer. Associates: the owner and a territory
Preferred Associate read; only the owner raises a request.

## Pack object (`GET .../handover`, staff and associate)

```json
{"onboarding_id": 12, "project_id": 7, "handover_recorded": false, "pack_required": true,
 "items": [{"key": "commissioning_report", "label": "...", "message_key": "handover.item.commissioning_report",
            "done": true, "source": "record|manual", "done_at": null, "done_by": null, "note": null}],
 "complete": false, "missing": ["orientation"], "done": 6, "total": 7,
 "warranty": null,
 "pdf": {"status": "not_generated|ready|held|failed", "held_reason": null, "generated_at": null},
 "can_act": true,
 "link": {"state": "none|active|revoked|expired", "issued_at", "issued_by", "expires_at", "revoked_at"},
 "email": {"status": "sent|skipped|failed|held|null", "skipped_reason", "emailed_at"}}
```
`can_act`, `link`, `email` are staff-only; the associate response adds
`can_raise_service_request`. Item keys: `commissioning_report`, `net_meter_report` (only where
the net-meter stage applies), `register_verified`, `final_payment` (also `through_milestone`),
`orientation`, `acknowledgement` (also `method`, `customer_name`), `om_contact`.
`warranty` (once handed over): `{handover_date, system:{years, starts, ends, active},
om:{free_months, free_until, free_period_active, terms_after_free_period|null},
panel_performance_life_years|null, units:[{unit_id, category, serial_number, make, model,
equipment_id, warranty_start|null, warranty_end|null, source: register|default|none, active}]}`.
`terms_after_free_period` is null while Grid Nova's text is still a placeholder.

## The gate

Recording `handover_complete` (staff direct record, or verifying an associate's submission) with
`handover.pack_required` true and an incomplete pack → **422**
`{"code": "handover_pack_incomplete", "message", "missing": ["orientation", ...], "missing_labels": [...]}`.
Evidence rules (`handover_checklist` document) are unchanged and checked first. Projects already
handed over are not re-checked. On success the warranty register is written, the customer link is
issued and the PDF is rendered/emailed in the background.

## Staff endpoints (`/api/v1/dashboard`)

| Method | Path | Roles | Body → response |
|---|---|---|---|
| GET | `/onboardings/{id}/handover` | read | → pack object |
| POST | `/onboardings/{id}/handover/orientation` | Admin, Operations | `{done: bool=true, note?}` → pack |
| POST | `/onboardings/{id}/handover/om-contact` | Admin, Operations | `{done, note?}` → pack |
| POST | `/onboardings/{id}/handover/acknowledgement` | Admin, Operations | multipart: `method` `signature`\|`in_person`, `customer_name`, `note?`, `file` (PNG, required for `signature`, ≤ `signature_max_kb`) → pack |
| POST | `/onboardings/{id}/handover/link` | Admin, Operations | `{send_email: bool=true}` → `{link:{url, expires_at}, pack}`; **issues a new link, the old one stops**; `url` is shown once |
| POST | `/onboardings/{id}/handover/link/revoke` | Admin, Operations | → pack |
| POST | `/onboardings/{id}/handover/pdf/regenerate` | Admin, Operations | → pack (renders in background) |
| GET | `/onboardings/{id}/handover/pdf` | read | → `{url, expires_at}` (signed) |
| GET | `/service-requests` | read | query: `status`, `open`, `overdue`, `within_warranty`, `category`, `assignee` (`me`/`unassigned`/id), `onboarding_id`, `page`, `page_size` → `{items, page, page_size, total, can_handle}` (items: request object + `customer_name`, `project_reference_id`) |
| GET | `/onboardings/{id}/service-requests` | read | → `{items, can_handle}` |
| POST | `/onboardings/{id}/service-requests` | Admin, Operations, Engineer | multipart: `category`, `description` (≤2000), `contact_phone?`, `files[]` (images, ≤ `max_photos`) → 201 request object with `history` |
| GET | `/service-requests/{rid}` | read | → request object + `history`, `customer_name`, `project_reference_id`, `can_handle` |
| POST | `/service-requests/{rid}/assign` | service actors | `{assignee_id: string\|null}` (active Admin/Operations/Engineer) |
| POST | `/service-requests/{rid}/status` | service actors | `{status: in_progress\|resolved\|rejected, note?}` (note required for resolved/rejected) |
| POST | `/service-requests/{rid}/notes` | service actors | `{note}` (internal) |
| GET | `/service-requests/{rid}/photos/{file_ref}` | read | → `{url, expires_at, filename, file_ref}` |
| POST | `/internal/check-service-requests` | Admin or `X-Internal-Key` | → `{overdue, newly_alerted}` or `{"status":"skipped","reason":"already_running"}` |

**Request object**: `{request_id, onboarding_id, category, description, status, raised_via
(staff|associate|customer), within_warranty (bool|null), warranty_end, photo_count, created_at,
closed_at, resolution_note, sla_due_at, overdue, raised_by, assignee_id, contact_phone,
updated_at, photos:[{file_ref, filename, content_type, size_bytes, received_at}]}` (+ `history`:
`[{kind: raised|status|assigned|note, actor, from_status, to_status, note, detail, at}]` for
staff detail). Categories: `no_generation`, `inverter_error`, `physical_damage`, `cleaning_om`,
`other`. Status: `open` → `in_progress` → `resolved` | `rejected` (open may go straight to either
end). `within_warranty` is fixed when raised (system warranty end ≥ today IST; null if unknown).
`sla_due_at`/`overdue` are null/false while `service.sla_hours` is null.

## Associate endpoints (`/api/v1/associate/onboardings/{id}`)

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/handover` | owner + territory | pack object (no link/email) + `can_raise_service_request` |
| GET | `/handover/pdf` | owner (territory only if `territory_viewers_see_documents`) | `{url, expires_at}`; 404 until rendered |
| GET | `/service-requests` | owner + territory | `{items, can_raise}` (no history) |
| POST | `/service-requests` | owner | multipart as staff → 201 |
| GET | `/service-requests/{rid}` | owner + territory | request object |

Home feed (`GET /associate/home`), new items (priority 3, informational):
`todo.handover_pending` (commissioned, not handed over), `todo.service_request_open`
(params `request_id, category, status, within_warranty`), `todo.service_request_closed`
(params `request_id, category, outcome`; for `service.associate_notice_days`).

## Public endpoints (`/api/v1/public/handover/{token}`) — no auth

Every response: `X-Robots-Tag: noindex, nofollow`, `Cache-Control: no-store`. An unknown,
revoked, expired or wrong-length token, or a project not handed over: **404** (`"Not found"`).
Rate limit per IP: 30 reads/60 s, 5 request submissions/600 s → **429**.

| Method | Path | Response |
|---|---|---|
| GET | `/{token}` | `{customer_name, project_reference_id, site_address, system:{installed_module_kw, connection_phase}, warranty, service:{phone, email, sla_hours, categories, max_photos}, pdf_available, link_expires_at}` |
| GET | `/{token}/pdf` | 302 to a signed URL; 404 while no PDF exists or it is held |
| GET | `/{token}/service-requests` | `{items}` — customer view: no `raised_by`, assignee, phone, photos or history |
| POST | `/{token}/service-requests` | multipart `category`, `description`, `contact_phone?`, `files[]` → 201 customer view; photos are typed by bytes (JPEG/PNG/WEBP, 5 MB each) and EXIF/GPS is stripped |

The customer email contains the **PDF link** (`{PUBLIC_API_BASE_URL}/api/v1/public/handover/{token}/pdf`).
The token is 32 random bytes, only its SHA-256 is stored; the token itself cannot be read back,
so re-sending means issuing a new link.

## Error codes

`handover_pack_incomplete` (422, `missing`), `handover_recorded` (409, pack frozen),
`execution_not_started` (409), `invalid_method`/`name_required`/`invalid_file` (422),
`file_too_large` (413), `handover_not_recorded` (409), `handover_pdf_held` (409,
`held_reason`), `no_link` (409), `not_handed_over` (409), `invalid_category`,
`invalid_description`, `too_many_photos` (422), `invalid_status`/`note_required`/`invalid_note`/
`note_too_long`/`invalid_assignee` (422), `invalid_transition`/`request_closed` (409),
`storage_not_configured` (503). Upload rejections: `invalid_file`, `heic_not_supported` (422),
413 for oversize.

## Work queues (`GET /dashboard/work-queues`)

For roles that can view execution, two new keys (SQL counts):
`handover: {awaiting_handover, packs_incomplete}` (commissioned and not handed over; of those, a
manual item — orientation, acknowledgement, O&M contact — still outstanding) and
`service_requests: {open, overdue}`.

## Held pack

The PDF is not rendered, and the customer is not emailed, while any printed text is still a
placeholder (O&M terms after year 1, company details) — `pdf.status = "held"`, staff get a
`handover_blocked` email. Values not yet chosen (panel performance life, per-category product
warranty years) are left out, never guessed.
