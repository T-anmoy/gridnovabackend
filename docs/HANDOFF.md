# Grid Nova Energy — AI Operations Platform: Full System Handoff

> **For the incoming tech lead and their Claude sessions.** Read this file first, then the two repos' own docs it points to. It describes what the system is, everything built so far, how it is organised, the rules the codebase follows, what is still open, and exactly how to deploy it. Snapshot date: **26 Oct 2026**. Backend `main` = `5eae788` (BE-15). Frontend `main` = `ff41eca` (FE-15).

---

## 0. How to work on this system (read before touching code)

**Order to read, for a human or a Claude session:**
1. This file.
2. Backend: `README.md`, then `deploy/RUNBOOK.md` (deployment, step by step), then `docs/api/README.md` (contract index).
3. Frontend: `README.md`, `PROGRESS.md`, `AGENTS.md` / `CLAUDE.md` (Next.js 16 rules: *"This is NOT the Next.js you know"* — read `node_modules/next/dist/docs/` before writing Next code).
4. `config/placeholder_registry.yaml` (backend) — every unconfirmed business value.

**Rules the codebase has followed throughout (keep them):**
- **The backend contract is the only source of truth for the frontend.** Every API change ships a contract doc in `grid-nova-ai-backend/docs/api/*.md` plus a regenerated `docs/api/openapi.json`. After a backend change is pushed, copy the changed `docs/api/*.md` and `openapi.json` into the frontend at **both** `docs/api/openapi.json` and `tests/contract/openapi.json`. Frontend contract-drift tests fail on mismatch. The frontend never invents fields or computes money.
- **Business values live in YAML config, not code.** `config/*.yaml`, validated at startup. Anything not confirmed by Grid Nova carries an `[UNCONFIRMED]`/status marker **and** an entry in `config/placeholder_registry.yaml` (`blocking: blocks_launch | blocks_feature | tunable`). Tests enforce the marker ↔ registry link.
- **Money:** Python `Decimal`, `ROUND_HALF_UP`, never floats in calculation paths (a test scans for floats in `app/estimate/`). The browser never does money maths.
- **Zero hallucination:** if a value, rule or API field is not in the repo or confirmed by Grid Nova, stop and ask; never guess. Prices, rates, warranty terms, signatory name, subsidy amounts are never hard-coded in UI (scan tests exist).
- **Production guard:** with `ENVIRONMENT=production|staging` the backend refuses to start on unsafe config (SQLite, empty Clerk/Fernet/identity/internal keys, localhost origins, `TRUSTED_PROXY_HOPS=0`, …). Do not weaken it.
- **Schema is never changed on boot** in staging/production — only by the migration job (`scripts/bootstrap_db.py` + Alembic). New Alembic revision ids ≤ 32 chars.
- **Languages:** public site and associate portals in English, Hindi, Bengali (`messages/en.json` is the reviewed source; `hi`/`bn` are flagged `machine-draft` until a native speaker signs off). Staff dashboard is English-only.
- **Workflow used so far:** one numbered task per Claude Code session (BE-n backend, FE-n frontend), each with a Phase 0 (trace the code, report, no edits) → build → tests baseline vs after → written report → human review → commit on `main` → push → CI green. Backend first, then contract copy, then frontend. One frontend session at a time.
- **Git:** work on `main` (no feature branches/PRs so far). Never commit `.env*`, `*.db`, `_private/`, `test-results/`.
- **Shell note (owner's Mac, zsh):** pasted commands must not carry trailing `# comments`.

---

## 1. What the system is

**Client:** Grid Nova Energy (rooftop and utility solar EPC, West Bengal and Bihar first; offices in India). **Built by:** Highline Work. **Owner contact:** Tanmoy Halder (Highline Work) — also manages the **gridnova.in DNS**.

**Purpose:** one platform that runs Grid Nova's whole customer journey — public website and instant solar estimate → lead capture and AI assistant → staff CRM → site survey → priced proposal (Grid Nova's fixed PDF templates) → customer acceptance → payments and receipts → execution stages and serial register → handover and warranty/O&M — plus an **Associate Network** (referral/sales/execution partners with commissions) and internal AI tools.

| Part | Repo (GitHub) | Local folder (owner's Mac) | Hosting |
|---|---|---|---|
| Backend API, AI agents, PDFs, jobs | `T-anmoy/gridnovabackend` | `~/Desktop/grid-nova-ai-backend` | **Not deployed yet.** Target: Google Cloud Run + Cloud SQL Postgres 16, `asia-south1` (Mumbai) |
| Website, portals, staff dashboard | `T-anmoy/gridnova` | `~/Desktop/grid-nova-energy-wip` | **Live on Vercel** at https://www.gridnova.in (region `bom1`) |

**Important live-state fact:** the website is live but `api.gridnova.in` does not exist, so on production today the calculator result, Ask Nova chat, booking slots and the lead CRM mirror cannot reach a backend (lead notification emails still go out from the website via Resend). Deploying the backend fixes this.

---

## 2. Scope and deliverables (everything built)

### 2.1 Public website (`app/(site)/[lang]/…`, en/hi/bn)
Home, services, industries, projects, technology, sustainability, about, leadership, careers (Associate Network recruitment), contact, estimate request, **solar calculator**, book-a-call, legal pages (privacy, terms, associate terms, commission policy, payment policy), verify (associate ID/QR check), 404/error pages. SEO: sitemap, robots (blocks dashboard/portals/API), metadata, AVIF images, security headers, CSP. Ask Nova floating assistant on public pages.

### 2.2 Solar calculator v2 (BE-14 + FE-14)
State → DISCOM → city/PIN; home or housing society (number of flats); usage by units (preferred), bill, or roof area; optional phase, sanctioned load, roof, shading. Result: recommended size with reason, a smaller "floor" option with coverage %, both packages' list prices (West Bengal and Bihar residential on-grid only; everything else = "custom quote" with reasons), subsidy breakdown (central CFA general/special-category, housing-society CFA, state top-ups with **Confirmed / Announced** status), net cost (confirmed subsidies only), generation, savings, payback, 25-year figures, confidence level, warnings, and a "How we calculate" panel (`GET /api/v1/pricing/estimate-assumptions`). Engine: `app/estimate/` (pure Decimal), config `config/solar_estimate.yaml` + `config/subsidies.yaml`; every value has `status/source/as_of`; `estimate_version` = hash of both files. Contract: `docs/api/solar_estimate.md`.

### 2.3 Lead capture, CRM and AI
- Public intake (estimate, contact, configurator/calculator leads, booking) → backend leads; website also emails staff via Resend and mirrors leads server-to-server with `X-Internal-Key`.
- Ask Nova: public chat assistant (LangGraph agents, OpenAI-compatible LLM, guardrails, customer memory with episodic recall/reflection). Mock mode when `LLM_API_KEY` is empty (refused in production).
- Staff dashboard leads: list/filters/export, assignment, stages, notes, meetings (Google Calendar OAuth), stale-lead alerts, attribution flags, audit log, analytics.
- **Nova Records:** internal staff assistant over company records with role scoping and redaction.
- Agent governance: daily action limits, disabled agents, slow-run logging, translation checks.

### 2.4 Proposals (BE-13 + FE-13)
- Two **fixed layouts** from Grid Nova's sample PDFs: **Compact** (≤ 10 kWp, 3 pages) and **Detailed** (> 10 kWp, 8 pages), chosen automatically. Constant frame: header band, watermark, page footer (all pages except last), QR (www.gridnova.in) + letterhead on the last page. **Signatory fixed: RAJU PAUL, Authorised Signatory** (config, not editable via API).
- Structured document per proposal (`Proposal.content.document`): addressee, connection, site, PM Surya Ghar status, plant, system, BOQ, customer scope, payment terms, notes; price via `adjust-pricing` only (total incl. GST, basic price, or rate per kWp). GST rule 8.9 % (5 % on 70 % + 18 % on 30 %). Amounts in Indian words.
- Reference `GN/DD/MM/YYYY/NN` allocated atomically on approval; revisions `-R1`, `-R2`. Aadhaar last-4 only.
- Role-based editing (server `editable_sections`), optimistic concurrency (`stale_version`), field-level audit, `missing_required` blocks approve/send, draft preview PDF stamped "DRAFT — NOT FOR CUSTOMER" (20 renders/min), manager sign-off tier, request-revision (Admin/Sales), customer accept page with key figures.
- Contract: `docs/api/proposal_document.md`. PDF: WeasyPrint 70, data-URI-only fetcher, font stack Liberation Sans → Noto Indic.

### 2.5 Site survey, onboarding, execution, payments, handover
- Site assessments and surveys (associate-submitted, staff review/corrections, results with sizing/price), customer onboarding (formal proposal drafts, documents, agreements).
- Payments ledger (milestones 20/60/20 Compact, 25/60/15 Detailed), verification, receipts (PDF), payment alerts.
- Execution stages, serial register of installed equipment (dummy equipment catalogue in dev only), execution alerts.
- Handover and warranty, service requests with SLA alerts, public handover/receipt links.
- Project archive (admin CSV import of past projects, statistics).

### 2.6 Associate Network (portal currently **closed** in production)
- Public application (Associate / Preferred Associate; KYC; date of birth 18+; documents), territory lists (State → District → Block), approval → Clerk invitation, ID card with QR, welcome documents.
- Portals `/associates/portal`, `/preferred/portal` (en/hi/bn): customers, referrals and links, estimates, surveys, execution, handover, payments, earnings, commission rate card.
- Commissions: append-only rate schedules; per-associate individual rates (BE-12/FE-12: reductions need 15 days' notice, increases same day, optional min/max), ledger, payout batches, queries, attribution lock (until project completed/closed), estimated commission, Network Command Centre, performance summary.
- Switch: `NEXT_PUBLIC_ASSOCIATE_PORTAL_OPEN` (unset = closed in production builds).

### 2.7 Staff dashboard (`/dashboard/*`, English)
Overview, leads, onboardings, operations, accounts, commissions, Network Command Centre, survey reviews/corrections, service requests, attribution flags, meetings, site assessments, proposals, analytics, employees, associate network, data checklist (placeholders), audit log, Ask Nova, project archive + import, equipment, price list, agents. Roles: `admin`, `management`, `sales`, `engineer`, `operations`, `accounts` (server-enforced; role tables tested).

### 2.8 Quality work delivered
Security hardening (BE-1), deployment readiness + CI (BE-3), performance (FE-10), careers redesign (FE-11/11b), responsive polish with a 21-viewport visual matrix (FE-15: `npm run test:visual`), shared `DateField` (typed DD/MM/YYYY + calendar with month/year dropdowns; replaced all 39 native date inputs), CORS on 500s (BE-15), SEO growth plan (separate PDF, not in repo).

---

## 3. Architecture

```
 Browser (customer / associate / staff)
   │  HTTPS
   ▼
 Vercel — Next.js 16 app (www.gridnova.in, region bom1)
   │  public pages, portals, /dashboard; Clerk session in cookie
   │  server routes: /api/contact, /api/estimate-request, /api/configurator-lead
   │     → Resend email to staff  + mirror to backend with X-Internal-Key
   │  browser → backend directly with Clerk JWT (Authorization: Bearer)
   ▼
 api.gridnova.in → Google global HTTPS load balancer (managed cert, static IP)
   ▼
 Cloud Run "gridnova-api" (FastAPI, min 1 instance, ingress = load balancer only)
 Cloud Run "gridnova-jobs" (same image, private; Cloud Scheduler via OIDC + X-Internal-Key)
 Cloud Run job "gridnova-migrate" (bootstrap_db.py + Alembic + seed_territories.py)
   │
   ├── Cloud SQL Postgres 16 (asia-south1; HA + PITR in production)
   ├── Secret Manager (all secrets)
   ├── Cloudflare R2 (private bucket: proposal PDFs, receipts, KYC docs; signed URLs only)
   ├── OpenAI-compatible LLM (Ask Nova, agents, Nova Records)
   ├── Clerk (staff + associate auth; JWT verified against Clerk JWKS)
   ├── Resend (customer/staff/associate emails from notifications@gridnova.in)
   ├── Google Calendar OAuth (meeting slots for Book-a-Call)
   └── Sentry (errors, PII scrubbed), optional Slack webhook
```

**Stack.** Backend: Python 3.12 (local venv currently 3.13 works), FastAPI, SQLAlchemy 2, Alembic, Pydantic, LangGraph, WeasyPrint 70, boto3 (R2), hash-pinned `requirements.lock`. Frontend: Next.js 16.3, React 19.2, TypeScript, Tailwind 4, shadcn/ui (Radix), TanStack Query, Clerk 7, Vitest, Playwright, Lenis smooth scroll on public pages.

**Auth.** Clerk. The backend reads role from the **session JWT** claims `public_metadata.role` (staff) or `public_metadata.principal` + `associate_id` (associates), plus `email`, `name`. Each Clerk instance must have a customised session token (RUNBOOK §8) or every backend call returns 403 `session_token_missing_claims`. Clerk's `azp` must be in `ALLOWED_ORIGINS`. Dev-only header bypass (`X-Employee-Id`/`X-Employee-Role`, `X-Associate-Id`) works only when `ENVIRONMENT` is `development` or `test`.

**Background work.** In-process thread pool after the response (emails, PDFs, receipts, documents, reflections) + sweepers in the scheduled jobs retry anything older than `SWEEPER_GRACE_MINUTES`. Scheduled jobs (`deploy/scheduler.yaml`, IST): check-stale-leads, check-onboarding-escalations, check-survey-reviews, check-payment-alerts, check-execution-alerts, check-commission-queries, check-service-requests.

**Rate limits** are in memory per instance (abuse brakes, not quotas).

---

## 4. Repository maps

### 4.1 Backend `grid-nova-ai-backend`
| Path | What |
|---|---|
| `app/main.py` | App, middleware order (UploadSizeLimit → UnhandledErrorMiddleware → CORS), lifespan |
| `app/config.py` | Settings + production startup guard (`check_production_config`) |
| `app/api/routes/` | ~40 route modules: public (`public_*`, `chat`, `pricing`, `health`), staff (`dashboard`, `*_admin`, `proposal_document`, `nova_records`, …), associate (`associate_*`) |
| `app/estimate/` | Solar estimate engine (config, tariff, subsidy, engine, assumptions) |
| `app/pricing/` | Price list application, GST, packages, amount in words |
| `app/proposals/` | Document model/build/figures/edit, render, reference allocation, content engine, send guard |
| `app/pdf/` | WeasyPrint generator, data-URI fetcher, assets (header/footer/watermark) |
| `app/commissions/`, `app/associates/`, `app/onboarding/`, `app/survey/`, `app/execution/`, `app/payments/`, `app/handover/`, `app/projects/`, `app/archive/`, `app/records/` | Domain modules |
| `app/agents/`, `app/orchestrator/`, `app/llm/`, `app/memory/`, `app/governance/` | AI agents, LLM client, customer memory, governance |
| `app/auth/` | Clerk JWT verification, roles, dev bypass |
| `app/security/` | Encryption (Fernet, identity HMAC), rate limits, client IP, Sentry scrubbing, error responses |
| `app/storage/r2.py` | R2 client, signed URLs |
| `app/notifications/notifier.py` | Resend email / Slack / log notifiers |
| `config/*.yaml` | 16 business config files incl. `price_list.yaml`, `pricing_policy.yaml`, `proposal_document.yaml`, `commissions.yaml`, `solar_estimate.yaml`, `subsidies.yaml`, `placeholder_registry.yaml` |
| `alembic/` | Migrations, head `0055_proposal_document_reference`; `alembic -c alembic/alembic.ini …` |
| `scripts/` | `dev_setup.py`, `bootstrap_db.py`, `seed_territories.py`, `seed_dummy_data.py`, `dev_storage.py` (local S3), `export_openapi.py`, `list_placeholders.py`, `rotate_fernet.py`, `rehash_identity_claims.py`, `smoke_fonts.py`, `eval_nova.py`, `lock_requirements.sh` |
| `deploy/` | `RUNBOOK.md`, `env.template.yaml`, `scheduler.yaml`, `scheduler_commands.py` |
| `docs/api/` | Contracts (README index, one file per feature) + `openapi.json` |
| `.github/workflows/backend-tests.yml` | ruff, pip-audit, SQLite suite, Postgres 16 suite (~1 h), Docker build + font smoke |
| `Dockerfile` | Non-root image; excludes tests, `.env`, `*.db` |
| `_private/` (git-ignored) | Grid Nova's sample proposal PDFs and renders — do not commit |

### 4.2 Frontend `grid-nova-energy-wip`
| Path | What |
|---|---|
| `app/(site)/[lang]/` | Public site + `associates/` (apply, portal, sign-in) + `preferred/` (portal, sign-in) |
| `app/(internal)/dashboard/` | Staff dashboard (layout = shell with scrolling sidebar; `sign-in`) |
| `app/(internal)/proposal/accept/[token]` | Customer accept page (public) |
| `app/api/` | Server routes: contact, estimate-request, configurator-lead |
| `proxy.ts` | Route protection (Next 16 "proxy", formerly middleware) |
| `components/` | `dashboard/`, `associate/`, `pricing/` (`SolarEstimateResult`, `PackageCards`, `HowWeCalculate`), `forms/DateField.tsx`, `careers/`, `AskNova.tsx`, `ServicesGrid.tsx`, … |
| `lib/` | `config.ts` (`API_BASE_URL` — the only place the API URL is read; production build fails on localhost), API clients per area, schemas, `pricing/`, `proposals/`, `security-headers.ts`, `env.ts` |
| `messages/{en,hi,bn}.json` | All copy |
| `docs/api/` | Copied backend contracts |
| `tests/unit`, `tests/contract`, `tests/e2e`, `tests/visual` | Vitest, contract drift, Playwright, visual matrix |
| `next.config.ts` | Images, headers; `VISUAL_STUB_AUTH=1` only for the visual matrix (refuses non-`.test` API hosts) |
| `.github/workflows/frontend-tests.yml` | Lint/types/unit/build + Playwright |

---

## 5. Key business facts (confirmed unless noted)

- **Packages:** Nova Assured (default) and Nova Signature (+12 % before GST). Price list = `config/price_list.yaml` (source PDF hash recorded); applies to **West Bengal and Bihar, residential, on-grid, known phase, 1–10 kW single phase / 3–10 kW three phase**; everything else is a custom quote. Size rounding: next whole kW.
- **GST:** 8.9 % effective. **Generation:** 120 units/kWp/month (Grid Nova). **Roof area:** 100 sq ft/kWp for sizing (proposal prints 83–100; unconfirmed PRO-17).
- **PM Surya Ghar CFA (MNRE):** general ₹30,000/kW first 2 kW + ₹18,000 third kW, max ₹78,000; special-category states ₹33,000 + ₹19,800, max ₹85,800; housing society ₹18,000/kW (₹19,800 special) on min(installed, 3 kW × houses, 500 kW); pro-rata fractions; DCR modules required. State top-ups: Bihar (announced) and West Bengal (announced, GO not located) — shown, never deducted until confirmed.
- **Proposals:** validity 15 days (samples); payment terms 20/60/20 (Compact), 25/60/15 (Detailed); panel life 25 years; signatory Raju Paul.
- **Commissions (decided in chat, to be entered by Admin, Sirs' approval pending):** Associate 7.5 % (enter under both Associate rate keys: sales and execution), Preferred Associate 8 %, territory roles 0 %; individual rates adjustable by Admin.
- Grid Nova people (approvers, "the Sirs"): Raju Paul and Shouvik Biswas (Partners), Michael Gomes (Managing Partner), Srinjay Das (VP Business), Moloy Das (GM Projects), Naren Mukherjee (Senior Advisor), Arun Kumar (Zonal Sales Manager).

---

## 6. Local development

**Backend** (`~/Desktop/grid-nova-ai-backend`):
```
source .venv/bin/activate
python scripts/dev_setup.py --dummy-data
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
uvicorn app.main:app --reload --port 8000
```
- WeasyPrint on macOS needs `brew install pango` and the `DYLD_FALLBACK_LIBRARY_PATH` line.
- Local file storage: `pip install "moto[server]"` once, then `python scripts/dev_storage.py` (S3 on `127.0.0.1:5055`, bucket `gridnova-dev`; in memory).
- **The owner's old `gridnova_dev.db` is at schema 0035 and cannot be upgraded** (fails at 0036). Move it aside (`mv gridnova_dev.db gridnova_dev.db.old-oct18`) and rebuild with `dev_setup.py`. A stale schema shows in the browser as 500s; `/api/v1/health/ready` reports `schema: behind`.
- Tests: `python -m pytest -q`; Postgres: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/gridnova_test python -m pytest -q`; `ruff check .`; `python scripts/export_openapi.py --check`. With a filled local `.env`, 3–5 storage/notifier tests fail by design of the environment; CI (no `.env`) is the reference.

**Frontend** (`~/Desktop/grid-nova-energy-wip`): `npm run dev`, open **http://localhost:3000** (not 127.0.0.1 — CORS). Checks: `npx vitest run --maxWorkers=3`, `npx tsc --noEmit`, `npm run lint`, `NEXT_PUBLIC_API_URL=https://api.example.test npm run build`, `npm run test:e2e`, `npm run test:visual` (needs a migrated local backend). Never `npx prettier --write`.

**Clerk dev instance** (`set-sparrow-38.clerk.accounts.dev`): add the session-token claims (RUNBOOK §8 step 1), set your user's Public metadata `{"role":"admin"}`, sign out/in at `/dashboard`.

**Local env files** exist on the owner's Mac (backend `.env`, frontend `.env.local`) with matching `INTERNAL_API_KEY`; both git-ignored. Every variable is documented in each repo's `.env.example`.

---

## 7. Current state (26 Oct 2026)

| Item | State |
|---|---|
| Backend `main` | `5eae788` BE-15, clean, pushed; CI green through BE-14 (verify BE-15 run) |
| Frontend `main` | `ff41eca` FE-15, clean, pushed; CI green |
| Tests | Backend ≈ 3,950 SQLite / ≈ 3,980 Postgres; frontend 2,476 unit + 56 Playwright; visual matrix 1,680 combinations pass |
| Website | Live on Vercel; backend not deployed; Clerk on **dev keys** (causes ~2 s redirect on protected routes) |
| Associate portal | Closed in production |
| Placeholder registry | 156 entries: open = 22 `blocks_launch`, 39 `blocks_feature`, 69 `tunable` (+ superseded/resolved) |
| hi/bn copy | Machine drafts, need native review |

Task history (for `git log` context): BE-1…BE-15 and FE-1…FE-15, HF-* hotfixes, Phase 3–4A. Latest: BE-12/FE-12 individual commission rates; BE-13/FE-13 proposal templates + editor; BE-14/FE-14 estimate engine + calculator v2; BE-15 CORS on 500s; FE-15 responsive polish.

---

## 8. Known issues and risks

| Issue | Status / action |
|---|---|
| Live site calls a backend that does not exist | Fixed by deployment |
| `ASSOCIATE_INVITE_REDIRECT_URL` template value is `https://<host>/associates/sign-up`, but the frontend has **no `/associates/sign-up` route** (only `associates/sign-in/[[...sign-in]]`) | **Verify on staging**: invited associate must land on a page that completes Clerk's invitation (sign-up with ticket). Either add a sign-up route or point the variable at the correct page |
| Savings figures optimistic until real tariffs (bill − new bill counts fixed charges/duty as saved) | Needs real WBSEDCL/CESC/Bihar bills or tariff orders; estimates are labelled medium/approximate |
| West Bengal and Bihar state subsidy top-ups unverified (no GO seen) | Status `announced`; never deducted |
| Bihar bill-only estimates add back 125 free units | Correct only if the scheme still runs (EST registry) |
| Dev `.env` was shared outside the team (per RUNBOOK §5) | **Every staging/production secret must be new** |
| Rate limits per instance | Acceptable at `--max-instances=4`; Redis if exact limits needed |
| Some PDF rendering checks skipped locally (Hindi/Bengali fonts) | Covered by CI and the image's `smoke_fonts.py`; also in smoke test §11 |
| Book-a-Call has no slots until a staff Google Calendar is connected | Connect after production deploy |

---

## 9. Open inputs from Grid Nova

**Phase 1 (company launch):** CMP-1 GSTIN · P5 legal entity name, GSTIN, bank account + IFSC · P6 receipt wording / GST tax invoices? · PRO-2 confirm sample terms suffice (no separate counsel T&C) · BE1-1 15-day validity (decided — record) · HW-OM O&M after year one (before first handover) · PRO-7/8/10/11/12 scope text for utility, government, PM-KUSUM, unclassified (only those proposal types blocked).

**Phase 2 (Associate Network):** C5-RATES standard rates (decided, Sirs' approval) · C5-1 notice 15 days (decided — record) · C5-2 commission incl./excl. GST · C5-8 query window · C5-10 payout schedule · C5-11/12 TDS (CA confirmation) · ASC-1 KYC consent notice · ASC-2 welcome letter · ONB-1 customer information notice · ONB-5 customer agreement · soften careers territory-commission wording.

**Accuracy (non-blocking):** 3–4 real electricity bills; BE-14's 12 estimate questions (state subsidy orders, Bihar free units, panels per "1 kW", roof area, 90 % target, degradation, export credit, next states, ₹/unit range, proposal state line); 69 tunables (`python scripts/list_placeholders.py`); staff alert addresses (P7); scheduler times (BE3-1).

Recommended: **two-phase launch** — Phase 1 needs only the Phase 1 list; the portal switch stays off until Phase 2 inputs arrive.

---

## 10. Deployment guide (staging → production)

`deploy/RUNBOOK.md` is authoritative; this is the ordered plan with the decisions already made.

### 10.1 Accounts to create (new credentials everywhere)
| Service | Staging | Production |
|---|---|---|
| Google Cloud | project `gridnova-staging` + billing | project `gridnova-prod` + billing |
| Clerk | separate application (test keys) | production instance on `gridnova.in` (live keys) |
| Cloudflare R2 | bucket `gridnova-staging` + Object R/W token | bucket `gridnova-prod` + token |
| OpenAI | project key, spend limit | project key, spend limit |
| Resend | — (or the same verified domain, separate key) | verify `gridnova.in`, key |
| Google OAuth (Calendar) | client + staging redirect URI | client + `https://api.gridnova.in/api/v1/dashboard/employees/me/calendar/callback` |
| Sentry | project | project (server-side scrubbing on) |
| Vercel | Preview env vars → staging API | Production env vars → `https://api.gridnova.in` |
| Search Console | — | verify www.gridnova.in, submit sitemap |

Tools on the deploying machine: `gcloud` (Owner on the project for first setup), `git`, Python 3.12 + PyYAML, `curl`, `openssl`.

### 10.2 Staging (RUNBOOK §0–§9, §11)
1. §0 variables (`ENV=staging`, `PROJECT_ID`, `REGION=asia-south1`, …), §1 enable APIs.
2. §2 Artifact Registry + `gcloud builds submit` (Cloud Build, linux/amd64).
3. §3 Cloud SQL Postgres 16 (7-day PITR on staging), §4 service accounts (`gridnova-run`, `gridnova-scheduler`).
4. §5 Secret Manager: `gridnova-database-url`, `-clerk-secret-key`, `-fernet-key`, `-identity-hmac-key`, `-internal-api-key`, `-llm-api-key`, `-storage-access-key`, `-storage-secret-key`, `-resend-api-key`, `-google-oauth-client-secret`, `-sentry-dsn`, (`-slack-webhook-url` optional). Values via stdin, never in history.
5. Copy `deploy/env.template.yaml` → `deploy/env.staging.yaml` (no secrets; may be committed): `ENVIRONMENT: "staging"`, `TRUSTED_PROXY_HOPS: "1"`, `ALLOWED_ORIGINS` = the Vercel **branch alias** of the staging frontend (stable), `FRONTEND_BASE_URL`, `PUBLIC_API_BASE_URL` = the `*.run.app` URL, `CLERK_JWT_ISSUER` = staging Clerk Frontend API URL, `ASSOCIATE_INVITE_REDIRECT_URL` (see known issue), R2 bucket/endpoint, `NOTIFY_FROM_EMAIL`, `STAFF_NOTIFY_EMAILS`, Google client id/redirect.
6. §6 deploy `gridnova-api` (min 1, no CPU throttling; staging may use min 0) and `gridnova-jobs` (private, min 0).
7. §7 migration job (`bootstrap_db.py` then `seed_territories.py`), wait, then `/api/v1/health/ready` = 200 before traffic.
8. §8 Clerk: session-token claims, first Admin `{"role":"admin"}`.
9. §9 Cloud Scheduler via `deploy/scheduler_commands.py` (OIDC + `X-Internal-Key`), run each job once.
10. Vercel Preview env: `NEXT_PUBLIC_API_URL` = staging API, `NEXT_PUBLIC_PRODUCTION_API_HOST=api.gridnova.in`, Clerk test keys, `INTERNAL_API_KEY` = staging value, `RESEND_API_KEY`, `ESTIMATE_NOTIFY_EMAIL` (a team address). Optionally `NEXT_PUBLIC_ASSOCIATE_PORTAL_OPEN=true` on Preview only to test Phase 2.
11. **Gate 1 — §11 smoke test** (health, readiness, CORS allow/deny, Clerk token → `/employees/me`, dev headers refused, internal job with/without key, territories, `/docs` 404, manual checks: Google Calendar, Resend, R2 signed links, Hindi/Bengali PDF, associate approval + invitation, every scheduler job, rate limit), then the full functional walk-through on a phone and tablet. Nothing goes to production until every item passes.

### 10.3 Production (RUNBOOK §0–§13)
1. Same steps in `gridnova-prod` (HA Cloud SQL, 30 backups + 7-day PITR, deletion protection).
2. §10 custom domain: global external Application Load Balancer + serverless NEG + Google-managed certificate for `api.gridnova.in` (Cloud Run domain mappings are not available in asia-south1). Static IP → **DNS A record (owner adds, §11 below)** → certificate ACTIVE (up to ~1 h) → `--ingress=internal-and-cloud-load-balancing` → `TRUSTED_PROXY_HOPS: "2"`, `PUBLIC_API_BASE_URL: "https://api.gridnova.in"`, `ALLOWED_ORIGINS: '["https://www.gridnova.in","https://gridnova.in"]'` (confirm which hosts the site answers on).
3. Production Clerk instance on `gridnova.in` (DNS records via the owner), session-token claims, first Admin, `CLERK_JWT_ISSUER=https://clerk.gridnova.in`.
4. Test one Cloud SQL restore (§12–13). Set log retention. R2 lifecycle: abort incomplete multipart uploads after 1 day; no expiry on records.
5. §11 smoke test against `https://api.gridnova.in`.
6. Vercel Production env: `NEXT_PUBLIC_API_URL=https://api.gridnova.in`, `NEXT_PUBLIC_PRODUCTION_API_HOST=api.gridnova.in`, Clerk live `pk_live`/`sk_live`, `INTERNAL_API_KEY` (= production backend value), `RESEND_API_KEY`, `ESTIMATE_NOTIFY_EMAIL`, `NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION`. **Do not set `NEXT_PUBLIC_ASSOCIATE_PORTAL_OPEN`.** Redeploy.
7. Admin enters Grid Nova company/bank details and standard commission rates; connect a staff Google Calendar.

### 10.4 Go-live day
Weekday morning IST, owner + one Grid Nova admin present. Before: CI green on deployed commits, smoke test passed, readiness `ready/at_head`, Admin sign-in on live Clerk, portal switch unset, staff test email received. After: phone on mobile data — calculator result, estimate form, Ask Nova, booking; lead in dashboard + email; one real proposal draft → approve → send to a team address → accept; Sentry clean; scheduler 200s.
**Rollback:** Cloud Run traffic to previous revision (migrations are additive); Vercel → promote previous deployment; data via Cloud SQL point-in-time clone.

### 10.5 Phase 2 (Associate Network)
After Phase 2 inputs: Admin enters rates/policies, update copy (KYC notice, welcome letter, agreement), test on staging with Preview portal switch on, then set `NEXT_PUBLIC_ASSOCIATE_PORTAL_OPEN=true` on Vercel Production and redeploy.

---

## 11. DNS — managed by Tanmoy (owner)

The deployer never needs registrar access. **Process:** the deployer copies the exact record values from each provider's dashboard into a message for Tanmoy → Tanmoy adds them → the deployer verifies (`dig +short <name> <type>`) and presses "Verify" in the provider. Do not remove or change existing records for www/apex (Vercel) without agreement.

| Purpose | Name (host) | Type | Value comes from | When |
|---|---|---|---|---|
| Backend API | `api` | **A** | the static IP from `gcloud compute addresses describe gridnova-api-ip --global` | Production §10, before the certificate |
| Clerk production | `clerk`, `accounts`, `clkmail`, `clk._domainkey`, `clk2._domainkey` (typical set) | CNAME | Clerk Dashboard → production instance → Domains (use exactly what it lists) | Before production sign-in works; start early |
| Resend sending domain | records Resend lists for `gridnova.in` (SPF TXT and MX on a `send` subdomain, DKIM TXT `resend._domainkey`) | TXT / MX | Resend → Domains → gridnova.in | Before any production email; start early |
| DMARC (recommended) | `_dmarc` | TXT | e.g. `v=DMARC1; p=none; rua=mailto:<team address>` (decide with owner) | With Resend |
| Search Console | root | TXT (or the HTML meta tag via `NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION`) | Google Search Console | Any time |
| Website (exists) | `www`, apex | CNAME / A | Vercel → Domains (already configured) | No change |
| Staging (optional) | e.g. `staging`, `api-staging` | CNAME / A | Vercel / load balancer | Only if a staging custom domain is wanted; branch alias + `*.run.app` work without DNS |

Notes: check whether gridnova.in already has SPF/MX records (Google Workspace or another mail provider) before adding — merge, never duplicate SPF. Clerk and Resend verification can take hours; request these records on day one of Stage C. Certificate provisioning needs the A record resolving first.

---

## 12. Glossary
CFA — Central Financial Assistance (PM Surya Ghar subsidy). DCR — domestic content requirement (made-in-India cells/modules). DISCOM — electricity distribution company (WBSEDCL, CESC, NBPDCL, SBPDCL). BOQ — bill of quantities. O&M — operations and maintenance. Associate / Preferred Associate — Grid Nova's referral/sales/execution partners; territory roles = State/District/Block heads. Sirs — Grid Nova's approvers. Gate 1 — staging smoke test that must pass before production. Placeholder — a business value not yet confirmed by Grid Nova, tracked in the registry.
