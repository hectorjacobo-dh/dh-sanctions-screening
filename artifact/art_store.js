/* ============================================================
   Persistence and the audit chain, in the browser.

   Two stores, for two different reasons:

   * IndexedDB for the watchlist snapshot and the audit chain. localStorage
     caps at roughly 5 MB and stores strings only; a 4.5 MB snapshot does not
     reliably fit, and an append-only log should not share that ceiling.
   * localStorage for the in-progress case, which is small and benefits from
     synchronous reads while typing.

   The audit chain mirrors backend/app/audit.py exactly: each record commits to
   the SHA-256 of its own canonical payload concatenated with the previous
   record's hash. The canonical form must match the server's byte for byte —
   sorted keys, no whitespace — or a record exported here could never be
   re-verified there.
   ============================================================ */
'use strict';

var DB_NAME = 'dh_screening';
var DB_VERSION = 1;
var GENESIS = '0000000000000000000000000000000000000000000000000000000000000000';

function openDb(){
  return new Promise(function(resolve, reject){
    var req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = function(){
      var db = req.result;
      if(!db.objectStoreNames.contains('snapshot')) db.createObjectStore('snapshot');
      if(!db.objectStoreNames.contains('audit')) db.createObjectStore('audit', {keyPath:'sequence'});
    };
    req.onsuccess = function(){ resolve(req.result); };
    req.onerror = function(){ reject(req.error); };
  });
}

function tx(store, mode, fn){
  return openDb().then(function(db){
    return new Promise(function(resolve, reject){
      var t = db.transaction(store, mode);
      var s = t.objectStore(store);
      var result = fn(s);
      t.oncomplete = function(){ resolve(result && result.result !== undefined ? result.result : result); };
      t.onerror = function(){ reject(t.error); };
      t.onabort = function(){ reject(t.error); };
    });
  });
}

/* ---------- watchlist snapshot ---------- */
function saveSnapshot(bundle){
  return tx('snapshot','readwrite',function(s){ s.put(bundle,'current'); });
}
function loadSnapshot(){
  return tx('snapshot','readonly',function(s){ return s.get('current'); })
    .catch(function(){ return null; });
}
function clearSnapshot(){
  return tx('snapshot','readwrite',function(s){ s.delete('current'); });
}

/* ---------- canonical JSON: must byte-match json.dumps(sort_keys=True) ---------- */
function canonicalJson(value){
  if(value === null || value === undefined) return 'null';
  var type = typeof value;
  if(type === 'number') return Number.isFinite(value) ? String(value) : 'null';
  if(type === 'boolean') return value ? 'true' : 'false';
  if(type === 'string') return JSON.stringify(value);
  if(Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  var keys = Object.keys(value).sort();
  return '{' + keys.map(function(k){
    return JSON.stringify(k) + ':' + canonicalJson(value[k]);
  }).join(',') + '}';
}

async function sha256Hex(text){
  var bytes = new TextEncoder().encode(text);
  var digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map(function(b){ return b.toString(16).padStart(2,'0'); }).join('');
}

/* ---------- audit chain ---------- */
function allAudit(){
  return tx('audit','readonly',function(s){ return s.getAll(); })
    .then(function(rows){ return (rows || []).sort(function(a,b){ return a.sequence-b.sequence; }); })
    .catch(function(){ return []; });
}

async function appendAudit(action, payload, actor){
  var rows = await allAudit();
  var last = rows.length ? rows[rows.length-1] : null;
  var record = {
    sequence: last ? last.sequence + 1 : 1,
    timestamp_utc: new Date().toISOString().replace(/\.(\d{3})Z$/, '.$1000+00:00'),
    actor: actor || (typeof CASE !== 'undefined' && CASE ? CASE.officer : 'not stated'),
    action: action,
    entity_ref: (payload && payload.entity_ref) || '',
    payload: payload || {},
    previous_hash: last ? last.record_hash : GENESIS
  };
  // Hash over the same fields, in the same order, as audit.canonical_payload.
  record.record_hash = await sha256Hex(canonicalJson({
    sequence: record.sequence,
    timestamp_utc: record.timestamp_utc,
    actor: record.actor,
    action: record.action,
    entity_ref: record.entity_ref,
    payload: record.payload,
    previous_hash: record.previous_hash
  }));
  await tx('audit','readwrite',function(s){ s.put(record); });
  return record;
}

/* Recompute the whole chain and report the first inconsistency. */
async function verifyAudit(){
  var rows = await allAudit();
  if(!rows.length) return {valid:true, events_checked:0, broken_at:null, detail:'empty chain', head:''};
  var expectedPrev = GENESIS, expectedSeq = 1, checked = 0;
  for(var i=0;i<rows.length;i++){
    var r = rows[i];
    if(r.sequence !== expectedSeq){
      return {valid:false, events_checked:checked, broken_at:r.sequence,
              detail:'sequence gap: expected '+expectedSeq+', found '+r.sequence, head:''};
    }
    if(r.previous_hash !== expectedPrev){
      return {valid:false, events_checked:checked, broken_at:r.sequence,
              detail:'previous_hash does not match the prior record', head:''};
    }
    var recomputed = await sha256Hex(canonicalJson({
      sequence: r.sequence, timestamp_utc: r.timestamp_utc, actor: r.actor,
      action: r.action, entity_ref: r.entity_ref, payload: r.payload,
      previous_hash: r.previous_hash
    }));
    if(recomputed !== r.record_hash){
      return {valid:false, events_checked:checked, broken_at:r.sequence,
              detail:'record contents were altered after write', head:''};
    }
    expectedPrev = r.record_hash; expectedSeq++; checked++;
  }
  return {valid:true, events_checked:checked, broken_at:null, detail:'chain intact', head:expectedPrev};
}

async function clearAudit(){
  await tx('audit','readwrite',function(s){ s.clear(); });
}

/* ---------- snapshot age ---------- */
function snapshotAgeDays(iso){
  if(!iso) return null;
  var then = Date.parse(iso);
  if(isNaN(then)) return null;
  return (Date.now() - then) / 86400000;
}
