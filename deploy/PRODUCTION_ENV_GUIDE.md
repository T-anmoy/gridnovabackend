# Grid Nova — Production environment guide (backend + website)

For the engineer deploying the Grid Nova backend to Google Cloud and wiring the live website.
Prepared 29 Oct 2026 against backend `main` (BE-20) and frontend `main` (FE-20).
The step-by-step commands are in the backend repo: **`deploy/RUNBOOK.md`**. This guide tells you **which values go where**.

> **Decision by the owner:** go straight to **production** (no staging). Follow the RUNBOOK with `ENV=production` and skip the "do staging first" note. The §11 smoke test is still mandatory after the first deploy.

---

## 0. Rules

1. **Every secret is new.** No value from anyone's laptop `.env` may be reused (the dev file was shared). Create fresh keys at every provider.
2. **Secrets never go in a file, chat, email or git.** They go straight into Google Secret Manager (backend) or Vercel → Environment Variables → Production (website).
3. `deploy/env.production.yaml` holds only **non-secret** settings and is committed.
4. Region everywhere: **asia-south1 (Mumbai)**. Website on Vercel region **bom1**.

---

## 1. Values you must obtain first (checklist)

| # | What | Where it comes from | Used in |
|---|---|---|---|
| 1 | Google Cloud **Project ID** (billing on) | GCP console | RUNBOOK §0 `PROJECT_ID` |
| 2 | **Cloudflare R2**: account ID, bucket `gridnova-prod` (private), API token (Object Read & Write, this bucket only) | Cloudflare → R2 | `STORAGE_ENDPOINT_URL` (yaml), `gridnova-storage-access-key`, `gridnova-storage-secret-key` |
| 3 | **OpenAI** project key with a monthly spend limit | platform.openai.com → Projects | `gridnova-llm-api-key` |
| 4 | **Resend**: domain `gridnova.in` verified (DNS below), API key with sending access | resend.com | backend `gridnova-resend-api-key`; website `RESEND_API_KEY` (may be a second key) |
| 5 | **Google OAuth client** (Web application) in the same GCP project; consent screen published; redirect URI `https://api.gridnova.in/api/v1/dashboard/employees/me/calendar/callback` | GCP → APIs & Services → Credentials | `GOOGLE_OAUTH_CLIENT_ID` (yaml), `gridnova-google-oauth-client-secret` |
| 6 | **Sentry** project (Python) DSN | sentry.io | `gridnova-sentry-dsn` |
| 7 | **Clerk production instance** on `gridnova.in`: secret key `sk_live_…`, publishable key `pk_live_…`, Frontend API URL `https://clerk.gridnova.in` | Clerk dashboard | backend `gridnova-clerk-secret-key` + `CLERK_JWT_ISSUER` (yaml); website `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` |
| 8 | **Internal API key** (generate) | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | backend `gridnova-internal-api-key` **and** website `INTERNAL_API_KEY` — **identical** |
| 9 | Fernet key (generate) | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | `gridnova-fernet-key` — keep an offline copy readable by two named people (losing it = Google calendar tokens unreadable) |
| 10 | Identity HMAC key (generate) | `python -c "import secrets; print(secrets.token_urlsafe(48))"` | `gridnova-identity-hmac-key` — never rotate casually (associate duplicate checks depend on it) |
| 11 | Database password (generated in RUNBOOK §3) | `openssl rand -hex 24` | inside `gridnova-database-url` only |
| 12 | Google Search Console verification code | Search Console → HTML tag → `content` value | website `NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION` |

Then put #2 (account ID) and #5 (client ID) into `deploy/env.production.yaml`, replacing the two `TODO_` markers. **The app does not detect an unreplaced `TODO_`** — it would boot and then fail on uploads / calendar.

---

## 2. Backend — non-secret settings (`deploy/env.production.yaml`, committed)

Already prepared in the repo. Final values:

```yaml
ENVIRONMENT: "production"
ALLOWED_ORIGINS: '["https://www.gridnova.in","https://gridnova.in"]'
TRUSTED_PROXY_HOPS: "2"                     # behind the HTTPS load balancer (RUNBOOK §10)
FRONTEND_BASE_URL: "https://www.gridnova.in"
PUBLIC_API_BASE_URL: "https://api.gridnova.in"
CLERK_JWT_ISSUER: "https://clerk.gridnova.in"
ASSOCIATE_INVITE_REDIRECT_URL: "https://www.gridnova.in/associates/sign-up"
STAFF_INVITE_REDIRECT_URL: "https://www.gridnova.in/dashboard/sign-up"
CLERK_BAN_ENABLED: "false"                  # BE-21: Clerk free plan — offboarding revokes sessions
BOOTSTRAP_ADMIN_EMAILS: "shouvik@gridnova.in,rp@gridnova.in,tanmoyhalderxyz@gmail.com"
STORAGE_BUCKET: "gridnova-prod"
STORAGE_ENDPOINT_URL: "https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com"      # FILL
NOTIFY_FROM_EMAIL: "Grid Nova <notifications@gridnova.in>"
STAFF_NOTIFY_EMAILS: "sales@gridnova.in,info@gridnova.in"
ACCOUNTS_NOTIFY_EMAILS: "tanmoyhalderxyz@gmail.com"
OPERATIONS_NOTIFY_EMAILS: "info@gridnova.in"
GOOGLE_OAUTH_CLIENT_ID: "<CLIENT_ID>.apps.googleusercontent.com"             # FILL
GOOGLE_OAUTH_REDIRECT_URI: "https://api.gridnova.in/api/v1/dashboard/employees/me/calendar/callback"
LLM_MODEL: "gpt-4o-mini"
STUDIO_AI_FILL_ENABLED: "false"
DB_POOL_SIZE: "10"
DB_MAX_OVERFLOW: "5"
PROPOSAL_VALUE_MANAGER_APPROVAL_THRESHOLD: "500000"
CAPACITY_DIRECTOR_NOTIFY_THRESHOLD_MW: "1.0"
```

Every other setting keeps its built-in default (business hours 10:00–18:00 IST Mon–Sat, agent limits, memory, guardrails). Do **not** set `FORWARDED_ALLOW_IPS`.

## 3. Backend — secrets (Google Secret Manager, RUNBOOK §5)

| Secret name | Env var | Value |
|---|---|---|
| `gridnova-database-url` | `DATABASE_URL` | `postgresql://gridnova:<DB_PASSWORD>@/gridnova?host=/cloudsql/<PROJECT_ID>:asia-south1:gridnova-production` |
| `gridnova-clerk-secret-key` | `CLERK_SECRET_KEY` | Clerk production `sk_live_…` |
| `gridnova-fernet-key` | `FERNET_ENCRYPTION_KEY` | generated (#9) |
| `gridnova-identity-hmac-key` | `IDENTITY_HMAC_KEY` | generated (#10) |
| `gridnova-internal-api-key` | `INTERNAL_API_KEY` | generated (#8) — same value in Vercel |
| `gridnova-llm-api-key` | `LLM_API_KEY` | OpenAI project key |
| `gridnova-storage-access-key` | `STORAGE_ACCESS_KEY` | R2 token access key ID |
| `gridnova-storage-secret-key` | `STORAGE_SECRET_KEY` | R2 token secret |
| `gridnova-resend-api-key` | `RESEND_API_KEY` | Resend key |
| `gridnova-google-oauth-client-secret` | `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth client secret |
| `gridnova-sentry-dsn` | `SENTRY_DSN` | Sentry DSN |
| `gridnova-slack-webhook-url` | `SLACK_WEBHOOK_URL` | optional — if unused, do not create it and **remove it from `$SECRETS`** |

At boot the app **refuses to start** if: the database is not PostgreSQL, the Clerk issuer/secret, Fernet key, identity key or internal key is empty, an origin is localhost or malformed, `TRUSTED_PROXY_HOPS` is 0, the frontend/public URLs are empty or local, or `LLM_API_KEY` is empty. Missing storage, Resend, Google or Sentry only logs a `PRODUCTION CONFIG WARNING` — check the first boot's logs for those.

## 4. Website — Vercel → Project → Settings → Environment Variables → **Production**

| Variable | Value | Secret? |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `https://api.gridnova.in` | no |
| `NEXT_PUBLIC_PRODUCTION_API_HOST` | `api.gridnova.in` | no |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | `pk_live_…` (production instance) | no |
| `CLERK_SECRET_KEY` | `sk_live_…` (same instance as the backend) | **yes** |
| `INTERNAL_API_KEY` | exactly the backend's `gridnova-internal-api-key` | **yes** |
| `RESEND_API_KEY` | Resend key | **yes** |
| `ESTIMATE_NOTIFY_EMAIL` | `info@gridnova.in,sales@gridnova.in` | no |
| `NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION` | Search Console code | no |
| `NEXT_PUBLIC_ASSOCIATE_PORTAL_OPEN` | **leave unset** (portal closed until Grid Nova opens the network) | — |
| `NEXT_PUBLIC_ASK_NOVA_ENABLED` | leave unset (= on) | — |
| `CSP_REPORT_ONLY` | leave unset (emergency switch only) | — |

**Preview** environment: never point at `api.gridnova.in`; use Clerk **test** keys there.
`NEXT_PUBLIC_*` values are baked in at build time: after changing any of them, **Redeploy** the production deployment.

## 5. Clerk production instance (settings, RUNBOOK §8)

**Clerk's free (Hobby) plan is enough** (BE-21). What Grid Nova needs from Clerk is free on
every plan: sign-in, invitations, custom session-token claims, user public metadata, listing
and revoking sessions, and the custom domain. Two features are **Pro only** and are **off**:
two-step sign-in (MFA) and banning a user. The backend does not depend on either — the
database decides who may do what, and a deactivated person is refused on their next request.

1. **Domains:** `gridnova.in` — add the DNS records Clerk lists (section 7).
2. **Sessions → Customize session token** (unchanged):
   ```json
   { "public_metadata": "{{user.public_metadata}}", "email": "{{user.primary_email_address}}", "name": "{{user.full_name}}" }
   ```
3. **Access mode → keep sign-up OPEN (`public`)** — not Invite-only. The app is the gate:
   anyone signed in who was not invited gets 403 `not_invited` and no account in Grid Nova;
   an associate who is not an approved, unclaimed associate with that verified address gets
   403 `claim_not_allowed`. Open sign-up is what lets the fallbacks work when Clerk's
   invitation email does not arrive or cannot be created: the person signs up themselves
   with the invited address (staff: `/dashboard/sign-up`; associates: `/associates/sign-up`,
   then the claim, `docs/api/associate_journeys.md`).
4. **Multi-factor: leave off.** Two-step is off for every role in
   `config/access_control.yaml` (owner's decision, 29 Oct 2026).
5. **First Admins** (after the migration job has run) — `BOOTSTRAP_ADMIN_EMAILS` is
   `shouvik@gridnova.in` (Shouvik Biswas), `rp@gridnova.in` (Raju Paul),
   `tanmoyhalderxyz@gmail.com` (Tanmoy Halder). For each of the three:
   1. They **sign up** at `https://www.gridnova.in/dashboard/sign-up` with that exact address
      and verify it (Clerk's email code).
   2. In the Clerk Dashboard → **Users** → that user → **Metadata** → **Public**, set
      `{"role": "admin"}` → **Save**.
   3. They **sign out and sign in again** (so the session token carries it), then open the
      dashboard. The first API call creates them as Admin (audited `bootstrap_admin`).
   Only a **verified** address on the list works; anything else is `not_invited`. The other
   way in, `scripts/grant_admin.py` as the migration job (RUNBOOK §8 step 4), still works.
   Everyone else is invited from the dashboard (Staff → Invite).

**Switching on two-step and ban after buying Clerk Pro:**
- Ban: set `CLERK_BAN_ENABLED: "true"` in `deploy/env.production.yaml` and redeploy.
  Deactivation then bans (and reactivation unbans) instead of revoking sessions.
- Two-step: Clerk Dashboard → Multi-factor → enable Authenticator app (TOTP) + Backup codes;
  leave "Require MFA" off (it would force it on associates). Have each Admin, Management,
  Network Manager and Accounts person enable it in their account first, **then** set
  `requires_two_factor: true` on those roles in `config/access_control.yaml` and deploy.
  Sensitive actions then need a session that completed the second factor.

## 6. Google Cloud resources (summary of RUNBOOK §1–§10)

| Resource | Setting |
|---|---|
| Cloud Run `gridnova-api` | public via load balancer only (`--ingress=internal-and-cloud-load-balancing` after §10), 1 vCPU / 2 GiB, concurrency 20, max 4 instances |
| Cloud Run `gridnova-jobs` | private (`--no-allow-unauthenticated`), min 0 / max 2, timeout 600 s |
| Cloud Run job `gridnova-migrate` | runs `bootstrap_db.py && seed_territories.py` — **before** every deploy (schema head: `0058_direct_customer_payments`) |
| Cloud SQL | PostgreSQL 16, asia-south1, PITR on, deletion protection on |
| Load balancer | global external HTTPS LB + serverless NEG + managed cert for `api.gridnova.in` |
| Cloud Scheduler | 9 jobs from `deploy/scheduler.yaml` via `deploy/scheduler_commands.py`, OIDC to `gridnova-jobs` + `X-Internal-Key`. Includes the new **`gridnova-check-studio-reminders`** (daily 09:00 IST) |

**Cost choice for launch (owner wants a lean start):**
- Cloud SQL: start with **`--tier=db-custom-1-3840 --availability-type=ZONAL`** (the RUNBOOK's staging size) with backups + PITR; move to the HA 2-vCPU production size when traffic justifies it. HA roughly doubles the database cost.
- `gridnova-api`: `--min-instances=1` avoids cold starts on chat and forms (always billed); `--min-instances=0` saves that cost but the first visitor after idle waits several seconds. Recommended: keep 1.
- Check the monthly estimate in the Google Cloud pricing calculator before creating resources.

## 7. DNS records (the owner manages gridnova.in DNS)

| Record | Type | Value | For |
|---|---|---|---|
| `api.gridnova.in` | A | the reserved global IP (RUNBOOK §10) | backend |
| Clerk records (`clerk`, `accounts`, `clkmail`, `clk._domainkey`, `clk2._domainkey`) | CNAME | exactly as Clerk → Domains lists them | sign-in |
| Resend records (SPF TXT, DKIM `resend._domainkey`, MX on the send subdomain) | as Resend lists | as Resend lists | email |
| Search Console | TXT or the HTML tag above | — | SEO |
| `www` / apex | as already set for Vercel | — | website (unchanged) |

Wait until the managed certificate for `api.gridnova.in` is **ACTIVE** before switching the website to the backend.

## 8. Go-live order

1. Accounts and keys (section 1) → fill the two `TODO_` values → commit.
2. RUNBOOK §1–§5 (APIs, registry, Cloud SQL, service accounts, secrets).
3. Build the image (§2) → create and **run the migration job** (§7) → deploy `gridnova-api` and `gridnova-jobs` (§6).
4. Load balancer + `api.gridnova.in` A record + ingress restriction (§10) → wait for the cert.
5. Cloud Scheduler (§9).
6. Clerk settings (section 5) → grant the three Admins.
7. Vercel Production variables (section 4) → **Redeploy** the website.
8. Smoke test (RUNBOOK §11) **plus** the checks below. Record every result.

### Extra live checks for what was built since the RUNBOOK (BE-17 → BE-20)
- **Admin sign-in with two-step**; open Employees; invite a test staff member as Sales → the email arrives → they land on `/dashboard/sign-up` and see only Sales pages.
- **Deactivate that test person** → they are signed out at once and cannot sign in (Clerk ban); reactivate works.
- **Proposal Studio** (as a Proposal Desk test user): create a 3 kWp residential proposal → preview → issue → download the PDF → open the customer link `/proposal/<token>` on a phone → scan the last-page QR → `/verify/p/<code>` shows "genuine" → accept with the last 4 digits of the mobile.
- **Direct payment** on that converted lead: Accounts person A records ₹1 test advance (NEFT) → person B verifies → the receipt shows "held" (expected until the receipt wording, P6, is supplied).
- **Scheduler:** `gcloud scheduler jobs run gridnova-check-studio-reminders --location=asia-south1` → 200 in the logs.
- **Hindi/Bengali PDF fonts:** a proposal for a customer with a Bengali name prints the name, not boxes.
- **Associate portal is closed**: `/careers` shows "opening soon" with no Apply / Sign-in buttons.
- Delete or mark lost the test records afterwards.

## 9. Not part of this deploy (content still owed — the app runs safely without it)

These are placeholders the app knows about (Dashboard → Data checklist). They block **specific features**, not the deploy:
- **P6** receipt wording → until supplied, receipts are numbered but held (not emailed).
- **PRO-7/8/10/11/12** scope text → ground-mounted, government, PM-KUSUM and generic proposals can be drafted but not issued. **STU-6/7/8/10** → street-lighting quotes the same.
- **ONB-5, C5-RATES, C5-8, C5-11** → associate network (portal stays closed).
- **HW-OM** → O&M terms after year one on handover documents.
