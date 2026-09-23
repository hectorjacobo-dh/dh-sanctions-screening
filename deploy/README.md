# Hosting the screening engine

The application is a single container with no build step for the UI. What
changes between a workstation and a hosted deployment is configuration, not
code — with one exception noted below (evidence storage), which is already
implemented.

---

## Before anything technical

This system holds **confidential partner data and UBO personal data** (names,
dates of birth, nationality) and produces output naming partners with a
suspected sanctions nexus.

It must run in an account **Delivery Hero controls**. Not a personal Google,
AWS or Vercel account — that is shadow IT, it puts GDPR-relevant data outside
DH's control, and if the account owner leaves the company the data leaves with
them. See `APPROVAL-REQUEST.md` for the one-page ask to send to whoever owns
internal hosting.

---

## Option A — Google Cloud Run *(recommended if DH has a GCP project)*

You already have a Google Workspace identity, which is what makes this the best
fit: **Identity-Aware Proxy turns your `@deliveryhero.com` login into the
application's sign-in**, with no password management and no code from us.

### Three platform gotchas that will bite you if ignored

These are specific to Cloud Run and are the reason a naive deployment fails:

1. **Evidence files vanish without object storage.**
   Cloud Run's filesystem is ephemeral. Set `EVIDENCE_BACKEND=gcs` and
   `EVIDENCE_BUCKET`. With the default `local` backend every attached document
   disappears on the next restart or scale-to-zero, while the audit chain keeps
   claiming — truthfully but uselessly — that a document with that digest once
   existed.

2. **Batch screening stalls unless CPU stays allocated.**
   A run returns `202` immediately and screens in the background. Cloud Run
   throttles CPU to near zero after the response unless you deploy with
   `--cpu-no-throttling`. Without it a 50-minute batch never finishes.

3. **The daily refresh never fires on a scale-to-zero service.**
   The scheduler runs in-process; no traffic means no process. Either keep
   `--min-instances=1`, or leave it at zero and drive the refresh from Cloud
   Scheduler (below). Scheduler is cheaper and more reliable — a missed refresh
   is a stale watchlist, which is the exact risk this project exists to remove.

### Deploy

```bash
PROJECT=dh-compliance-screening      # your DH project
REGION=europe-west3                  # Frankfurt — keep the data in the EU
SERVICE=sanctions-screening

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
    storage.googleapis.com cloudscheduler.googleapis.com iap.googleapis.com

# 1. Evidence bucket — EU region, uniform access, versioned
gsutil mb -l "$REGION" -b on "gs://${PROJECT}-evidence"
gsutil versioning set on "gs://${PROJECT}-evidence"

# 2. Postgres. db-g1-small is ample: the watchlist is ~60k rows.
gcloud sql instances create screening-db \
    --database-version=POSTGRES_16 --tier=db-g1-small --region="$REGION" \
    --storage-auto-increase --backup
gcloud sql databases create screening --instance=screening-db
gcloud sql users create screening --instance=screening-db --password='<strong-password>'

# 3. Secrets, not environment variables, for anything sensitive
python -c "import secrets; print(secrets.token_urlsafe(48))" | \
    gcloud secrets create screening-secret-key --data-file=-
printf '%s' '<strong-password>' | \
    gcloud secrets create screening-db-password --data-file=-

# 4. Service account with least privilege
gcloud iam service-accounts create screening-run
SA="screening-run@${PROJECT}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" --role=roles/cloudsql.client
gsutil iam ch "serviceAccount:${SA}:objectAdmin" "gs://${PROJECT}-evidence"
gcloud secrets add-iam-policy-binding screening-secret-key \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding screening-db-password \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor

# 5. Build and deploy
gcloud run deploy "$SERVICE" \
    --source=./backend \
    --region="$REGION" \
    --service-account="$SA" \
    --add-cloudsql-instances="${PROJECT}:${REGION}:screening-db" \
    --set-env-vars="EVIDENCE_BACKEND=gcs,EVIDENCE_BUCKET=${PROJECT}-evidence" \
    --set-env-vars="SINGLE_USER_MODE=false,DISPLAY_TIMEZONE=Europe/Berlin" \
    --set-env-vars="REFRESH_ON_STARTUP=true" \
    --set-secrets="SECRET_KEY=screening-secret-key:latest,DB_PASSWORD=screening-db-password:latest" \
    --set-env-vars="DATABASE_URL=postgresql+psycopg://screening:\${DB_PASSWORD}@/screening?host=/cloudsql/${PROJECT}:${REGION}:screening-db" \
    --cpu=2 --memory=4Gi --timeout=3600 --cpu-no-throttling \
    --min-instances=0 --max-instances=4 \
    --no-allow-unauthenticated          # IAP / IAM decides who gets in
```

`--no-allow-unauthenticated` is the important flag: the service is never
publicly reachable. Grant access per person or per Google group:

```bash
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
    --member='group:compliance-sanctions@deliveryhero.com' \
    --role=roles/run.invoker
```

### Daily refresh via Cloud Scheduler

```bash
gcloud scheduler jobs create http screening-refresh \
    --location="$REGION" --schedule='15 3 * * *' --time-zone=UTC \
    --uri="https://<service-url>/api/watchlists/refresh" --http-method=POST \
    --oidc-service-account-email="$SA"
```

The endpoint already exists and requires the `admin` role, so give the
scheduler's service account an admin user record (see *First run*).

### Cost

Roughly **€25–45/month**: Cloud SQL `db-g1-small` is the bulk of it, Cloud Run
is near zero at this usage, the bucket is pennies. Dropping to scale-to-zero
without `--min-instances` is what keeps it cheap.

---

## Option B — an internal VM or container host

Often the fastest approval, because nothing leaves DH's network.

```bash
cp deploy/env.production.example .env     # fill in every value
docker compose up -d
```

`docker-compose.yml` already wires Postgres and binds the app to loopback. Put
a reverse proxy in front for TLS and authentication, and keep it on the
internal network — do not publish it.

With a persistent volume you can leave `EVIDENCE_BACKEND=local`; the storage
abstraction exists precisely so this option needs no code change.

---

## Mandatory before anyone else can reach it

| Setting | Value | Why |
|---|---|---|
| `SINGLE_USER_MODE` | `false` | Otherwise **anyone who reaches the port has full admin with no credential**. |
| `SECRET_KEY` | 48+ random chars | Signs session tokens. The default is a known placeholder. |
| `EVIDENCE_BACKEND` | `gcs` on Cloud Run | Local files do not survive a restart. |
| `DATABASE_URL` | Postgres | SQLite is single-writer and file-based. |
| Ingress | authenticated only | Never `--allow-unauthenticated`. |

With `SINGLE_USER_MODE=false` the app creates a bootstrap admin on first start
and prints a generated password to the log **once**. Sign in, create real
accounts, then disable it.

---

## First run

1. Open the service URL and sign in.
2. **Watchlists → Refresh all lists.** Expect five green tiles and ~58,000
   designations. A red tile means that regulator's URL has moved — the app
   falls back to a manually downloaded file in `data/`, so a moved URL degrades
   to "stale", never to "silently empty".
3. Create user accounts (`admin` / `analyst` / `viewer`).
4. Run a small batch to confirm end to end, then download the due diligence
   record.

## Verifying it is actually working

```bash
curl -s https://<service-url>/api/health
# Then, signed in, confirm:
#   /api/watchlists/status  -> five sources, any_source_stale=false
#   /api/audit/verify       -> {"valid": true}
```

`/api/audit/verify` recomputes the whole hash chain. If it ever returns
`valid: false`, records have been altered after the fact — treat that as an
incident, not a bug.

---

## What is still not solved by hosting

- **Retention.** Nothing is deleted automatically. Agree a retention period for
  screening results and evidence with Privacy, then implement it.
- **Backups.** Cloud SQL automated backups are enabled above; the evidence
  bucket has versioning. Neither has been restore-tested.
- **Audit immutability.** The hash chain makes tampering *detectable*, not
  *impossible*. For evidential immutability, ship the chain head daily to
  WORM storage (bucket with a retention lock) — not yet built.
- **UBO screening** stays inactive until UBO data exists in the partner export.
