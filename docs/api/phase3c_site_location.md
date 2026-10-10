# Phase 3C-A — required, confirmed site location

Commit `035c562`, migration `0042_site_location`. Code: `app/onboarding/site_location.py`,
`app/geo.py`, `app/onboarding/maps_link.py`.

A customer's site location is **required and must be confirmed by the associate** before
an onboarding can be submitted. One location fix per button press; no background
tracking.

## 1. Associate endpoints (owner only)

Base: `/api/v1/associate/onboardings/{onboarding_id}`. A Preferred Associate in the
territory, a sibling-territory associate or an unrelated associate gets 403.

### `POST /site-location`

Body — exactly one of:

```json
{"source": "device_gps", "latitude": 22.5726, "longitude": 88.3639, "accuracy_m": 12.0, "captured_at": "2026-10-04T10:15:00+05:30"}
{"source": "photo_gps", "photo_slot": "panel_installation_area"}
{"source": "google_maps_link", "link": "https://maps.app.goo.gl/…"}
{"source": "pasted_coordinates", "text": "22.5726, 88.3639"}
```

`accuracy_m` > 0 and ≤ 100000; `captured_at` ISO 8601; `link` ≤ 500; `text` ≤ 100.

Rules:
- Outside the India sanity box → 422 `outside_india` (+ `likely_swapped: true` when the
  pair reversed would be inside). Non-finite, out-of-range or (0,0) / near-zero →
  422 `invalid_coordinates`.
- `device_gps` / `photo_gps` accuracy above `accuracy_reject_m` → 422 `accuracy_too_low`
  (+ `accuracy_m`, `accuracy_reject_m`).
- `photo_gps` copies the coordinates (and `accuracy_m` if stored) from that slot's
  **camera** photo: 422 `no_gps_on_photo` with `reason` `no_photo` | `not_camera` |
  `no_gps`; unknown slot → 422 `unknown_photo_slot`.
- `pasted_coordinates` that do not parse → 422 `invalid_coordinates` (field `text`).
- A device/photo fix at or under `accuracy_good_m` is `verified: true`; every other
  source or poorer accuracy is accepted but unverified, with flags.
- Setting a location — any source, even the same coordinates — leaves it **unconfirmed**.
- Draft and submitted customers only; a submitted customer **returns to draft**;
  verified/closed → 409 `not_editable`.

Response: the full onboarding object plus `warnings` (standard warnings + location
warnings `{code, message_key: "site_location.warning.<code>", message}` with codes
`low_accuracy`, `accuracy_unknown`, `approximate_location`, `unverified_source`,
`far_from_photos`, `accuracy_poor`).

Google Maps link errors (422 unless noted), each with `message_key`
`site_location.<code>` and `field: "link"`:
- `invalid_link` + `reason` `empty` | `too_long` | `malformed` | `scheme` | `userinfo` |
  `port` | `host_not_allowed`
- `link_unresolvable` + `reason` `dns` | `blocked_address` | `timeout` | `network` |
  `too_many_redirects` (+ `guidance_keys`)
- `no_coordinates_in_link` (+ `guidance_keys`:
  `site_location.guidance.drop_pin_and_share`, `site_location.guidance.use_device_gps`,
  `site_location.guidance.paste_coordinates`)
- `link_rate_limited` — **429** (per associate per hour)

Supported URL forms (parsed without any request): place pin `!3d<lat>!4d<lon>` (exact;
last pair wins), `?q=` / `?q=loc:` / `?query=` (exact), `/maps/place/<lat>,<lon>` and
`/maps/search/<lat>,<lon>` (exact), `?ll=` and `/@<lat>,<lon>,<zoom>z`
(`viewport_center` → flag `approximate_location`). Short links (`maps.app.goo.gl`,
`goo.gl`) are expanded server-side (exact-host allowlist, ≤3 redirects, 5 s, body never
read). Not supported: degrees-minutes-seconds, `/dir/`, place names without coordinates.

### `POST /site-location/confirm`

No body. "This is the customer's house." 409 `site_location_not_set`; a stored location
outside India → 422 `outside_india`; verified/closed → 409 `not_editable`. Idempotent;
does not change the status. Response: onboarding object + `warnings`.

Any later change of the coordinates clears the confirmation (audited
`site_location_confirmation_cleared`).

## 2. Staff endpoint

`POST /api/v1/dashboard/onboardings/{onboarding_id}/site-location` — **admin, sales**
(management/engineer/operations/accounts 403; Sales scoped to own/unassigned leads).

Body: `{source: "google_maps_link" | "pasted_coordinates", link? ≤500, text? ≤100,
reason ≤1000}` — `link` required for a link, `text` for pasted coordinates (422);
`reason` ≥ 5 characters after trimming (422 `reason_required`). Stored as
`staff_entered`, unverified, **confirmed by that staff member**; the status does not
change; closed → 409 `not_editable`. Audited `site_location_corrected` with
before/after and the reason. Response: staff onboarding object + `warnings`.

## 3. The `site_location` object

On every onboarding object (`GET`/`PATCH`/create responses, list items, staff detail):

Owner and staff:
```
{present, latitude, longitude, source, accuracy_m, captured_at, verified, confirmed,
 confirmed_at, confirmed_by, precision, flags[], photo_distance_m, maps_url}
```
- `source`: `device_gps` | `photo_gps` | `google_maps_link` | `pasted_coordinates` |
  `staff_entered` | `unknown_legacy`
- `precision`: `exact` | `viewport_center` | null
- `flags`: `low_accuracy`, `accuracy_unknown`, `approximate_location`,
  `unverified_source`, `far_from_photos`, `location_missing`
- `photo_distance_m`: distance from the median of the camera photos' GPS fixes
- `maps_url`: `https://www.google.com/maps?q=<lat>,<lon>`
- Staff also get `check_with_customer` (any flag, or not confirmed).

**Preferred Associate viewing a downline customer:** `{present, confirmed}` only;
top-level `site_latitude`/`site_longitude` are `null`, and every photo's `latitude`,
`longitude`, `accuracy_m` are `null`. Owner/staff keep `site_latitude`/`site_longitude`.

Legacy: a raw `site_latitude`/`site_longitude` pair on `POST`/`PATCH` is still accepted,
stored as `unknown_legacy`, unverified, unconfirmed.

## 4. `/options` addition

`GET /api/v1/associate/onboardings/options` gains:
`site_location: {accuracy_good_m, accuracy_warn_m, accuracy_reject_m, sample_seconds,
required_to_submit, sources: ["device_gps","photo_gps","google_maps_link","pasted_coordinates"],
recommended_source: "photo_gps"}` (values from `config/associate_onboarding.yaml`
`location`, registered dummies L1–L7).

## 5. Submit gate

`POST /api/v1/associate/onboardings/{onboarding_id}/submit` on a draft returns 422:
```json
{"detail": {"code": "incomplete", "message": "Before submitting, …",
            "missing_documents": [], "missing_site_location": true, "site_location_unconfirmed": false}}
```
Both booleans are always present on `incomplete`. Since S1 the detail also always carries
`missing_survey_sections` and `survey_unsigned` (lists) — see [site_survey.md](site_survey.md) §6. Customers already submitted, verified
or closed are grandfathered (a submitted customer without a location can still be
verified by staff).

## 6. Photo `accuracy_m`

`POST /api/v1/associate/onboardings/{onboarding_id}/files` (`kind=photo`) accepts optional
`accuracy_m` (> 0, ≤ 100000), stored in the photo metadata and returned as
`photos[].accuracy_m` (`null` to a Preferred Associate). Ignored for documents.

## 7. Feed and list additions

Home feed kinds (draft customers only, priority 1): `site_location_missing`
(`todo.site_location_missing`) and `site_location_unconfirmed`
(`todo.site_location_unconfirmed`). `ready_to_submit` needs documents **and** a
confirmed location. `progress_summary.site_location`: `{present, confirmed}`.

## 8. Scoring

New factor `site_location` (weight 5; `site_readiness` 10 → 5): confirmed + verified 1.0,
confirmed + unverified 0.5, else 0.
