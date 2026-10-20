# Handover

For whoever maintains this next. Written by the person who built it, while the
whole codebase was still in one head.

It assumes you are a competent engineer who has never seen this project, and
knows nothing about sanctions screening. It tells you the parts that are not
obvious from reading the code, and it is honest about what is unfinished.

---

## 1. What this is

A sanctions screening engine for Delivery Hero Compliance. It downloads the
OFAC, EU, UK and UN designation lists daily, screens counterparty names against
them, and records every search and decision in a tamper-evident log.

It replaced a manual Excel and Power Query process with three defects: exact
matching only (so "UAC" never found "United Aircraft Corporation"), lists
refreshed by hand and therefore stale, and no record of who decided what.

**Who uses it:** Compliance officers. Not engineers. Treat every screen as
something a non-technical person has to read under time pressure and be able
to defend to an auditor afterwards.

## 2. Run it

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
PYTHONPATH=. .venv/Scripts/python -m pytest tests/       # 189 tests, ~3s
PYTHONPATH=. .venv/Scripts/python serve.py               # http://127.0.0.1:8000
```

On the officer's laptop a Windows scheduled task runs `serve.py` at logon plus
a five-minute watchdog (`autostart-install.ps1`). `docker-compose.yml` brings up
Postgres and the API for a shared deployment.

There is **no frontend build step**. `frontend/` is an abandoned React scaffold
— see its `SUPERSEDED.md`. The entire interface is `backend/app/static/index.html`,
served at `/`.

## 3. Layout

| Path | What lives there |
|---|---|
| `app/ingest/` | One adapter per regulator, all flattening onto `CanonicalTarget` |
| `app/ingest/endpoints.py` | URL recovery when a regulator moves a file |
| `app/ingest/identity.py` | Pulls DOB, nationality, documents out of OFAC's free text |
| `app/matching.py` | Scoring. Start here for "why did/didn't this match?" |
| `app/blocking.py` | The index that makes 33k×N comparisons tractable |
| `app/geography.py` | Country resolution, ISO 3166-1 via pycountry |
| `app/dismissal.py` | Resolution vocabulary: status → risk levels → reason |
| `app/screening.py` | Batch runs, and all the upload-parsing heuristics |
| `app/audit.py` | The hash chain |
| `app/report.py`, `app/pdf_report.py` | CSV and PDF. **Two generators — see §6** |
| `app/static/index.html` | The whole UI, one file |
| `calibrate.py` | Validation harness. **Unfinished — see §7** |

## 4. Things that will bite you

**The four feeds each lie in their own way.** Every adapter has comments
explaining the specific defect it works around. The three worst:

- *The EU repeats `Entity_logical_id` six times* in its header, once per
  sub-record block. A dict comprehension keeps the last, which is populated on
  ~7% of rows. That broke grouping entirely: 6,234 parties appeared as 31,047
  fragments, aliases never attached to their primary name, and every EU
  identity field came through empty because the rows carrying DOB and
  nationality hold no name and were dropped by the "no names" guard.
- *OFAC publishes no identity columns.* Date of birth, nationality, passport
  and national ID are all inside one free-text `remarks` field as
  semicolon-delimited clauses. `identity.py` parses them; 99.93% of 92,813
  clauses are classified, and `audit_clauses()` re-runs the census so a new
  label shows up as residue rather than vanishing.
- *OFSI publishes Windows-1252*, not UTF-8.

**Aliases must be scored, not just indexed.** The blocking index recalls a
designation by any published spelling, but scoring only the primary name threw
that away — "UAC" reached "PUBLIC JOINT STOCK COMPANY UNITED AIRCRAFT
CORPORATION" through its alias and then scored 8.9 against the primary. See
`score_against_all_names`.

**`recall_mode` is a deliberate policy split, not a tuning preference.** Manual
search is recall-first (the officer typed one name and will read every row);
batch is precision-first (nobody reads 28,000 alerts). Changing one calibration
without the other will look like a bug in the other mode.

**Never let a country resolve to the wrong country.** `resolve_country` splits
on `/ ; |` but *not* on commas, because ISO publishes "Korea, North" and
"Congo, Democratic Republic of the". Splitting on commas first made the latter
resolve to CG — the *other* Congo — so the geographic control reported
contradictions that did not exist. A bare ambiguous name ("Korea") resolves to
nothing on purpose: guessing would be a fact stated from a coin flip.

## 5. The rules the UI enforces, and why

- **A signal states which side of a comparison is missing.** "not provided" was
  ambiguous: it meant *the officer searched without a DOB*, but read as *the
  sanctions record has no DOB*. An officer who believes the regulator holds no
  date of birth has lost their strongest discriminator, in the direction of
  clearing the alert.
- **A false match cannot carry a risk grade** (`Unknown`/`N/A` only). "False /
  Low" would sit in a report beside genuine low-risk positives and be counted
  with them.
- **Confirming or clearing requires a written rationale.** Both commit the
  business to something.
- **Deleting a case writes `SCREENING_RUN_DELETED` to the chain first**, with a
  count of how much resolved work went with it. A screening may leave the
  worklist, never the record.

## 6. Traps that have already caught someone

**There are two record generators.** `report.py` builds the CSV and the text
dossier; `pdf_report.py` builds the PDF. Changing the vocabulary in one does not
change the other — a first pass at renaming statuses edited only the TXT
generator, which was then deleted, so the PDF was unchanged and looked fine
until its text was extracted and read.

**The test suite did not import the app.** A syntax error in a router shipped
with 138 tests green; the only symptom was a server that refused connections.
`tests/test_app_imports.py` now imports the app and every router and asserts the
routes the UI calls are registered.

**Duplicate test names silently delete coverage.** `test_matching.py`
accumulated 33 shadowed definitions through repeated appends — Python keeps the
last, so a third of the file never ran while the suite reported green. Guarded
now in `test_app_imports.py`.

**There is no JavaScript engine in CI.** `tests/test_frontend_syntax.py` is a
set of static guards, each written after a specific defect broke the page:
`const` shadowing a parameter, an odd backtick, a referenced element id that
does not exist, a template reading a field the API never returns, and
`renderRun` drawing rows before the status vocabulary had loaded. They are
narrow on purpose. **Load the page in a browser before shipping UI changes** —
the guards cannot do it for you.

## 7. Unfinished, and it matters

**The engine has never been validated against the manual process.** This is the
gate before it becomes the control of record, and it is not a code task.

`calibrate.py` is the harness. It has two modes and only one is validation:

```bash
# Real calibration. Needs the team's own spreadsheet.
python calibrate.py --from-file "DH Group_Restaurants Matches in Sanctions Lists.xlsx"

# Regression only. Reports 100% by construction — see below.
python calibrate.py --from-archive "../archive/*/partner-data-archive.json"
```

The archive mode cannot validate anything, and the reason is worth
understanding before anyone cites its output. The archive holds the engine's
own past alerts, so the only names recoverable from it are ones the engine
already matched. Of 234 names screened in September, 230 appear in alerts and
the 4 that matched nothing left no trace. Asking "of the names it found, how
many does it find?" returns 100% and means nothing. The script prints a warning
saying so, and `deploy/CALIBRATION.md` leads with it.

**To actually do this you need `DH Group_Restaurants Matches in Sanctions
Lists.xlsx`** — the original file, all 379 rows. Compliance has it. Then:

1. Run `--from-file`. Read every name scoring below the operating threshold.
2. A low score is not automatically an engine fault: the manual process
   produced false positives too, and a name it flagged may not be designated.
   Each one has to be read by someone who knows the rules.
3. Precision remains unmeasurable either way — that needs the names the team
   screened and **cleared**, and no record of those survives. If anyone wants a
   false-positive rate, that has to be captured going forward.

**Other open items**

- Ongoing screening: re-screen a monitored list on each refresh and show what
  changed. Designed, never built. The data is there — `WatchlistRefresh` keeps
  the payload digest and record count per snapshot.
- Only 3 snapshots are retained (`SNAPSHOTS_RETAINED`), which is why "what
  changed since last week?" could only be answered from an archive file.
- Batch screening applies one brand to a whole file. If uploads span brands,
  that belongs in a column in the template instead.

## 8. Data and privacy

The application stores **public designation data only**. Names to be screened
are uploaded at screening time and no customer, partner or employee records are
retained.

Two exceptions, both outside git:

- `archive/` — the pre-purge archive: 902 alerts and **236 personal (UBO)
  names**, retained as evidence the purge ran correctly. Gitignored, because git
  history is effectively permanent. It belongs wherever Compliance keeps
  retained records, not in a source tree. There are currently two identical
  copies; one is enough.
- `data/screening.db` and `data/calibration-detail.csv` — live screening output.
  Gitignored.

`backend/purge_partner_data.py` is how the purge was done: archive, read the
archive back, verify the counts, then delete, and restart the hash chain with
an event recording that it happened. Re-read it before doing anything similar.

## 9. If a feed breaks

The likeliest failure. It is quiet: the list stops refreshing while everything
else works, and the dashboard turns the source red after 48 hours.

`app/ingest/endpoints.py` tries the configured URL, then the last one that
worked, then known mirrors, then scrapes the regulator's own download page. Any
candidate must parse with that source's adapter and yield at least 60% of the
records it produced last time.

That floor exists because reachability proves nothing:
`ofac.treasury.gov/media/5926/download` answers 200 with a non-HTML body and is
**not** the SDN list — 199 KB against 5.7 MB. Accepting it would have replaced
the watchlist with something 96% smaller and every screening afterwards would
have come back clean for the wrong reason.

If all of that fails, the adapter needs a person: a **format** change cannot be
recovered automatically. The engine fails safely — it refuses to activate a
snapshot it cannot parse and leaves the previous one in place — but someone has
to fix it. Set `OFAC_SDN_URL` (or the equivalent) in the environment to
override, or drop a manually downloaded copy at the path named in the error.

## 10. Cleaning a finished run without repeating it

A large batch takes hours. If the matching logic is later found to over-alert,
re-scoring the stored alerts is far cheaper than re-running the file:

```bash
cd backend
python rescore_run.py --run-id 9            # dry run: reports, changes nothing
python rescore_run.py --run-id 9 --apply    # archives removed alerts, then deletes them
```

It is only valid while corrections make scoring **stricter**. Re-scoring can
remove alerts but never create them, so if a future change makes matching more
permissive, the run has to be repeated instead. It never touches an alert a
person has resolved, keeps any alert whose designation has since left the list,
archives what it removes to `data/` (gitignored: it holds partner names) and
writes `RUN_RESCORED` to the audit chain with the archive's SHA-256.

The first real use took an 83,642-name Latin American file from 5,435 alerts to
565. Four faults, each pinned in `tests/test_matching_precision.py`:

1. Acronyms were bridged to **individuals** ("AMF SAS" -> four people whose
   initials spell AMF). A person has no acronym.
2. In batch the **reverse** acronym direction paired long partner names with a
   designation's acronym alias because both began A-A-B.
3. `token_set_ratio` scores 100 whenever one name's word SET sits inside the
   other's, and a length check let it through for any similar-length pair.
   The gate now requires the words themselves to overlap.
4. A name of one **generic** word ("INTERNATIONAL", 13 letters) counted as
   discriminative on length alone.

A lesson from doing it: an early dry run reported "0 alerts depend on the
acronym bridge" and was wrong. A short-name cap sat *after* the acronym and
pushed every acronym match below the 95% threshold, which looked like the rule
working. Moving the cap exposed 461 of them. When a number looks too good,
change one thing at a time and re-measure.

### Alias matching is off in batch, on in manual search

`BATCH_ALIAS_LIMIT = 0` in `app/matching.py`. A published alias is often one
common word or a nickname, and tens of thousands of partner names collapse to
exactly such a word once the legal form and generic words are stripped
("JULIA HOLDING E.I.R.L." -> JULIA). In an 83,642-name file this was the largest
remaining source of noise: of 565 alerts, 285 rested on an alias alone.

The cost is a real one: a partner whose legal name equals a designation's
genuine alias, and nothing else, is no longer raised in batch. 167 of the 285
removed had a multi-word alias, the kind most likely to be a real hit; they sit
in `data/rescore-removed-run9-*.csv` for review. If that trade-off is revisited,
the middle path is to keep aliases of two or more distinctive words.

Dropping aliases exposed an older fault: `acronyms_of` knew the abbreviation
"PJSC" but not the written-out "PUBLIC JOINT STOCK COMPANY", so from OFAC's
primary name for United Aircraft Corporation it built PJSCUAC and never UAC. The
match had only ever worked through an alias. Fixed in `normalize.py`, narrowly:
the spelled-out prefixes are consulted when building acronyms only, not in
name normalisation, which would have changed scoring for every name.

### Batch scores are honest: 100 means "the same name"

Batch scoring (not manual search) is `_shared_name_score` in `app/matching.py`: a
weighted Dice coefficient over words, `2 x shared / (partner + target)`, so an
extra word on EITHER name costs points. It replaced `token_set_ratio` (100
whenever one name's words sit inside the other's) and an alignment score that
charged 3 points for a whole unexplained surname. A real file showed 100 on
"ALEXIS RODRIGUEZ" vs "ROMERO RODRIGUEZ, Alexis" and 95 on "KIM SUN JUNG" vs
"KIM, Ung Sun". Rules:

* Only identical words, in any order, may score 100; everything else is held at
  99 or below.
* A near-identical spelling counts only for long words (6+ letters, ratio 90+):
  MOHAMED/MOHAMMED is a transliteration, JUNG/UNG is a different name.
* A prefix counts only for organisations, and only when it is long and most of
  the word (6+ letters, 55%+ of it): GAZPROM -> GAZPROMBANK, but SOUTH is not
  SOUTHFRONT. Never for a person: BELLO is not BELLOSO.
* The character-level ratio is not part of the batch score. On short names it
  rescues a different surname ("BELLO MIGUEL"/"BELLOSO MIGUEL" are 92% alike).
* A name of 3+ distinct words wholly inside another gets a stated floor of 85,
  so "Banco de Venezuela" still reaches OFAC's "... SA BANCO UNIVERSAL".
* Acronyms are capped at 95 in batch.

The country adjustment is OFF by default (`geo_contradiction_penalty = 0`). It
never decided whether an alert was raised; it only reordered the list and made a
95% screen display "85". The country is shown as a label (COUNTRY DIFFERS /
MATCHES / UNKNOWN). It is a different question from name similarity, not a
duplicate of it, so it was neutralised rather than removed.

Alerts for one partner are drawn as one case ("match 2 of 8") instead of
looking like repeated rows.

Re-running the 83,642-name file now takes about 15 minutes (was ~2.5 hours):
aliases are off in batch and nothing competes for the machine.
