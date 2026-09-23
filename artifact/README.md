# Shareable case workbench (artifact sources)

Assembles into a single self-contained HTML file that runs the screening
workflow entirely in a browser — no server, no uploads, no network calls after
load.

## Rebuild

```powershell
# 1. Export the current watchlist snapshot and the reason taxonomy
cd ..\backend
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe ..\artifact\export_data.py ..\artifact

# 2. Assemble, with guards
.\.venv\Scripts\python.exe ..\artifact\assemble.py ..\artifact
```

Produces `artifact/workbench.html`.

## Then verify it in a browser — do not skip this

The first published version was **completely broken** and looked fine to every
static check: a regex literal written with raw control characters compiled to
"Range out of order in character class", which aborts the whole script before a
single line runs. Nothing renders, no error is visible on the page.

Serve the file and confirm in the console:

```js
typeof normalize === 'function' && REC.length   // expect 57573
```

If the script silently did not run, get the real reason with:

```js
fetch(location.href).then(r=>r.text()).then(h=>{
  const src = h.match(/<script>([\s\S]*)<\/script>/)[1];
  try { (new Function(src))(); return 'ok'; } catch(e){ return e.message; }
});
```

## Files

| File | Purpose |
|---|---|
| `art_shell.html` | Markup, design tokens, both themes |
| `art_engine.js`  | Normalisation, scoring, blocking — ported from `app/normalize.py`, `app/matching.py`, `app/blocking.py` |
| `art_docs.js`    | Document text extraction and cross-check — ported from `app/doc_extract.py`, `app/doc_crosscheck.py` |
| `art_ui.js`      | Case state, rendering, record generation |
| `art_boot.js`    | Wiring, theme, restore-in-progress-case |
| `assemble.py`    | Concatenates, embeds data as JSON islands, runs guards |
| `taxonomy.json`  | Reason codes exported from `app/dismissal.py` — never hand-edited |

## Guards in `assemble.py`

Each one exists because that defect actually shipped:

1. **`const`/`let` shadowing a parameter** — killed the local UI at parse time.
2. **Raw bytes in a regex character class** — killed the first artifact at run time.
3. Data embedded as a **JSON island**, not a JS object literal — a 4.5 MB
   literal took the compiler tens of seconds before anything executed; with
   `JSON.parse` the index builds in ~1.2 s.
4. Partner-data leak check, and a check that the data cannot close its own
   `<script>` tag.

## Parity

The artifact must return the same counts as the server for these:

| Query | Threshold | Expected |
|---|---|---|
| `UAC` | 80% | 1 (PJSC United Aircraft Corporation @100) |
| `gazprom` | 90% | 68 |
| `Banco de Venezuela` | 90% | 3 |
| `Pizza` | 90% | 2 |
| `Bella Napoli Pizzeria Hamburg` | 90% | 0 |

A mismatch means the port has drifted from the Python engine.
