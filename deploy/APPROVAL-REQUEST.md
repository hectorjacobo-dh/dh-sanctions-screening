# Request: internal hosting for the Sanctions Screening Engine

**From:** Compliance — Sanctions
**For:** IT / Cloud Platform, Information Security, Data Privacy
**Ask:** run one container on DH-controlled infrastructure, reachable only by
authenticated Delivery Hero staff.

---

## What it is

A tool that screens Delivery Hero partners against the consolidated sanctions
lists published by OFAC, the EU, the UK (FCDO) and the UN — about **58,000
designations, refreshed daily**.

It replaces a manual Excel and Power Query process currently running on a
single analyst's laptop.

## Why this is a risk reduction, not a new risk

The control exists today regardless — it just runs on a workstation. Moving it
onto managed infrastructure removes four problems that exist **right now**:

| Today (laptop) | Hosted |
|---|---|
| Sanctions lists go stale whenever the laptop is off. Observed: **141 hours (~6 days) stale**, during which OFAC added 23 designations. | Refreshed daily, staleness alerted in the UI. |
| Partner data sits on one endpoint, backed up by nothing. | Managed database with automated backups. |
| One person can run screening; nobody else can see the results. | Regional compliance teams work the same queue. |
| No access control — the local build has no login by design. | Google Workspace SSO, role-based access, every action attributed. |

Three defects in the current spreadsheet process were found and fixed while
building this. All three were live gaps:

1. **UN-designated entities were never screened.** The Power Query loaded only
   the `INDIVIDUALS` block — **275 UN-designated companies, banks and
   organisations** were invisible to the control.
2. **OFAC rows had no country and no aliases**, so a party listed under an
   alternate spelling did not match.
3. Two of the five regulator URLs were **dead**: OFSI's Consolidated List
   closed on 28 January 2026, and the UN's `scsanctions.un.org` endpoint now
   returns 404.

## What data it holds

| Data | Source | Sensitivity |
|---|---|---|
| Sanctions designations (names, DOB, nationality, programme) | Published by OFAC / EU / FCDO / UN | **Public** — published by governments for this purpose |
| Partner legal names, GRID, country, status | DH internal | Confidential |
| UBO names, DOB, nationality *(when that data exists)* | DH internal | **Personal data — GDPR** |
| Screening results and analyst decisions | Generated | Confidential; names partners with a suspected sanctions nexus |
| Evidence documents uploaded by analysts | Analyst-supplied | Varies; may contain personal data |

**No data is sent to any third party.** The only outbound network calls are to
the four regulator endpoints to download their public lists. There is no
analytics, no telemetry, and **no AI or LLM service** — matching is
deterministic string comparison (`rapidfuzz`) running locally.

## What we are asking for

1. A **container host** — a Cloud Run service in a DH GCP project, or an
   internal VM. The container is built from a `Dockerfile` in the repository.
2. A **PostgreSQL database** (small: the full dataset is ~60,000 rows).
3. An **object storage bucket** for evidence documents, EU region.
4. **Authenticated access only** — Google Workspace SSO via Identity-Aware
   Proxy, or an equivalent internal mechanism. Never public.

Estimated cost: **€25–45 per month**. Full commands are in `deploy/README.md`;
nothing needs to be designed, only reviewed and run.

## Security posture already built in

- Runs as an **unprivileged user** in the container; no hardcoded credentials.
- **Role-based access**: admin / analyst / viewer.
- **Tamper-evident audit trail** — every refresh, search, decision and export is
  appended to a SHA-256 hash chain that can be re-verified on demand
  (`/api/audit/verify`). Altering a historic record breaks every later hash.
- **Evidence integrity**: documents are stored under their own SHA-256 and
  re-hashed on download; a file that no longer matches its digest is refused,
  not served.
- Uploads are **allowlisted by type**; executables and scripts are rejected.
- Security headers and a strict CSP are set at the proxy.
- **96 automated tests**, including regression tests for the sanctions-specific
  failure modes.

## For Data Privacy

- **Purpose**: sanctions compliance screening — a legal obligation under EU and
  national sanctions regulations.
- **Data subjects**: designated persons (public government data) and partner
  UBOs / directors (DH data).
- **Residency**: we propose `europe-west3` (Frankfurt) for all storage.
- **Retention**: **not yet defined — we need your decision.** Nothing is deleted
  automatically today. We will implement whatever period you set.
- **Access**: named DH staff only, authenticated via Workspace, every access
  logged and attributable.
- A DPIA may be warranted given the UBO personal data. We would rather start
  that conversation now than retrofit it.

## What we are not claiming

- The hash chain makes tampering **detectable**, not impossible. Anyone with
  database write access could rewrite the whole chain. True evidential
  immutability needs the chain head shipped to WORM storage — not yet built.
- The matching calibration is **tested but not yet validated against a known
  answer set**. We intend to run it in parallel with the existing spreadsheet
  before relying on it alone.
- This does not remove the need for human adjudication. The engine raises and
  explains alerts; a person decides, and the system refuses to record a
  dismissal without stated grounds.

---

**Contact:** Compliance — Sanctions
**Repository:** `dh-sanctions-screening` (deployment guide in `deploy/README.md`)
