# Grid Nova AI Operations Platform — Phase 1

AI-powered lead qualification and proposal management for Grid Nova Energy.

## Quick start (local development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
uvicorn app.main:app --reload
```

Open `http://localhost:8000/api/v1/health` to confirm the server is running.

## Running tests

```bash
# From the project root (grid-nova-ai-backend/)
python -m pytest tests/ -v
```

No external services required — tests use an in-memory SQLite database and
LLM mock mode activates automatically when `LLM_API_KEY` is unset.

## Environment variables required before deployment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (Cloud SQL for PostgreSQL, Mumbai `asia-south1`, HA) |
| `CLERK_SECRET_KEY` | Clerk secret key for JWT verification |
| `CLERK_JWT_ISSUER` | Your Clerk frontend API URL (e.g. `https://clerk.your-domain.com`) |
| `STORAGE_BUCKET` | Cloudflare R2 bucket name |
| `STORAGE_ENDPOINT_URL` | R2 S3-compatible endpoint |
| `STORAGE_ACCESS_KEY` | R2 access key ID |
| `STORAGE_SECRET_KEY` | R2 secret access key |
| `LLM_API_KEY` | OpenAI API key |
| `SENTRY_DSN` | Sentry DSN for error tracking |
| `ALLOWED_ORIGINS` | JSON array of allowed CORS origins |

Optional / defaulted:
- `LLM_BASE_URL` — defaults to `https://api.openai.com/v1`
- `LLM_MODEL` — defaults to `gpt-4o-mini`
- `PROPOSAL_VALUE_MANAGER_APPROVAL_THRESHOLD` — defaults to `500000` (INR)
- `CAPACITY_DIRECTOR_NOTIFY_THRESHOLD_MW` — defaults to `1.0` MW
- `AI_ESTIMATED_COST_PER_CALL` — defaults to `0.01`; used to derive
  `ai_estimated_cost` from the number of AI agent replies in `/analytics`

Meeting scheduling (Google Calendar) — scheduling stays inert until these are set:
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`
- `FERNET_ENCRYPTION_KEY` — encrypts employee Google refresh tokens at rest. Generate with
  `python -c "from app.security.encryption import generate_key; print(generate_key())"`
- `BUSINESS_HOURS_START_IST` (10), `BUSINESS_HOURS_END_IST` (18), `BUSINESS_DAYS` (`[0,1,2,3,4,5]`,
  Mon–Sat), `BUSINESS_TIMEZONE` (`Asia/Kolkata`), `SCHEDULING_DAYS_AHEAD` (5),
  `SCHEDULING_DEFAULT_DURATION_MINUTES` (30)

## Parameterized proposal engine

Proposal content is composed by `app/proposals/content_engine.py` entirely from
`config/proposal_content.yaml` — Grid Nova staff edit copy, pricing tiers, and matching
rules without touching code. Composable `parameter_rules` (all matching rules contribute
their sections) select the scope/notes; capacity-tiered `pricing` + `segment_modifiers`
compute the value. The config is **validated at startup** (overlapping tiers or a rule
referencing a missing section id refuse to boot). An unmatched combination falls back to a
generic scope and sets `used_fallback_template = true` on the proposal so the dashboard
flags it for manual review before sending.

## Meeting scheduling (Google Calendar)

Availability is a deterministic free/busy query across Sales/Admin employees who have
connected their calendars — never an LLM guess. Booking creates a real Google Meet event
and emails both parties (`sendUpdates="all"`), after re-verifying the slot is still free.

```
GET  /api/v1/dashboard/employees/me/calendar/connect   start Google OAuth (any authed employee)
GET  /api/v1/dashboard/employees/me/calendar/callback  OAuth callback — stores encrypted refresh token
POST /api/v1/dashboard/leads/{id}/schedule-meeting     employee-initiated booking (Sales/Admin)
```

The Sales Agent also offers real slots mid-conversation and auto-books once the customer
confirms one (the confirmed slot is always one it actually offered — re-verified free
before the event is created). Only Sales/Admin connected calendars receive bookings; with
zero connected calendars, scheduling fails with a clear error rather than doing nothing.

## Dashboard read/list endpoints

These back the internal dashboard. All require Clerk auth and enforce the same
role matrix as the action endpoints — **filtering happens in the query, not the
response**, so no role ever receives rows it cannot see.

```
GET  /api/v1/dashboard/proposals            list (status, date_from, date_to, lead_id, page, page_size)
GET  /api/v1/dashboard/proposals/{id}       detail + all versions for the lead
GET  /api/v1/dashboard/analytics            pipeline + AI-usage metrics (date_from, date_to, employee_id)
GET  /api/v1/dashboard/employees            Admin only
POST /api/v1/dashboard/employees/{id}/role  Admin only — syncs Clerk public_metadata.role
GET  /api/v1/dashboard/audit-log            Admin full, Management read-only
```

Role scoping: Sales is scoped to its own leads/proposals and its own analytics;
Management is read-only; Engineers get a technical-fields-only projection on
proposals and are blocked from analytics, employees, and the audit log.

## Architecture

```
Customer → POST /api/v1/chat/message
              └─ Orchestrator (LangGraph StateGraph)
                    └─ Sales Agent (field extraction + reply)
                          └─ MemoryManager (single DB gateway)

Employee → POST /api/v1/dashboard/proposals/draft
              └─ Orchestrator → gate (readiness check) → Proposal Agent
                                                              └─ MemoryManager

POST /api/v1/dashboard/proposals/{id}/approve          (Sales/Admin, standard)
POST /api/v1/dashboard/proposals/{id}/manager-sign-off (Management/Admin, high-value)
POST /api/v1/dashboard/proposals/{id}/send             (Sales/Admin, after approval)
```

## Non-negotiable principles

1. One public AI face — only Sales Agent ever speaks to customers.
2. Only the Orchestrator calls an agent — agents never import each other.
3. One shared memory — all DB writes go through `MemoryManager`.
4. Every commercially significant action requires a human — no code path sends
   a proposal without an authenticated employee explicitly triggering it.
5. The AI drafts. It never decides — lead scoring, approval thresholds, and
   all business-critical judgments are deterministic functions, never LLM prompts.

## Deployment (Google Cloud Run + Cloud SQL)

1. Provision a Cloud SQL for PostgreSQL instance in Mumbai (`asia-south1`), HA
   (regional) configuration.
2. Build and deploy the container to Cloud Run (it listens on the `$PORT` Cloud Run
   injects; the `Dockerfile` falls back to `8000` locally).
3. Connect Cloud Run to the Cloud SQL instance and set `DATABASE_URL` accordingly.
4. Set all environment variables listed above, and `ENVIRONMENT=production`.

Run Alembic migrations after the first deploy:
```bash
alembic -c alembic/alembic.ini upgrade head
```
(`alembic.ini` lives at `alembic/alembic.ini`, not the project root — the bare
`alembic upgrade head` command fails immediately with "No config file 'alembic.ini'
found" unless `-c` points at it, or the command is run from inside `alembic/`.)

### Deploying to a fresh database

This repo has historically relied on `Base.metadata.create_all()` (run automatically
by the app's startup lifespan in `app/main.py`) rather than a true baseline migration —
see the note at the top of `alembic/versions/0001_scheduling_and_fallback.py`. Every
migration from `0001` onward is an incremental `ALTER` that assumes the tables it's
touching already exist, so running `alembic upgrade head` against a genuinely empty
database fails (`NoSuchTableError`), and running it against a database `create_all()`
already brought fully up to date fails a different way (`CircularDependencyError` on
SQLite, "column already exists" on Postgres) — the columns those migrations try to add
are already there.

The correct, current workflow against a brand-new database is therefore:

1. Start the app once (`uvicorn app.main:app`, or just let Cloud Run's first boot do
   it) — the startup lifespan calls `Base.metadata.create_all()` and builds the full
   current schema.
2. Mark migration history as caught up, **without** replaying any migrations:
   ```bash
   alembic -c alembic/alembic.ini stamp head
   ```
3. From then on, `alembic -c alembic/alembic.ini upgrade head` works normally for any
   *new* migration added after this point, since the database will already be at
   `head` in Alembic's bookkeeping.

Writing a real `0000` baseline migration (so `upgrade head` alone works end-to-end
against a genuinely empty database, the way it should) is a known follow-up — tracked,
intentionally not done in this change.
