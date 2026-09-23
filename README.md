# DH Group — Sanctions Screening Engine

Centralised screening engine replacing the Power Query / Excel process in
`Master_Sanctions_Database.xlsx`. Ingests live designations from five regulator
feeds, screens partner batches with calibrated fuzzy matching, and records every
action in a hash-chained audit trail.

---

## Status — what has and has not been verified

**Verified by execution on 2026-09-09** (Python 3.12.10, this workstation):

| Check | Result |
|---|---|
| Test suite | **77 passed** (`cd backend && pytest`) |
| All five regulator feeds fetched live | **58,206 designations** ingested |
| End-to-end batch run | 50,000 rows / 27,334 distinct names in **117 s** |
| Report export | 28,166-row CSV with full audit columns |
| Audit hash chain | **verified intact** over real activity |
| UI | Served at `http://127.0.0.1:8000`, search returns in 3–19 ms |

**Not yet verified — you must do these before relying on it:**

1. **Calibration against a known answer set.** Re-run your 242-match sample and
   confirm the engine reproduces every true positive. Run in parallel with the
   spreadsheet before decommissioning it. The alert volume is tuned but not
   validated against ground truth, and only you can supply that.
2. **Alert volume is still high.** At an 80% threshold the 50k-row sample
   produced 28,153 alerts (4,448 Strong / 23,705 Potential). Expect to raise the
   threshold — the UI slider makes this a live decision. See *Calibration*.
3. **UBO screening is inactive** because the data does not exist yet (below).

Sanctions screening is a regulated control. Verify it independently.

### Regulator endpoints — corrected

Two of the five URLs in the legacy process are dead. Both were found and fixed:

* **OFSI's Consolidated List closed on 28 January 2026.** The FCDO **UK Sanctions
  List** is now the single source for all UK designations, keyed on `Unique ID`
  rather than `OFSI Group ID`. Anything still pointing at `ConList.csv` is
  reading a list that stopped updating.
  Now: `https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv`
* **`scsanctions.un.org` now 404s.** The UN publishes from a new host.
  Now: `https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/EN/consolidated.xml`
* OFAC's `treasury.gov` URLs still work but now 302-redirect to
  `sanctionslistservice.ofac.treas.gov`; redirects are followed automatically.

---

## What this changes versus the current process

| Deficiency | Remediation |
|---|---|
| Exact-match failure | Normalisation strips legal forms, punctuation and diacritics; transliterates non-Latin scripts; bridges acronyms in both directions. |
| Static data decay | All five feeds refresh on a daily UTC schedule and on demand; the dashboard shows per-source age and flags anything over 48h. |
| No audit trail | Every refresh, run, decision and export is appended to a SHA-256 hash chain that can be re-verified end to end. |

### Three defects found in the existing pipeline

These were discovered while reading the Power Query model inside
`Master_Sanctions_Database.xlsx` and are fixed here:

1. **The UN list was only half-ingested.** The `UN_Sanctions_List` query expands
   `INDIVIDUALS/INDIVIDUAL` only. The UN consolidated file also contains an
   `ENTITIES/ENTITY` block — **275 UN-designated companies, banks and
   organisations** (confirmed by parsing the live file) that were **never
   screened**. `ingest/un.py` parses both blocks: 736 individuals + 275 entities.

2. **OFAC rows had no country and no aliases.** Both OFAC queries read
   `sdn.csv` / `cons_prim.csv` alone, neither of which carries a country. OFAC
   SDN is 91 of your 242 current matches, so a naive geographic control would
   have silently de-prioritised 38% of alerts on missing data.
   `ingest/ofac.py` additionally pulls `add.csv` (country) and `alt.csv`
   (aliases) and joins on `ent_num`.

3. **Aliases were being discarded.** The UK and EU queries de-duplicated
   alternate spellings away, leaving one name per designation. Aliases are now
   retained and separately indexed, so a partner registered under a
   transliteration still matches.

### Scale

Your partner export is **1,304,616 rows / 719,651 distinct legal names**.
Against 58k designations that is ~42 billion naive comparisons — which is why a
flat RapidFuzz loop is not viable and `blocking.py` exists. Candidate generation
uses three complementary inverted indexes (discriminative tokens, rare character
trigrams, acronyms) so each name is scored against a few hundred plausible
targets rather than the whole list.

Measured: **~234 distinct names/second**, so a full 719,651-name run takes
roughly **50 minutes**. Run it once over the whole base, then screen deltas.

One structural quirk of your export worth knowing: **rows 1–72,167 have an
empty `Legal Name`** — they are `Group` and `Brand` account records. Vendor
names begin at row 72,168. A sample taken from the top of the file screens
nothing at all.

---

## Running it

**One runtime, one command, one URL.** Node.js is not required — FastAPI serves
the UI itself from `backend/app/static/index.html` (no build step, no bundler).

Python 3.12.10 is already installed per-user at
`%LOCALAPPDATA%\Programs\Python\Python312` and the virtualenv is already created.
To start:

```powershell
cd "C:\Users\HéctorJacobo\Documents\dh-sanctions-screening\backend"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Then open **<http://127.0.0.1:8000>**.

That is the whole thing. On startup it checks watchlist age and, if anything is
more than 24 hours old, refreshes all five feeds in the background — the
dashboard tiles fill in live while you work.

To recreate the environment from scratch (new machine):

```powershell
winget install --id Python.Python.3.12 --scope user   # no admin rights needed
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                  # expect 40 passed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

API docs (interactive): <http://127.0.0.1:8000/docs>

### No login screen

`SINGLE_USER_MODE` defaults to **true**: there is no sign-in, and all audit
events are attributed to `LOCAL_OPERATOR` (set it in `.env` to your own address
so the audit trail names you).

> This is only safe while the server is bound to `127.0.0.1`. **If you ever bind
> to `0.0.0.0` or put this behind a proxy, set `SINGLE_USER_MODE=false` and a
> strong `SECRET_KEY` first** — otherwise anyone who can reach the port has full
> admin access with no credential. With it false, the app creates a bootstrap
> admin and prints a generated password once to the log.

### Two ways to screen

**Name search** (default tab) — type one name, get an answer in milliseconds.
Optional Date of birth / Nationality / Country / National ID fields do **not**
filter results; each hit reports whether they *match*, *differ* or were *not
provided*. Filtering on them would hide true positives whose identity data the
regulator recorded differently. The fuzziness slider re-runs the search live, so
you can see how threshold choice changes exposure. Every search is written to
the audit trail — you can prove a name was checked on a given date.

**Batch screening** — drag in a partner CSV, tune threshold and subject types,
run, adjudicate inline, export the report.

### Subject types

Regulators encode subject kind inconsistently (`individual`, `p`, `entity`, `e`,
`-0-`, `vessel`, `ship`). These are folded into one field: **Individual /
Entity / Vessel / Aircraft / Unknown**.

Vessels and aircraft are **ingested and fully searchable but excluded from batch
screening by default** — matching restaurant legal names against ship names is
near-pure noise. They are not deleted, because your designation counts should
reconcile against the regulator's published list, and a partner that *owns* a
designated vessel still needs to surface. Toggle them per run in the UI.

### If a regulator endpoint is down

Drop the manually downloaded file into `data/` and re-run the refresh. The
ingester falls back to it automatically:

| Source | Fallback filename |
|---|---|
| UK Sanctions | `data/UK-Sanctions-List.csv` |
| UN Sanctions | `data/consolidated.xml` |

The refresh records `file://…` as the source URL so the audit trail shows the
list came from a manual download rather than a live fetch.

---

## Input format

**Excel (`.xlsx`, `.xlsm`) and delimited text are both accepted**, detected from
the filename or the file's magic bytes. Legacy `.xls` is rejected with an
instruction to re-save; supporting it would mean a second parsing library for a
format Excel itself has deprecated.

Three kinds of tolerance, each driven by what real exports actually look like:

* **The delimiter is sniffed** — your semicolon-delimited export works unmodified.
* **The header need not be row 1.** The most header-like row in the first 25 is
  used. Real files need this: the UK Sanctions List opens with
  `Report Date: 16-Jun-2026`, and your own adjudication workbook has a merged
  title row above the real header.
* **The right sheet is chosen.** Workbooks often lead with a summary or pivot
  tab. Each sheet's best header row is scored and the highest wins, with row
  count as the tiebreak — verified against your matches workbook, where the
  records are on sheet 2.

### When the column has no name we recognise

If no header matches a known spelling, the name column is identified from its
**contents**: legal names are text, usually multi-word, reasonably long and
close to unique, whereas countries and statuses repeat, IDs are short codes and
dates are dates. So a Spanish export headed `Razón Social`, or one with no
useful labels at all, still screens.

Two guards keep this honest, because a wrong guess here silently screens the
wrong field:

* A column of short single tokens is rejected however distinct it looks —
  otherwise a `GRID` column wins on uniqueness alone and the engine screens
  account codes against sanctions lists.
* If nothing scores as name-like, the upload is **refused with an explanation**
  rather than guessed at.

The UI always states which column it read, which row the header was on, and
whether that came from the header or from content — and flags the content case
in amber so you check it. The same detail is written to the audit trail.

Recognised header spellings (case, spacing and punctuation insensitive):

| Field | Accepted headers |
|---|---|
| Partner name *(required)* | `Legal Name`, `Entity_Name`, `Partner Legal Name`, `Company Name` |
| UBO name | `UBO_Name`, `UBO`, `Beneficial Owner` |
| Country | `Account Country`, `Country`, `Market` |
| Identifier | `GRID`, `Partner ID`, `Account ID` |
| Tax ID | `Tax_ID`, `VAT Number`, `Registration Number` |
| Status | `Account Status`, `Status` |
| Created | `Created Date`, `Onboarding Date` |

> **`UBO_Name` and `Tax_ID` are not present in `DH_Group_All_Restaurants.csv`.**
> The engine supports them and will screen UBOs the moment that data exists.
> Until then you are screening legal entity names only — which does not detect a
> sanctioned individual behind a clean-sounding company. Sourcing UBO data is
> the single highest-value improvement available to this control.

---

## Calibration

Set in `.env`, overridable per run from the UI.

| Setting | Default | Meaning |
|---|---|---|
| `MATCH_THRESHOLD` | 80 | Similarity % required to raise an alert. |
| `STRONG_MATCH_THRESHOLD` | 95 | At/above this an alert is banded *Strong Match*. |
| `GEO_CONTRADICTION_PENALTY` | 15 | Points deducted when countries contradict. |
| `MAX_CANDIDATES_PER_NAME` | 400 | Blocking recall cap per name. |
| `SCREEN_SUBJECT_TYPES` | `Individual,Entity,Unknown` | Batch scope. Add `Vessel,Aircraft` to include them. |

### What the scorer does and does not use

This was calibrated empirically against the live 58k watchlist, and the
measurements mattered — the first working version put **34% of all scored pairs
over the 80% threshold**, which is unusable. Progression on a fixed 3,000-name
sample: **274,720 → 101,712 → 4,016 → 1,818 alerts**, with the share of names
raising at least one alert falling from 66% to 9%.

| Scorer | Used? | Why |
|---|---|---|
| `token_sort_ratio` | **Primary** | Order-insensitive but still length-sensitive, so a fragment cannot satisfy it. |
| `token_set_ratio` | Only when length ratio ≥ 0.70 | Returns ~100 whenever one name's tokens are a subset of the other's. Ungated, "Pizza" matches "Pizza House Trading Co" at full score. |
| Acronym bridge | Yes, min 3 characters | Handles `UAC` ↔ `PJSC United Aircraft Corporation`. Two-letter acronyms were dropped: `BM` matched "Bank Melli" to "BM Holding AG" at 100. |
| Token alignment | **Manual search: always. Batch: only for discriminative names.** | Scores whether the searched name is *present as a name*, with prefix forms (`GAZPROM`→`GAZPROMBANK`) and leftover tokens weighted by whether they carry identity. `gazprom` at 90% returned **0** before this and **86** after; 109 designations contain it. Applied in batch it would reflood the queue, since "Pizza" in "Pizza House Trading Co" is structurally identical. |
| Containment rule | Yes, when the contained name is discriminative | A name of 3+ tokens or 12+ characters wholly inside another admits `token_set` even below the length gate. Without it, searching `banco de venezuela` **missed `BANCO DE VENEZUELA SA BANCO UNIVERSAL`** — a live OFAC SDN designation. Cost: +2 alerts on the 3,000-name sample. |
| `partial_ratio` | **Recorded, never scored** | Pure substring test — the single largest false-positive source for company names. |
| `WRatio` | **Recorded, never scored** | Falls back to `partial_ratio × 0.9` for uneven lengths, reintroducing the same problem. |

Two pre-processing controls exist for reasons found in your real data:

* **Runs of single-character tokens are merged.** `U N N I E STORE` (spaced-out
  branding) otherwise produces six one-letter tokens that match single letters
  anywhere in a designation — vessel names like `SILVIA I`, initials like
  `HAN, Kwon U`. Also fixes `L.L.C.` → `LLC`.
* **Designations whose name is *only* a legal form are excluded at ingest.** The
  regulators publish a few rows reading merely "sociedad de responsabilidad
  limitada"; screening against those matches every company registered under that
  form. 33 such rows were dropped on the live feeds (26 OFAC SDN, 6 EU, 1 UK),
  logged and counted in the audit trail — never silently.

### How the geographic control behaves — and why

Your specification asked the engine to *"automatically lower the threat score"*
on a country contradiction. Implemented literally, that control creates false
negatives, which is the exact failure sanctions screening exists to prevent —
designated parties routinely operate through fronts in third countries, and a
UAE-registered restaurant with a Russian-designated UBO is the case you most
need to catch.

It is therefore implemented with three guardrails:

- **It never fires on missing data.** Both sides must resolve to a known
  country. Unknown is recorded as `GEO_UNKNOWN_NO_ADJUSTMENT`, never as a
  contradiction.
- **It never suppresses a row.** Banding uses the *pre-geography* score, so a
  contradiction lowers queue priority but cannot push a genuine name match out
  of the alerting population.
- **It is always disclosed.** Every affected row carries
  `GEO_CONTRADICTION_DEPRIORITISED` and its exact point adjustment in both the
  UI and the exported CSV.

Set `GEO_CONTRADICTION_PENALTY=0` to disable it entirely.

---

## Dismissal grounds and the due-diligence record

Free text alone is not an auditable rationale — it can't be counted, compared
across regions, or reviewed for consistency. So every disposition carries a
**structured reason code** from a closed taxonomy (`dismissal.py`), and three
rules are enforced **server-side**, not just in the form:

1. **Clearing or confirming an alert requires stated grounds.** A `False Match`
   or `Positive Match` without a reason code is rejected with HTTP 422.
2. **Weak grounds require a written rationale.** `COUNTRY_MISMATCH`,
   `COMMON_NAME_COINCIDENCE`, `REQUIRES_FURTHER_DILIGENCE` and `OTHER` are not
   self-evidencing, so the rationale field becomes mandatory. A designated party
   routinely operates through a third country — country alone must never clear
   an alert silently.
3. **Grounds must match the direction of the decision.** `DOB_MISMATCH` cannot
   support a `Positive Match`; `CONFIRMED_DESIGNATED_PARTY` cannot support a
   `False Match`.

Reverting a decision to *Unreviewed* needs no grounds, so nobody is trapped by
a misclick.

### Evidence documents

Attach the trade licence, registry extract, UBO register, screenshot or email
that supports the decision. Files are stored **under their own SHA-256** rather
than their supplied filename, which:

* makes path traversal structurally impossible rather than filtered,
* de-duplicates the same document attached to many alerts,
* lets the app **re-hash on download** and refuse to serve a file that no longer
  matches its recorded digest.

The digest goes into the audit chain, so the record proves *that exact document*
existed when the alert was cleared. Allowlist of document types only — nothing
executable or script-like. 25 MB per document. Files never leave your machine.

There is **no delete endpoint**: evidence that can be removed after the fact
isn't evidence. Superseded documents are marked, not erased.

### Manual searches produce records too

A search on its own is ephemeral — it evidences nothing and there is nothing to
attach a document to. **Create case for review** promotes one into a persisted
case that uses the *same* tables as a batch upload, so adjudication, reason
codes, evidence, the audit chain and the due-diligence record all apply
unchanged. Manual cases are tagged `manual` in the run list.

A case is created **even when nothing matched** — that is the point. "This party
was screened on this date, against these exact list versions, at this threshold,
and no designation matched" is the evidence due diligence exists to produce, and
it is worthless unrecorded. The clean-result record states the finding and then
states its limits explicitly: valid only for the threshold applied, the subject
types screened, the list versions of that date, and the identifiers as supplied.

### Documents are read and cross-checked automatically

On attach, the document's text is extracted and compared against the
designation it is offered against. The engine reports:

| Check | What it looks for |
|---|---|
| Designated party named | Does the document mention the designation? If it does, that points *towards* a match. |
| Partner named | A document that doesn't name the partner may not be about them. |
| Dates | Designation year(s) appearing in the document point at the same subject. |
| Identifiers | A registration/tax/passport number shared with the designation record. |
| Country | Corroboration or conflict, with the third-country caveat stated. |

Formats read: PDF (text layer), `.docx`, `.pptx`, `.xlsx`, `.eml`, `.csv`,
`.txt`, `.md`. Legacy `.doc`/`.xls`/`.msg` are reported as unreadable with
instructions to re-save. Images and scanned PDFs cannot be read — there is no
OCR here.

**The engine recommends; it never decides.** This is the single most important
constraint in the feature, and the reason is concrete: a scanned licence yields
zero text, and a system that inferred "nothing contradicts, therefore clear" from
that would dismiss alerts on no evidence at all. So an unreadable document is a
first-class outcome that explicitly provides **no corroboration**:

```
[unreadable] Document readability — the PDF has 1 page(s) but no extractable
             text — most likely a scan or photograph
engine advised: This document could not be read, so it provides NO automatic
             corroboration. Review it by eye before relying on it, and do not
             treat its presence as evidence in itself.
```

And a document that names the designated party pushes the other way:

```
[contradicts] Designated party named in document — 'BANCO CENTRAL DE VENEZUELA'
              appears in this document (100% of its distinctive terms)
engine advised: 2 finding(s) point TOWARDS this being the designated party. Do
              not clear this alert without resolving them.
```

Findings, the recommendation and the confidence are written into the audit
chain and both report formats — so the record shows what the engine advised
*and* that a named person accepted or overrode it.

### The record itself

**Due diligence record** (`due-diligence-record.txt`) is the document a
reviewer or regulator actually reads. Per alert it states what matched, on what
grounds it was resolved, the rationale, the evidence filenames and digests, who
decided, when (Berlin and UTC), and whether the audit chain verifies.

Two things it deliberately does *not* hide:

* **Unreviewed alerts are listed explicitly.** An incomplete review is a
  finding; omitting it would misrepresent the file as a completed screening.
* **Decisions with no stated grounds are counted and named** at the top under
  `!! N decision(s) carry NO stated grounds`. Decisions recorded before this
  control existed show as `Grounds: NOT STATED` rather than being quietly
  presented as sound.

The CSV gains `Reason_Code`, `Reason_Label`, `Evidence_Documents` and
`Evidence_SHA256` for analysis; the dossier is for reading.

## Adjudication and comments

Decisions, comments and Regional GRC input are saved per alert and flow into
the exported report. Three ways an edit persists:

* Choosing a **Decision** saves immediately.
* Leaving a **Comment** or **GRC input** field saves that row.
* **Save comments** in the sticky bar saves everything outstanding at once —
  including the field you are still typing in.

Each row shows its own state (`unsaved` → `saving…` → `saved 14:39`), the bar
counts outstanding edits, and closing the tab with unsaved work prompts first.
A failed save keeps your text on screen and says so rather than discarding it.
Every change is written to the audit chain with the previous and new values, so
an amended decision is reconstructable.

## Timezone: Berlin for people, UTC for the record

Everything you read is **Europe/Berlin** — the dashboard, run list, saved
stamps, and the report filename (`..._2026-09-09_1428_Berlin_....csv`).

Everything stored and hashed is **UTC**, and the report carries both:

| Column | Purpose |
|---|---|
| `Screened_At_Berlin`, `Decided_At_Berlin` | For the reader. |
| `Screened_At_UTC`, `Decided_At_UTC` | **Authoritative.** What the audit chain commits to. |

This split is deliberate. Berlin shifts between CET and CEST twice a year, so a
local timestamp is ambiguous across the changeover and cannot anchor an audit
trail — one hour each October genuinely occurs twice. Changing
`DISPLAY_TIMEZONE` re-renders the local columns and never alters a hash;
`tests/test_matching.py` asserts that.

## The audit trail

Each `audit_events` row stores the SHA-256 of its own canonical payload
concatenated with the previous row's hash. Editing or deleting any historic
record breaks every subsequent hash.

- **Audit trail → Verify chain** recomputes the whole chain and reports the
  first inconsistency.
- `backend/tests/test_audit_chain.py` proves detection works for both an
  altered payload and a deleted record.

Every exported row is self-contained evidence, carrying `Screened_At_UTC`,
`Source_List`, `Run_Reference`, `Input_File_SHA256`, `Source_List_SHA256` (the
digest of the exact published regulator file) and `Engine_Threshold`.

> **On "immutability":** a hash chain makes tampering *detectable*, not
> *impossible*. Anyone with database write access can rewrite the whole chain.
> For evidential immutability, ship the chain head to append-only storage
> (WORM S3 Object Lock, or a signed daily digest to a separate system). That is
> not built here.

---

## Why this is not a standalone HTML file

A browser page — whether a local `.html` file or a hosted one — **cannot fetch
the sanctions lists**, so it cannot be the screening engine:

* A local `file://` page has origin `null`. For it to read OFAC/EU/UK/UN, each
  regulator would have to send `Access-Control-Allow-Origin` headers permitting
  you. None do. The browser blocks the request before it leaves the machine.
* Hosted single-page artifacts run under a strict Content-Security-Policy that
  blocks all external network requests outright.
* There would also be nowhere tamper-evident to keep the audit chain —
  `localStorage` is editable from the console in two keystrokes.

Moving the fetching server-side into Python removes all three problems at the
cost of one `winget install`. The result is still "open a page in your browser",
just at `http://127.0.0.1:8000` instead of a file path.

## Sharing this with other compliance teams

**A `localhost` app cannot be shared** — your colleagues cannot reach
`127.0.0.1` on your workstation, and running a screening engine off one laptop
is not a viable control anyway.

`docker-compose.yml` is production-shaped (PostgreSQL, unprivileged container,
loopback-bound ports, CSP headers). It is **not** production-ready to expose,
because it deliberately does not include TLS or SSO.

Before this is shared with anyone:

- **It must be deployed on Delivery Hero infrastructure**, not a personal
  machine — this processes confidential partner data and produces output naming
  partners with a suspected sanctions nexus.
- **Involve InfoSec and Data Privacy first.** Screening output is GDPR-relevant
  personal data (UBO names, dates of birth, nationality). It needs a lawful
  basis, a retention schedule and a documented data residency position — the
  same review your current spreadsheet arguably never received.
- **Authentication is required the moment it leaves your machine.** You have
  said SSO is not wanted, which is a reasonable call for a single-operator local
  tool — and `SINGLE_USER_MODE=true` reflects that. But that mode means *no
  credential at all*. Before anyone else can reach it, set
  `SINGLE_USER_MODE=false` and a strong `SECRET_KEY`; the built-in
  email/password auth with admin/analyst/viewer roles then applies. It is not a
  substitute for corporate identity, so if this becomes a shared control, expect
  InfoSec to ask for SSO at a reverse proxy regardless.
- **Do not expose it to the public internet**, and do not use a tunnelling
  service (ngrok or similar) to share it — that publishes partner data outside
  DH's control.

The realistic path is a ticket to whoever owns internal app hosting, with
InfoSec and Privacy as reviewers.

---

## Project layout

```
backend/
  app/
    normalize.py      Pre-processing control (legal forms, transliteration, acronyms)
    blocking.py       Candidate generation — makes 42B comparisons tractable
    matching.py       Explainable scoring + geographic control
    geography.py      Country resolution and DH region mapping
    subject_type.py   Individual / Entity / Vessel / Aircraft normalisation
    screening.py      Batch orchestration
    report.py         Adjudication CSV export
    audit.py          Hash-chained audit log
    ingest/           One adapter per regulator + refresh orchestration
    routers/          API endpoints (users, watchlists, runs, search, audit)
    static/index.html The whole UI — no build step, served by FastAPI
  tests/              40 calibration, parsing and audit-chain regression tests
frontend/             Optional React version of the same UI. NOT needed to run
                      the app; requires Node.js. Kept for a future hosted
                      deployment — the served UI is backend/app/static.
data/                 Local DB, region map, manual-download fallbacks — gitignored
```

## Operational notes

- **SQLite is the default and is single-writer.** Move to PostgreSQL
  (`DATABASE_URL`) before more than one analyst uses this concurrently.
- **The first full run will be slow** — building the blocking index over ~40k
  designations plus scoring 719k distinct names is minutes, not seconds. Run it
  once against the full base, then screen deltas.
- **Retention:** the three most recent successful snapshots per source are kept
  so a historic run can be reproduced against the list as it stood that day.
