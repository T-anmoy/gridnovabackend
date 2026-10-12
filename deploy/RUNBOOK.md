# Grid Nova backend — deployment runbook

From an **empty GCP project** to **staging**, then to **production**: Cloud Run + Cloud SQL
for PostgreSQL in **asia-south1 (Mumbai)**. Every command is meant to be copied as-is
after the variables in §0 are set. `<ANGLE_BRACKETS>` are values you must supply.

Staging and production are **separate GCP projects**, so every resource and secret name
below is the same in both; only §0 differs. Do staging end to end (including §11, the
smoke test = Gate 1) before touching production.

Contents: 0 Variables · 1 APIs · 2 Artifact Registry & build · 3 Cloud SQL (+ connection
budget) · 4 Service accounts · 5 Secret Manager (+ rotation) · 6 Deploy (Cloud Run flags,
background work, probes) · 7 Migration job · 8 Clerk session token (D8) · 9 Scheduler ·
10 Custom domain & CORS · 11 Smoke test (Gate 1) · 12 Rollback · 13 Backups & data safety ·
14 Cost notes · 15 Rate limits, concurrency, scaling

---

## 0. Variables (set these in every shell you use)

```bash
export ENV=staging                          # staging | production
export PROJECT_ID=<GCP_PROJECT_ID>          # e.g. gridnova-staging / gridnova-prod
export REGION=asia-south1
export SQL_INSTANCE=gridnova-$ENV
export SQL_CONN=$PROJECT_ID:$REGION:$SQL_INSTANCE
export REPO=gridnova
export TAG=$(git rev-parse --short HEAD)    # the commit being deployed
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/backend:$TAG
export RUN_SA=gridnova-run@$PROJECT_ID.iam.gserviceaccount.com
export SCHEDULER_SA=gridnova-scheduler@$PROJECT_ID.iam.gserviceaccount.com
gcloud config set project $PROJECT_ID
```

You need `gcloud` (authenticated as a project Owner for the first setup), `git`, Python 3.12
with `PyYAML` (for `deploy/scheduler_commands.py`), `curl` and `openssl`.

## 1. APIs

```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  iam.googleapis.com compute.googleapis.com logging.googleapis.com
```

## 2. Artifact Registry and the image

```bash
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION \
  --description="Grid Nova backend images"

# Build in Cloud Build (linux/amd64, the hash-pinned requirements.lock, fonts included):
gcloud builds submit --region=$REGION --tag=$IMAGE .
```

The image is the same for the API service, the jobs service and the migration job. It
runs as a non-root user and contains no tests, `.env` or `*.db` (`.dockerignore`;
`tests/test_dockerignore.py`). To check its fonts before deploying:
`docker run --rm $IMAGE python scripts/smoke_fonts.py` (CI does this on every PR).

## 3. Cloud SQL for PostgreSQL

Staging (single zone, small):

```bash
gcloud sql instances create $SQL_INSTANCE --database-version=POSTGRES_16 --region=$REGION \
  --edition=ENTERPRISE --tier=db-custom-1-3840 --availability-type=ZONAL \
  --storage-type=SSD --storage-size=10 --storage-auto-increase \
  --backup-start-time=20:30 --enable-point-in-time-recovery \
  --retained-backups-count=7 --retained-transaction-log-days=7 \
  --database-flags=max_connections=200 --deletion-protection
```

Production — the same, but **HA (regional)**, larger, longer retention and a maintenance window:

```bash
gcloud sql instances create $SQL_INSTANCE --database-version=POSTGRES_16 --region=$REGION \
  --edition=ENTERPRISE --tier=db-custom-2-7680 --availability-type=REGIONAL \
  --storage-type=SSD --storage-size=20 --storage-auto-increase \
  --backup-start-time=20:30 --enable-point-in-time-recovery \
  --retained-backups-count=30 --retained-transaction-log-days=7 \
  --maintenance-window-day=SUN --maintenance-window-hour=21 \
  --database-flags=max_connections=200 --deletion-protection
```

(`--backup-start-time` and the maintenance window are UTC: 20:30 UTC = 02:00 IST,
Sunday 21:00 UTC = Monday 02:30 IST.) Tiers are starting points: watch CPU and memory
in the first weeks.

Database and user — the password goes straight into the `DATABASE_URL` secret (§5):

```bash
gcloud sql databases create gridnova --instance=$SQL_INSTANCE
export DB_PASSWORD=$(openssl rand -hex 24)        # hex: safe inside a URL
gcloud sql users create gridnova --instance=$SQL_INSTANCE --password="$DB_PASSWORD"
```

### Connection budget

Each instance holds at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections (10 + 5 = 15,
`app/memory/db.py`). The budget must hold:

    15 × (API --max-instances + jobs --max-instances) + migration job (≈2) + admin (≈5)  <  max_connections

With §6's flags: 15 × (4 + 2) + 2 + 5 = 97 < 200. Raising `--max-instances` means
raising `max_connections` (`gcloud sql instances patch $SQL_INSTANCE
--database-flags=max_connections=<N>` — this restarts the instance) or lowering the pool.

`DATABASE_URL` always uses the **Cloud SQL Unix socket** that `--set-cloudsql-instances`
mounts — never a TCP address, and never the `?gssencmode=disable&sslmode=disable` some
macOS laptops need for local tests (the production guard refuses `sslmode=disable` over
TCP).

## 4. Service accounts

```bash
gcloud iam service-accounts create gridnova-run --display-name="Grid Nova API, jobs and migrations"
gcloud iam service-accounts create gridnova-scheduler --display-name="Grid Nova Cloud Scheduler caller"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:$RUN_SA --role=roles/cloudsql.client
# Whoever deploys must be able to act as the runtime account:
gcloud iam service-accounts add-iam-policy-binding $RUN_SA \
  --member=user:<DEPLOYER_EMAIL> --role=roles/iam.serviceAccountUser
```

The runtime account gets read access to each secret individually in §5 — no
project-wide Secret Manager role. The scheduler account gets `roles/run.invoker` on the
jobs service only, in §9.

## 5. Secret Manager

**Every staging and production secret is new.** The development `.env` was shared
outside the team: no value from it — Clerk keys, the Fernet key, R2 keys, the OpenAI key,
Resend, Google OAuth — may be reused. Create fresh credentials at each provider for each
environment.

| Secret name | Env var | How to make it |
|---|---|---|
| `gridnova-database-url` | `DATABASE_URL` | `postgresql://gridnova:$DB_PASSWORD@/gridnova?host=/cloudsql/$SQL_CONN` |
| `gridnova-clerk-secret-key` | `CLERK_SECRET_KEY` | Clerk Dashboard → the environment's instance → API keys |
| `gridnova-fernet-key` | `FERNET_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `gridnova-identity-hmac-key` | `IDENTITY_HMAC_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `gridnova-internal-api-key` | `INTERNAL_API_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `gridnova-llm-api-key` | `LLM_API_KEY` | OpenAI → a new project key for this environment |
| `gridnova-storage-access-key` | `STORAGE_ACCESS_KEY` | Cloudflare R2 → API token scoped to this bucket (Object Read & Write) |
| `gridnova-storage-secret-key` | `STORAGE_SECRET_KEY` | (same token) |
| `gridnova-resend-api-key` | `RESEND_API_KEY` | Resend → API keys (sending access, the verified domain) |
| `gridnova-google-oauth-client-secret` | `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud console → OAuth client for this environment |
| `gridnova-sentry-dsn` | `SENTRY_DSN` | Sentry → project for this environment → Client Keys |
| `gridnova-slack-webhook-url` | `SLACK_WEBHOOK_URL` | (optional) Slack → incoming webhook |

Create each one (the value is read from stdin and never lands in shell history), then let
the runtime account read it:

```bash
create_secret() {   # usage: create_secret <name>   then paste the value, Enter, Ctrl-D
  gcloud secrets create "$1" --replication-policy=user-managed --locations=$REGION --data-file=-
  gcloud secrets add-iam-policy-binding "$1" --member=serviceAccount:$RUN_SA \
    --role=roles/secretmanager.secretAccessor
}
printf '%s' "postgresql://gridnova:$DB_PASSWORD@/gridnova?host=/cloudsql/$SQL_CONN" | \
  gcloud secrets create gridnova-database-url --replication-policy=user-managed --locations=$REGION --data-file=-
gcloud secrets add-iam-policy-binding gridnova-database-url --member=serviceAccount:$RUN_SA \
  --role=roles/secretmanager.secretAccessor
unset DB_PASSWORD
create_secret gridnova-clerk-secret-key
create_secret gridnova-fernet-key
create_secret gridnova-identity-hmac-key
create_secret gridnova-internal-api-key
create_secret gridnova-llm-api-key
create_secret gridnova-storage-access-key
create_secret gridnova-storage-secret-key
create_secret gridnova-resend-api-key
create_secret gridnova-google-oauth-client-secret
create_secret gridnova-sentry-dsn
create_secret gridnova-slack-webhook-url          # skip if Slack is not used, and drop it from $SECRETS
```

The mapping every Cloud Run resource uses:

```bash
export SECRETS="DATABASE_URL=gridnova-database-url:latest,CLERK_SECRET_KEY=gridnova-clerk-secret-key:latest,\
FERNET_ENCRYPTION_KEY=gridnova-fernet-key:latest,IDENTITY_HMAC_KEY=gridnova-identity-hmac-key:latest,\
INTERNAL_API_KEY=gridnova-internal-api-key:latest,LLM_API_KEY=gridnova-llm-api-key:latest,\
STORAGE_ACCESS_KEY=gridnova-storage-access-key:latest,STORAGE_SECRET_KEY=gridnova-storage-secret-key:latest,\
RESEND_API_KEY=gridnova-resend-api-key:latest,GOOGLE_OAUTH_CLIENT_SECRET=gridnova-google-oauth-client-secret:latest,\
SENTRY_DSN=gridnova-sentry-dsn:latest,SLACK_WEBHOOK_URL=gridnova-slack-webhook-url:latest"
```

`:latest` is resolved when an instance starts: a new secret version reaches the
service on its next deploy (or `gcloud run services update gridnova-api --region=$REGION`).

### 5.1 The Fernet key: storage and backup

`FERNET_ENCRYPTION_KEY` encrypts employees' Google refresh tokens and every associate's
bank account number. **Losing it makes those values unreadable for good.** It lives in
Secret Manager (all versions are kept until someone destroys them — never destroy a
version still in use). Also keep an **offline copy** of every version in the company
password manager, readable by two named people. Grant `roles/secretmanager.admin` to as
few people as possible.

### 5.2 The identity-claim key

`IDENTITY_HMAC_KEY` signs the associate duplicate-registration claims
(`app/associates/service.py`). Before BE-3 that key was derived from the Fernet key, so a
Fernet rotation would have silently broken duplicate detection. It is now its own secret,
required in staging and production. A **fresh** database needs nothing more. A database
that already holds claims signed the old way (any copy of the development database)
must be re-signed once, after the new key is in place:

```bash
gcloud run jobs execute gridnova-migrate --region=$REGION --wait \
  --args=-c,"python scripts/rehash_identity_claims.py --apply"
```

(Run it without `--apply` first for a dry-run count.) The same command is needed if
`IDENTITY_HMAC_KEY` itself is ever changed.

### 5.3 Rotating `FERNET_ENCRYPTION_KEY` (MultiFernet)

The variable takes a comma-separated list, newest first: the first key encrypts, every
key decrypts (`app/security/encryption.py`).

```bash
NEW=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
OLD=$(gcloud secrets versions access latest --secret=gridnova-fernet-key)
printf '%s' "$NEW,$OLD" | gcloud secrets versions add gridnova-fernet-key --data-file=-
# 1. deploy (§6) so every instance has both keys
# 2. re-encrypt every stored secret with the new key:
gcloud run jobs execute gridnova-migrate --region=$REGION --wait --args=-c,"python scripts/rotate_fernet.py --apply"
# 3. drop the old key, deploy again:
printf '%s' "$NEW" | gcloud secrets versions add gridnova-fernet-key --data-file=-
unset NEW OLD
```

Store the new key offline (§5.1) before step 3. The identity claims are unaffected.

## 6. Deploy the services

Copy `deploy/env.template.yaml` to `deploy/env.$ENV.yaml` and fill it in (no secrets in
it). Two services run the same image:

- **`gridnova-api`** — public: the website, the dashboard and the associate portal.
- **`gridnova-jobs`** — private (`--no-allow-unauthenticated`): only Cloud Scheduler calls
  it, with an OIDC token (§9). It serves the same routes; Cloud Run itself rejects any
  request without a valid token.

First deploy of each (a new service cannot be created with `--no-traffic`):

```bash
gcloud run deploy gridnova-api --image=$IMAGE --region=$REGION --service-account=$RUN_SA \
  --set-cloudsql-instances=$SQL_CONN --env-vars-file=deploy/env.$ENV.yaml --set-secrets="$SECRETS" \
  --allow-unauthenticated --ingress=all \
  --concurrency=20 --min-instances=1 --max-instances=4 --cpu=1 --memory=2Gi \
  --no-cpu-throttling --timeout=300 --execution-environment=gen2

gcloud run deploy gridnova-jobs --image=$IMAGE --region=$REGION --service-account=$RUN_SA \
  --set-cloudsql-instances=$SQL_CONN --env-vars-file=deploy/env.$ENV.yaml --set-secrets="$SECRETS" \
  --no-allow-unauthenticated --ingress=all \
  --concurrency=5 --min-instances=0 --max-instances=2 --cpu=1 --memory=2Gi \
  --no-cpu-throttling --timeout=600 --execution-environment=gen2
```

**But run the migration job (§7) before the very first deploy**: in staging and
production the app never creates tables, and `/api/v1/health/ready` stays 503 until the
schema is at head and the territories are loaded.

Every later deploy — job first, then the revision without traffic, gate on readiness,
then shift traffic:

```bash
gcloud builds submit --region=$REGION --tag=$IMAGE .
gcloud run jobs update gridnova-migrate --image=$IMAGE --region=$REGION
gcloud run jobs execute gridnova-migrate --region=$REGION --wait          # stop here if it fails

gcloud run deploy gridnova-api --image=$IMAGE --region=$REGION --no-traffic --tag=candidate
CANDIDATE=$(gcloud run services describe gridnova-api --region=$REGION --format=json | \
  python3 -c "import json,sys; print(next(t['url'] for t in json.load(sys.stdin)['status']['traffic'] if t.get('tag')=='candidate'))")
curl -fsS "$CANDIDATE/api/v1/health/ready" && echo                        # must be 200 "ready"
gcloud run services update-traffic gridnova-api --region=$REGION --to-latest

gcloud run deploy gridnova-jobs --image=$IMAGE --region=$REGION
```

(`gcloud run deploy` with only `--image` keeps every other flag of the service.) On
production, where `gridnova-api` only accepts traffic from the load balancer (§10), run
the readiness check through the jobs service instead:
`curl -fsS -H "Authorization: Bearer $(gcloud auth print-identity-token)" <JOBS_URL>/api/v1/health/ready`
(the migration has already run, so both services see the same database).

### 6.1 Why these flags

| Flag | Value | Why |
|---|---|---|
| one uvicorn process | (Dockerfile CMD, `--workers 1`) | Cloud Run scales by instances; one process keeps the per-instance pool and rate limiters simple |
| `--concurrency` | 20 (jobs: 5) | Handlers are synchronous and run in a 40-thread pool; PDF rendering is CPU-bound. 20 keeps latency predictable; raise after load testing |
| `--max-instances` | 4 (jobs: 2) | Caps the Cloud SQL connection budget (§3) |
| `--min-instances` | 1 (jobs: 0) | Background work and no cold start for the first visitor (§14 cost) |
| `--no-cpu-throttling` | on | CPU stays allocated after the response, so emails, receipts, PDFs, ID cards and reflections queued on the background runner (`app/jobs/runner.py`) actually run. With throttling they stall until the next request |
| `--memory` | 2Gi | WeasyPrint with the Noto fonts; measure and lower if it stays far below |
| `--timeout` | 300 (jobs: 600) | Public requests are short; the jobs may make up to `STALE_LEAD_REFLECTIONS_PER_RUN` model calls |
| graceful shutdown | `GRACEFUL_SHUTDOWN_SECONDS` = 8 (Dockerfile) | Cloud Run sends SIGTERM and kills 10 s later; uvicorn (PID 1 through `exec`) finishes in-flight requests, then the lifespan drains the background runner |
| `--proxy-headers` | on, `FORWARDED_ALLOW_IPS` left at its default | Nothing in the app reads its own scheme or host. Never set `FORWARDED_ALLOW_IPS='*'` (uvicorn would trust the visitor-controlled leftmost X-Forwarded-For entry) |
| `TRUSTED_PROXY_HOPS` | 1 on `*.run.app`, 2 behind the load balancer | How many trusted proxies append to X-Forwarded-For; the rate limiters read the visitor from the right (`app/security/client_ip.py`) |

### 6.2 Background work (Cloud Tasks later)

Work that used to hold the response — Resend/Slack calls after a commit, receipt render +
upload + email, the associate's two PDFs and Clerk invitation — runs on a bounded
in-process thread pool (`BACKGROUND_WORKERS`, default 4). It survives a normal deploy
(the pool is drained on SIGTERM) but not a crash; the scheduled jobs sweep what was lost:
proposals approved more than `SWEEPER_GRACE_MINUTES` ago without a PDF
(`check-stale-leads`) and receipts still `pending` (`check-payment-alerts`). When this
needs per-task retries and durability, move each `runner.submit` call to a Cloud Tasks
queue that calls an internal endpoint — the call sites are already isolated behind
`app/jobs/runner.py`.

### 6.3 Probes

`GET /api/v1/health` is liveness (never touches the database). `GET /api/v1/health/ready`
is the **deploy gate** (database reachable, schema at head, territories loaded; cached
15 s): §6 checks it on the tagged candidate revision before any traffic moves. It is
deliberately **not** a probe: as a liveness probe a database blip would restart every
instance, and as a startup probe a rollback would fail — after a migration the previous
revision reports `unknown_revision` and its new instances could never start. Both probes
use `/api/v1/health`. Set them once per service:

```bash
gcloud run services describe gridnova-api --region=$REGION --format=export > /tmp/gridnova-api.yaml
```

Under `spec.template.spec.containers[0]` add, then apply with
`gcloud run services replace /tmp/gridnova-api.yaml --region=$REGION`:

```yaml
        startupProbe:
          httpGet: {path: /api/v1/health}
          initialDelaySeconds: 2
          periodSeconds: 3
          timeoutSeconds: 2
          failureThreshold: 20
        livenessProbe:
          httpGet: {path: /api/v1/health}
          periodSeconds: 30
          timeoutSeconds: 3
          failureThreshold: 3
```

(Repeat for `gridnova-jobs`.) Probes are kept by later `gcloud run deploy --image` calls.

## 7. The migration job

```bash
gcloud run jobs create gridnova-migrate --image=$IMAGE --region=$REGION --service-account=$RUN_SA \
  --set-cloudsql-instances=$SQL_CONN --env-vars-file=deploy/env.$ENV.yaml --set-secrets="$SECRETS" \
  --tasks=1 --max-retries=0 --task-timeout=1800s --cpu=1 --memory=1Gi \
  --command=sh --args=-c,"python scripts/bootstrap_db.py && python scripts/seed_territories.py"

gcloud run jobs execute gridnova-migrate --region=$REGION --wait
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="gridnova-migrate"' \
  --limit=30 --format='value(textPayload)' --freshness=1h
```

- Empty database: `bootstrap_db: empty database — creating … tables`, then `stamping
  alembic_version at head`; `seed_territories: inserted …`.
- Every later run: `existing schema at <rev> — running alembic upgrade head` (a no-op at
  head); `seed_territories: inserted 0, updated …`.
- Two executions at once serialise on a PostgreSQL advisory lock (`bootstrap_db: waiting
  for the migration lock`).
- Exit code 2 (`REFUSED`) means the database has tables but no `alembic_version` (or the
  reverse): nothing was changed — inspect it by hand. Never "fix" it by stamping a guess.
- `0047_be3_indexes` builds its indexes with `CREATE INDEX CONCURRENTLY` (tables stay
  writable). If that run is interrupted, drop the INVALID index it leaves
  (`DROP INDEX CONCURRENTLY <name>;`) and run the job again.

## 8. Clerk (D8) — the session token must carry the role

The backend reads the caller's role from the **session JWT**: `public_metadata.role`
(staff), `public_metadata.principal` and `public_metadata.associate_id` (associates), plus
`email` and `name` (`app/auth/dependencies.py`). The frontend reads the role from
`user.publicMetadata`, which works without any Clerk change — so on a fresh Clerk instance
the dashboard menus look right while **every backend call answers 403
`session_token_missing_claims`**. For each Clerk instance (staging, production):

1. Clerk Dashboard → the instance → **Sessions** → **Customize session token** → Claims:
   ```json
   {
     "public_metadata": "{{user.public_metadata}}",
     "email": "{{user.primary_email_address}}",
     "name": "{{user.full_name}}"
   }
   ```
   → **Save**. (Clerk caps custom claims at about 1.2 KB, because the token lives in a
   cookie: keep `public_metadata` small — role, principal, associate id.)
2. Users → the first administrator → **Metadata** → Public: `{"role": "admin"}`. Every
   other staff role is set from the dashboard after that; associates get
   `{"principal": "associate", "associate_id": "GNA-…"}` from their invitation.
3. Copy the instance's **Frontend API URL** (`https://clerk.<domain>` in production) into
   `CLERK_JWT_ISSUER`, and its secret key into `gridnova-clerk-secret-key`.
4. `ALLOWED_ORIGINS` (§10) must contain every origin the frontend is served from: Clerk
   puts that origin in the token's `azp` claim, and a token whose `azp` is not listed is
   refused (401).

The app logs a reminder of step 1 at every staging/production start. Check it with the
smoke test (§11, step 4).

## 9. Cloud Scheduler (OIDC + `X-Internal-Key`)

The scheduled jobs are in `deploy/scheduler.yaml` (cron in IST, deadlines, retries).
Both checks apply:

- **OIDC → Cloud Run invoker.** Scheduler signs each call as `gridnova-scheduler`; the
  private `gridnova-jobs` service rejects anything else at Cloud Run's front door.
- **`X-Internal-Key`** (`INTERNAL_API_KEY`) is checked by the app on every internal route.
  The public `gridnova-api` serves the same routes, protected by that key alone — do not
  point Scheduler at it.

```bash
gcloud run services add-iam-policy-binding gridnova-jobs --region=$REGION \
  --member=serviceAccount:$SCHEDULER_SA --role=roles/run.invoker
JOBS_URL=$(gcloud run services describe gridnova-jobs --region=$REGION --format='value(status.url)')
python deploy/scheduler_commands.py --project=$PROJECT_ID --region=$REGION \
  --service-url=$JOBS_URL --scheduler-sa=$SCHEDULER_SA > /tmp/scheduler.sh
less /tmp/scheduler.sh && sh /tmp/scheduler.sh          # review, then create the jobs
gcloud scheduler jobs run gridnova-check-stale-leads --location=$REGION   # try one now
```

(After a schedule change in `deploy/scheduler.yaml`: rerun with `--update`.) The header
value is stored in the Scheduler job definition, so keep `cloudscheduler.jobs.get` to
deployers. A run that overlaps the previous one answers `{"status": "skipped", "reason":
"already_running"}` — retries never double an alert. The schedules themselves are not
confirmed by Grid Nova (placeholder BE3-1).

## 10. Custom domain and CORS

Cloud Run domain mappings are **not available in asia-south1** (and are Preview
elsewhere), so production uses a **global external Application Load Balancer** with a
serverless NEG and a Google-managed certificate. Staging can use its `*.run.app` URL
directly (`TRUSTED_PROXY_HOPS=1`).

```bash
export API_HOST=<api.gridnova.in>
gcloud compute addresses create gridnova-api-ip --global
gcloud compute addresses describe gridnova-api-ip --global --format='value(address)'
# → create a DNS A record: $API_HOST → that address, and wait for it to resolve
gcloud compute network-endpoint-groups create gridnova-api-neg --region=$REGION \
  --network-endpoint-type=serverless --cloud-run-service=gridnova-api
gcloud compute backend-services create gridnova-api-backend --global \
  --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend gridnova-api-backend --global \
  --network-endpoint-group=gridnova-api-neg --network-endpoint-group-region=$REGION
gcloud compute url-maps create gridnova-api-lb --default-service=gridnova-api-backend
gcloud compute ssl-certificates create gridnova-api-cert --domains=$API_HOST --global
gcloud compute target-https-proxies create gridnova-api-https --url-map=gridnova-api-lb \
  --ssl-certificates=gridnova-api-cert
gcloud compute forwarding-rules create gridnova-api-https-rule --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --address=gridnova-api-ip \
  --target-https-proxy=gridnova-api-https --ports=443
# Only the load balancer may reach the API service now:
gcloud run services update gridnova-api --region=$REGION --ingress=internal-and-cloud-load-balancing
```

Then in `deploy/env.production.yaml`: `TRUSTED_PROXY_HOPS: "2"` and
`PUBLIC_API_BASE_URL: "https://<api host>"`. **Restricting ingress is not optional**: with
`TRUSTED_PROXY_HOPS=2`, a request that skipped the load balancer would let the visitor
choose their own rate-limit key. The certificate can take up to an hour to become
ACTIVE (`gcloud compute ssl-certificates describe gridnova-api-cert --global`).
`GOOGLE_OAUTH_REDIRECT_URI` must use the same host, and must be added to the Google OAuth
client's authorised redirect URIs.

**`ALLOWED_ORIGINS`** — the exact strings, JSON array, `scheme://host`, no path, no
trailing slash (the startup guard refuses anything else, and it is also the list Clerk's
`azp` must be in):

- production: `["https://www.gridnova.in","https://gridnova.in"]` — both if the site
  answers on www and apex (`<confirm the real hosts>`).
- staging: the staging frontend's origin, e.g. the Vercel **branch alias**
  `https://<vercel-project>-git-<branch>-<vercel-team>.vercel.app` (stable per branch).
  Per-deployment preview URLs (with a hash) change on every push, so each would need a
  redeploy here — use the branch alias or a staging custom domain instead.

## 11. Smoke test — Gate 1 (staging), and after every production deploy

```bash
export API=<https://api host or the gridnova-api *.run.app URL>
export ORIGIN=<one ALLOWED_ORIGINS entry>
INTERNAL_KEY=$(gcloud secrets versions access latest --secret=gridnova-internal-api-key)
```

1. Liveness — `curl -fsS $API/api/v1/health` → `{"status":"ok"}`
2. Readiness — `curl -fsS $API/api/v1/health/ready` → `"status":"ready"`, `"schema":"at_head"`, `"territories":{"seeded":true,…}`
3. CORS preflight —
   `curl -si -X OPTIONS $API/api/v1/dashboard/leads -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: GET" | grep -i '^access-control-allow-origin'` → your origin;
   with `-H "Origin: https://evil.example"` → no such header.
4. **Clerk session token (D8)** — sign in to the dashboard as the admin, open the browser
   console and run `await window.Clerk.session.getToken()`; then
   `curl -fsS $API/api/v1/dashboard/employees/me -H "Authorization: Bearer <that token>"`
   → 200 with your role. A 403 `session_token_missing_claims` means §8 step 1 is missing;
   401 means issuer, `azp`/`ALLOWED_ORIGINS`, or an expired token (they last ~60 s).
5. Dev headers are refused —
   `curl -s -o /dev/null -w '%{http_code}\n' $API/api/v1/dashboard/leads -H 'X-Employee-Id: x' -H 'X-Employee-Role: admin'` → `403`
6. Internal job with the key (through the jobs service on production) —
   `curl -fsS -X POST $API/api/v1/dashboard/internal/check-survey-reviews -H "X-Internal-Key: $INTERNAL_KEY"` → 200 JSON;
   without the key → 401.
7. Public territory lists — `curl -fsS $API/api/v1/public/territories/states` → the states.
8. Interactive docs are off — `curl -s -o /dev/null -w '%{http_code}\n' $API/docs` → `404`
9. Manual checks (need real accounts — record the outcome in the deploy log):
   - **Google OAuth**: Dashboard → connect Google Calendar → consent → back on the
     dashboard as connected; book a test meeting; it appears in the calendar.
   - **Resend**: send a test proposal to a team address; the email arrives from
     `NOTIFY_FROM_EMAIL` with working links (domain verified, SPF/DKIM pass).
   - **R2**: upload a site-assessment document; download it from the dashboard (a signed
     link that expires); the bucket itself refuses an unsigned URL.
   - **PDF fonts**: approve a proposal for a customer with a Hindi or Bengali name; the
     PDF shows the name, not boxes (the image's `scripts/smoke_fonts.py` checks the fonts;
     this checks the real document).
   - **Associate approval**: approve a test application; within seconds the associate's
     record shows the Clerk invitation and both documents (they are generated after
     the response — BE-3).
   - **Scheduler**: `gcloud scheduler jobs run …` for each job; Cloud Logging shows a 200.
10. Rate limit (`TRUSTED_PROXY_HOPS` is right) — 21 quick `POST /api/v1/chat/message`
   from one machine end in 429; from a second network right after, 200. If the second
   network also gets 429, every visitor is sharing one bucket: check `TRUSTED_PROXY_HOPS`.

## 12. Rollback

- **Code**: send traffic back to the previous revision —
  `gcloud run revisions list --service=gridnova-api --region=$REGION` then
  `gcloud run services update-traffic gridnova-api --region=$REGION --to-revisions=<PREVIOUS_REVISION>=100`.
  Same for `gridnova-jobs`. Migrations are additive, so the previous revision runs
  against the newer schema (its readiness check reports `unknown_revision`, which is
  expected during a rollback — do not gate a rollback on readiness).
- **A migration**: only with a reviewed downgrade, from the migration job:
  `gcloud run jobs execute gridnova-migrate --region=$REGION --wait --args=-c,"alembic -c alembic/alembic.ini downgrade <REVISION>"`.
  `0047_be3_indexes` downgrades cleanly (drops its indexes).
- **Data**: Cloud SQL point-in-time recovery restores into a **new** instance —
  `gcloud sql instances clone $SQL_INSTANCE $SQL_INSTANCE-restore --point-in-time=<YYYY-MM-DDTHH:MM:SSZ>` —
  then point `gridnova-database-url` at it (new secret version + deploy).

## 13. Backups and data safety (D9)

- **Cloud SQL**: automated daily backups + point-in-time recovery (§3: 7 days on staging;
  30 backups + 7 days of logs on production), deletion protection on. Test a restore
  (§12, clone) once before launch and once a quarter.
- **R2**: the bucket is **private** (objects are only ever served through short-lived
  signed URLs, `STORAGE_SIGNED_URL_TTL_SECONDS`); no public access, no custom public
  domain on it. Cloudflare dashboard → R2 → bucket → Settings → Object lifecycle rules:
  abort incomplete multipart uploads after 1 day. Do **not** add an expiry rule for
  documents, receipts, agreements or ID cards — they are records. Separate buckets and
  API tokens per environment.
- **Logs**: Cloud Logging keeps `_Default` 30 days. Set the retention you need
  explicitly: `gcloud logging buckets update _Default --location=global --retention-days=<DAYS>`.
  Request bodies are never logged by the app.
- **Sentry**: `send_default_pii=False`, request bodies never sent, KYC values scrubbed
  (`app/security/sentry_scrub.py`). Also turn on Sentry's server-side Data Scrubber for
  the project.
- **Never shipped**: `gridnova_dev.db`, `*.db`, `.env*` are excluded from the image
  (`.dockerignore`, `tests/test_dockerignore.py`) and from git (`.gitignore`). Never
  copy a development database into staging or production.

## 14. Cost notes

- `--min-instances=1 --no-cpu-throttling` on `gridnova-api` means one instance with 1 vCPU
  and 2 GiB is billed **24 × 7**, whether or not anyone visits. Price it in the
  [Cloud Run pricing calculator](https://cloud.google.com/products/calculator) for
  asia-south1 (instance-based billing, always-allocated CPU). That is the price of
  background work that actually runs and no cold start. `gridnova-jobs` has
  `--min-instances=0`: it costs only while a scheduled run is in flight (plus a few idle
  minutes after).
- On staging you may drop `gridnova-api` to `--min-instances=0` to save money; the first
  request after idle then waits for a cold start, and queued background work can be cut
  short when the instance scales to zero (the sweepers pick it up later).
- Cloud SQL HA (production) roughly doubles the instance cost; storage, backups and the
  load balancer's forwarding rule are billed separately.

## 15. Rate limits, concurrency and scaling

The public rate limits (chat, estimate form, booking, accept link, registration, …) are
kept **in memory, per instance** (`app/security/rate_limit.py`): a visitor whose requests
land on N instances can make up to N × the limit. They are abuse brakes, not quotas;
with `--max-instances=4` the worst case is 4×. If they ever need to be exact, move the
store to Memorystore (Redis). Idle keys are forgotten, so memory does not grow with the
number of visitors.
