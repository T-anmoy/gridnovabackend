# Grid Nova backend — API contract docs

Written from the code (routes, request schemas, serializers, error codes), one file per
phase. Frontend sessions should paste these files instead of chat reports. When code and
these files disagree, the code wins — fix the file.

| File | Covers |
|---|---|
| [phase4a.md](phase4a.md) | Execution stages, serial register, payments ledger, receipts, placeholder registry, associate home feed |
| [phase4a_followups.md](phase4a_followups.md) | Payments keyed by project, admin execution override and mode switch, voided-serial flag, segregation of duties, bounced-cheque re-presentation |
| [phase3c_site_location.md](phase3c_site_location.md) | Required, confirmed site location (set / confirm / staff-correct), the `site_location` object, submit gate |
| [pma_pricing.md](pma_pricing.md) | Grid Nova's price list: instant estimate, `/pricing/packages`, proposal pricing content, payment schedule, Nova, catalogue-from-nameplate |
| [hf_c_equipment.md](hf_c_equipment.md) | Associate equipment catalogue, unmatched units and the staff match action, customer name on payment lists |
| [site_survey.md](site_survey.md) | S1 on-site site survey: survey fields on the onboarding, status, live results and warnings, signatures, the survey part of the submit gate; the Engineer's feasibility review and formal-proposal gate, corrections and proposal re-verification, the survey report PDF, the review queue and its overdue alert |
| [be1_public_contracts.md](be1_public_contracts.md) | BE-1: website chat sessions (`session_token`), paged leads list, accept-link codes, public booking limits, estimate-request calculator fields, health, auth changes, territory documents, validation, CSV, PDFs |
| [be3_deploy_contracts.md](be3_deploy_contracts.md) | BE-3: associate approval answers before the invitation/PDFs, scheduled-job `skipped` answer and new counters, readiness cache, stricter token claims, per-instance rate limits |
| [staff_contracts.md](staff_contracts.md) | BE-6: response shapes, roles, scoping and error strings for the staff lead endpoints (list/detail/assign/fields/stage/export/schedule-meeting), customer memory, memory ask and reflect; server-side lead filters; chat `locale` |
| [commissions.md](commissions.md) | BE-4: commission rate schedules, the commission ledger (earn / reversal / recovery), approval, monthly payout batches and TDS, Accounts exports (commissions, TDS, payout bank file), commission queries and their overdue job, the associate commission/payout views and home-feed items, the network summary; PAN encrypted at rest |
| [handover_warranty.md](handover_warranty.md) | BE-5: handover pack and gate, warranty register, handover PDF and the customer's tokenised link, service requests (staff / associate / customer), work-queue counts, `check-service-requests`, home-feed items |
| [project_archive.md](project_archive.md) | BE-8: Project Archive — Admin CSV import (template, dry run, commit, void), archived-project list / detail / statistics, the live-and-archive project view, per-role field visibility |
| [nova_records.md](nova_records.md) | BE-9: Nova Records, the internal staff assistant — ask, conversations, feedback, retention job; statuses, citations and paths, tables, role scoping and redaction, limits |
| [associate_ease.md](associate_ease.md) | BE-10: Associate referral links (`ref` / `share_with_referrer` on the public estimate request and call booking, the associate's referrals list and referral link, `referred_by` and its filter on staff leads, REF-1) and the estimated commission on the associate's customers |
| [attribution_lock.md](attribution_lock.md) | BE-11: the attribution lock lasts until the project is completed or closed (Associate Terms A7) — `attribution.locked` / `released_reason` replace `locked_until`, `attribution_lock_days` removed from `/options`, migration 0053, what the frontend must change |
| [commission_rates_individual.md](commission_rates_individual.md) | BE-12: an Associate's individual commission rate (Admin sets, Management reads; reduction needs 15 days' notice, increase today), the one rate resolver used by the ledger, estimate and displays, the associate's own rate, the per-Associate performance summary, the rate-change home-feed item and email, the first real schedule taking effect today, migration 0054 |
| [proposal_document.md](proposal_document.md) | BE-13: the proposal document — two fixed layouts (≤ 10 kWp Compact, > 10 kWp Detailed), the structured document and its computed figures, the unique reference, who may edit which section, the document / edit / preview routes and their errors, `missing_required` on approval and send, adjust-pricing `basic_price` / `rate_per_kwp`, migration 0055 |
| [solar_estimate.md](solar_estimate.md) | BE-14: the solar estimate engine — one calculation of size, generation, PM Surya Ghar subsidy (general / special-category / housing society, state top-ups with status), DISCOM tariffs and marginal savings, payback and 25-year figures; new instant-estimate inputs and outputs, `GET /pricing/estimate-assumptions`, the other callers, backward compatibility |
| `openapi.json` | Machine-readable schema — regenerate with `python scripts/export_openapi.py` (`--check` fails when stale) |

## Conventions (all phases)

**Prefixes.** Staff routes live under `/api/v1/dashboard/...` (plus `/api/v1/equipment`,
`/api/v1/proposal-inputs`, `/api/v1/pricing/breakdown`); associate routes under
`/api/v1/associate/...`; public routes under `/api/v1/public/...` and `/api/v1/pricing/...`
(`estimate`, `instant-estimate`, `packages`, `estimate-assumptions`).

**Authentication.**
- Production: a Clerk session token. Staff tokens resolve through `get_current_employee`,
  associate tokens (Clerk `public_metadata.principal = "associate"`) through
  `get_current_associate` (`app/auth/dependencies.py`).
- Development and test only (`ENVIRONMENT` exactly `development` or `test`): header pairs
  `X-Employee-Id` + `X-Employee-Role`, or `X-Associate-Id` (refused everywhere else: 403
  "Dev auth headers are only accepted in development and test.").
- A verified token without `public_metadata`: 403 `session_token_missing_claims` (BE-1).

**401 vs 403** (corrects earlier reports that said "401 for a wrong-audience token"):
- **401** — missing or invalid credentials. In dev-header mode a request carrying only
  the *other* audience's headers also gets 401, because it simply has no credentials for
  this audience.
- **403** with a real Clerk token of the wrong audience:
  - an associate token on a staff route → `"This account cannot access staff endpoints."`
  - a staff token on an associate route → `"This endpoint is for Grid Nova associates only."`
- **403** for a role or relationship refusal: a plain-string `detail`, e.g.
  `"Your role cannot view payments."`, `"Only Operations or Admin can change the serial register."`.
  An inactive associate gets 403 (`"This associate account is suspended."` etc.), so
  "active associates only" holds on every associate route.

**Lists.** Paged lists return `{items, page, page_size, total}` — including, since BE-1,
`GET /dashboard/leads`.

**Errors.** Business errors use a structured detail:
`{"detail": {"code": "<code>", "message": "<English>", "field"?: "<field>", ...extra}}`.
404s and role refusals use a plain-string `detail`. Pydantic validation failures are
FastAPI's standard 422 (`detail` is a list).

**Money** is sent as decimal strings (`"20000.00"`) everywhere it is a Decimal (payments,
milestones, totals, price-list amounts). Exceptions kept for backward compatibility: the
instant-estimate top-level cost fields and `proposal.total_value` are JSON numbers.

**Times** are ISO 8601, UTC, with offset (`2026-10-04T04:45:00+00:00`).

**Message keys.** To-do feed items and some notices carry `message_key` (+ `params`) for
the app to translate (English / Hindi / Bengali), e.g. `todo.payment_due`,
`payments.instructions.not_ready`, `payments.instructions.pay_only_here`.

**Roles** (staff): `admin`, `management`, `sales`, `engineer`, `operations`, `accounts`.
`tests/test_permission_matrix.py` is route-complete for every router documented here — it
fails when a route has no row, so the role tables in these files are the tested ones.
