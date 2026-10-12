# BE-3 — deployment readiness: contract changes

BE-3 adds **no new routes**. It changes the timing of one response, adds fields to the
scheduled-job responses, and tightens token checks. Everything else (paths, bodies,
response shapes, error codes) is unchanged. Conventions: [README.md](README.md).

## 1. Associate approval answers before the invitation and documents

`POST /api/v1/dashboard/associate-applications/{application_type}/{application_id}/approve`
— Admin only, body and errors unchanged.

The approval (Associate ID, associate row, seat) still commits before the response. The
Clerk invitation and the two PDFs (ID card, welcome letter) now run **after** the
response, on the background runner. So the response's `associate` object normally shows:

| field | right after approval | once the background work has run |
|---|---|---|
| `clerk_invite_status` | `null` | `"sent"`, `"skipped"` (no Clerk key) or `"failed"` |
| `documents.id_card`, `documents.welcome_letter` | `false` | `true` (or `documents.error` set) |
| `documents.generated_at` | `null` | ISO time |

The dashboard should show "Invitation and documents are being prepared" while
`clerk_invite_status` is `null`, and re-read
`GET /api/v1/dashboard/associates/{associate_id}` (a few seconds later, or on demand). The
retry actions are unchanged and still run inline:
`POST .../associates/{associate_id}/clerk-invite`, `POST .../associates/{associate_id}/documents/regenerate`.

## 2. Scheduled jobs (`POST /api/v1/dashboard/internal/check-*`)

Auth unchanged: a valid `X-Internal-Key`, or an Admin. No key / wrong key / no token →
401; any other staff role → 403.

**One run at a time.** A run that starts while another run of the same job is still
going answers immediately:

```json
200 {"status": "skipped", "reason": "already_running"}
```

**New fields** (additive):

| job | new field | meaning |
|---|---|---|
| `check-stale-leads` | `reflections_skipped` (int) | quiet-lead reflections not made this run (cap `STALE_LEAD_REFLECTIONS_PER_RUN`) |
| `check-stale-leads` | `pdfs_retried` (int) | approved/sent/accepted proposals without a PDF, older than `SWEEPER_GRACE_MINUTES`, whose PDF was re-queued |
| `check-payment-alerts` | `receipts_retried` (int) | receipts still `pending` (never rendered/emailed) that were re-queued |

`check-execution-alerts`, `check-onboarding-escalations`, `check-survey-reviews`:
unchanged except for the skipped answer.

## 3. Health

- `GET /api/v1/health` — liveness, unchanged, never touches the database.
- `GET /api/v1/health/ready` — same body and status codes; the answer is now reused for
  `READINESS_CACHE_SECONDS` (default 15), so after a fix it can take up to that long to
  turn 200. It is the deploy gate (checked before traffic moves) — not a liveness or
  startup probe; both probes use `/api/v1/health`.

## 4. Authentication (Clerk session tokens)

A token is refused with **401** `{"detail": "Invalid or expired token."}` when, in
addition to the existing checks (signature against the JWKS, RS256 only, expiry,
issuer, `azp` ∈ `ALLOWED_ORIGINS` when present):

- it has no `sub`, an empty `sub`, no `exp` or no `iss` (a token without `sub` used to be
  accepted and created an employee with an empty id).

`azp` is compared with `ALLOWED_ORIGINS` exactly (scheme + host, ignoring a trailing
slash on `azp`). The backend reads the role from the **session token**, not from Clerk's
user object: the Clerk instance must add `public_metadata`, `email` and `name` to the
session token (deploy/RUNBOOK.md §D8). Without it every staff and associate call is
**403** `{"detail": {"code": "session_token_missing_claims", "message": …}}` (unchanged
from BE-1) even though the dashboard menus — which read `user.publicMetadata` — look right.

## 5. Rate limits

Same limits, same 429 bodies. They are counted **per Cloud Run instance**: with N
instances a visitor can get up to N × the limit. Idle keys are now forgotten, so memory
no longer grows with the number of visitors.

## 6. Lists whose implementation changed (shapes unchanged)

- `GET /api/v1/dashboard/activity/recent` (Sales): same 15 items and order; visibility is
  resolved in batches.
- `GET /api/v1/dashboard/associate-applications`: same `{items, page, page_size, total}`,
  newest `submitted_at` first (ties: higher id first); paginated in SQL.
