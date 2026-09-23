/* ============================================================
   Boot and wiring
   ============================================================ */
'use strict';

/* ---------- theme: three states, Auto follows the OS ---------- */
(function(){
  var THEMES = ['auto','dark','light'];
  var LABEL = {auto:'Theme: Auto', dark:'Theme: Dark', light:'Theme: Light'};
  function apply(t){
    if(t==='auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    try{ localStorage.setItem('dh_art_theme', t); }catch(e){}
    $('themeBtn').textContent = LABEL[t];
  }
  var saved = null;
  try{ saved = localStorage.getItem('dh_art_theme'); }catch(e){}
  apply(THEMES.indexOf(saved)>=0 ? saved : 'auto');
  $('themeBtn').onclick = function(){
    var cur = 'auto';
    try{ cur = localStorage.getItem('dh_art_theme') || 'auto'; }catch(e){}
    apply(THEMES[(THEMES.indexOf(cur)+1) % THEMES.length]);
  };
})();

/* ---------- snapshot: age, import, revert ---------- */
function renderProvenance(){
  var counts = {}, i;
  for(i=0;i<REC.length;i++) counts[REC[i][N_SRC]] = (counts[REC[i][N_SRC]]||0)+1;
  var html = '';
  for(i=0;i<SRC.length;i++){
    html += '<div class="prov"><div class="lbl">'+esc(SRC[i])+'</div><div class="val">'+
            nf(counts[i]||0)+'</div></div>';
  }
  $('prov').innerHTML = html;
  $('nAll').textContent = nf(REC.length);
  $('snapDate').textContent = BUNDLE.snapshot || 'unknown';
  if($('snapDate2')) $('snapDate2').textContent = BUNDLE.snapshot || 'unknown';
}

/* A stale watchlist is the failure this project exists to remove, so it is
   stated loudly rather than left in small print. */
function renderStaleness(){
  var iso = BUNDLE.exported_at_utc || BUNDLE.snapshot;
  var days = snapshotAgeDays(iso);
  var imported = !!BUNDLE.exported_at_utc;
  $('snapSummary').textContent = imported
    ? '(imported, ' + (days !== null ? Math.floor(days) + ' days old' : 'date unknown') + ')'
    : '(built in, ' + (days !== null ? Math.floor(days) + ' days old' : 'date unknown') + ')';

  if(days === null){
    banner($('staleBanner'),'warn',
      '<strong>Snapshot age unknown.</strong> Import a fresh snapshot before relying on a clean result.');
    return;
  }
  if(days >= 7){
    banner($('staleBanner'),'err',
      '<strong>This watchlist is ' + Math.floor(days) + ' days old.</strong> ' +
      'Designations change daily, so a party listed since ' + esc(String(BUNDLE.snapshot)) +
      ' will not be found here. Import a fresh snapshot before treating any result as current.');
  } else if(days >= 2){
    banner($('staleBanner'),'warn',
      'Watchlist is ' + Math.floor(days) + ' days old. Import a fresh snapshot for current results.');
  } else {
    banner($('staleBanner'),'','');
  }
}

$('snapDrop').onclick = function(){ $('snapFile').click(); };
$('snapDrop').ondragover = function(e){ e.preventDefault(); $('snapDrop').style.borderColor = 'var(--accent)'; };
$('snapDrop').ondragleave = function(){ $('snapDrop').style.borderColor = ''; };
$('snapDrop').ondrop = function(e){
  e.preventDefault(); $('snapDrop').style.borderColor = '';
  if(e.dataTransfer.files[0]) importSnapshot(e.dataTransfer.files[0]);
};
$('snapFile').onchange = function(){ if($('snapFile').files[0]) importSnapshot($('snapFile').files[0]); };

async function importSnapshot(file){
  banner($('snapResult'),'info','Reading ' + esc(file.name) + '...');
  try{
    var text = await file.text();
    var bundle = JSON.parse(text);
    /* Validate the shape before trusting it: the wrong file must not leave the
       page with an empty or malformed watchlist and no sign of it. */
    if(!bundle || !Array.isArray(bundle.src) || !Array.isArray(bundle.rec))
      throw new Error('That is not a watchlist snapshot (expected "src" and "rec" arrays).');
    if(!bundle.rec.length) throw new Error('That snapshot contains no designations.');
    var bad = -1;
    for(var j=0;j<bundle.rec.length;j++){
      if(!Array.isArray(bundle.rec[j]) || bundle.rec[j].length !== 8){ bad = j; break; }
    }
    if(bad >= 0) throw new Error('Record ' + bad + ' has the wrong shape; snapshot rejected.');

    var digest = await sha256Hex(text);
    await saveSnapshot(bundle);
    adoptBundle(bundle);
    renderProvenance();
    renderStaleness();
    await appendAudit('SNAPSHOT_IMPORTED', {
      designations: bundle.rec.length,
      snapshot: String(bundle.snapshot || 'unknown'),
      exported_at_utc: String(bundle.exported_at_utc || ''),
      file_sha256: digest,
      source_digests: bundle.source_digests || {}
    });
    banner($('snapResult'),'ok',
      '<strong>Watchlist updated.</strong> ' + nf(bundle.rec.length) +
      ' designations, snapshot ' + esc(String(bundle.snapshot || 'unknown')) +
      '. Recorded in the audit trail with the file digest.');
    if($('qName').value.trim()) runSearch();
    refreshAuditPanel();
  }catch(err){
    banner($('snapResult'),'err', esc(err.message || String(err)));
  }
  $('snapFile').value = '';
}

$('btnSnapReset').onclick = async function(){
  if(!window.confirm('Revert to the snapshot built into this page? The imported one is discarded.')) return;
  await clearSnapshot();
  adoptBundle(BUILTIN);
  renderProvenance();
  renderStaleness();
  await appendAudit('SNAPSHOT_REVERTED', {snapshot: String(BUILTIN.snapshot)});
  banner($('snapResult'),'info','Reverted to the built-in snapshot of ' + esc(String(BUILTIN.snapshot)) + '.');
  if($('qName').value.trim()) runSearch();
  refreshAuditPanel();
};

/* ---------- audit panel ---------- */
async function refreshAuditPanel(){
  var rows = await allAudit();
  $('auditCount').textContent = rows.length
    ? nf(rows.length) + ' event' + (rows.length === 1 ? '' : 's') + ' recorded'
    : 'no events yet';
  if(!rows.length){ $('auditList').innerHTML = ''; return; }
  var recent = rows.slice(-40).reverse();
  $('auditList').innerHTML = '<div class="tablewrap"><table><thead><tr>' +
    '<th>#</th><th>When</th><th>Action</th><th>By</th><th>Hash</th>' +
    '</tr></thead><tbody>' + recent.map(function(r){
      return '<tr><td class="wid">' + r.sequence + '</td>' +
        '<td class="wid">' + esc(berlin(new Date(r.timestamp_utc))) + '</td>' +
        '<td><span class="clip">' + esc(r.action) + '</span></td>' +
        '<td class="dim" style="font-size:12px">' + esc(r.actor) + '</td>' +
        '<td class="wid">' + esc(r.record_hash.slice(0,16)) + '...</td></tr>';
    }).join('') + '</tbody></table></div>' +
    (rows.length > 40 ? '<p class="meta">Showing the most recent 40 of ' + nf(rows.length) +
      '. The exported record carries the full chain.</p>' : '');
}

$('btnVerify').onclick = async function(){
  var btn = $('btnVerify');
  btn.disabled = true; btn.textContent = 'Verifying...';
  var v = await verifyAudit();
  banner($('verifyResult'), v.valid ? 'ok' : 'err',
    '<strong>' + (v.valid ? 'Chain intact' : 'TAMPERING DETECTED') + '</strong>' +
    '<div>' + nf(v.events_checked) + ' event(s) verified - ' + esc(v.detail) +
    (v.broken_at !== null && v.broken_at !== undefined ? ' (first break at #' + v.broken_at + ')' : '') +
    '</div>' + (v.head ? '<div class="mono" style="margin-top:6px;font-size:11px">head ' +
      esc(v.head.slice(0,40)) + '...</div>' : ''));
  btn.disabled = false; btn.textContent = 'Verify chain';
  refreshAuditPanel();
};

/* ---------- examples ---------- */
['Banco de Venezuela','gazprom','UAC','Rosneft Oil Co','Prince Super Market Cambodia',
 'Bank Melli','Bella Napoli Pizzeria Hamburg'].forEach(function(name){
  var b = document.createElement('button');
  b.type='button'; b.className='chip'; b.textContent=name;
  b.onclick = function(){ $('qName').value = name; runSearch(); };
  $('chips').appendChild(b);
});

/* ---------- search controls ---------- */
$('go').onclick = runSearch;
$('qName').onkeydown = function(e){ if(e.key==='Enter') runSearch(); };
['qDob','qNat','qCountry','qId'].forEach(function(id){
  $(id).onkeydown = function(e){ if(e.key==='Enter') runSearch(); };
});
$('qTh').oninput = function(){ $('thv').textContent = $('qTh').value + '%'; };
$('qTh').onchange = function(){ if($('qName').value.trim()) runSearch(); };
document.querySelectorAll('.qST').forEach(function(c){
  c.onchange = function(){ if($('qName').value.trim()) runSearch(); };
});

/* ---------- case ---------- */
$('btnCase').onclick = openCase;

$('btnTxt').onclick = async function(){
  if(!CASE) return;
  var text = await recordLines();
  download('DH_Due_Diligence_'+slug(CASE.query)+'.txt', text);
  await appendAudit('RECORD_EXPORTED', {
    format: 'txt', bytes: text.length,
    export_sha256: await sha256Hex(text),
    query: CASE.query,
    reviewed: CASE.hits.filter(function(h){ return h.decision; }).length,
    outstanding: CASE.hits.filter(function(h){ return !h.decision; }).length
  });
  refreshAuditPanel();
};

$('btnPdf').onclick = async function(){
  if(!CASE) return;
  var html = await printableHtml();
  await appendAudit('RECORD_EXPORTED', {
    format: 'print', bytes: html.length,
    export_sha256: await sha256Hex(html),
    query: CASE.query,
    reviewed: CASE.hits.filter(function(h){ return h.decision; }).length,
    outstanding: CASE.hits.filter(function(h){ return !h.decision; }).length
  });
  refreshAuditPanel();
  var w = window.open('', '_blank');
  if(w && w.document){
    w.document.open(); w.document.write(html); w.document.close();
    $('exportNote').innerHTML = '';
    setTimeout(function(){ try{ w.focus(); }catch(e){} }, 200);
  } else {
    // Popups blocked (common inside an embedded frame) — hand over the file so
    // the officer can still open and print it.
    download('DH_Due_Diligence_'+slug(CASE.query)+'.html', html, 'text/html;charset=utf-8');
    $('exportNote').innerHTML = '<div class="banner info">Your browser blocked the new window, '+
      'so the record was downloaded as an HTML file instead. Open it and press Ctrl+P '+
      '(Cmd+P on Mac), then choose “Save as PDF”.</div>';
  }
};

$('btnReset').onclick = function(){
  if(!CASE) return;
  var undecided = CASE.hits.filter(function(h){ return !h.decision; }).length;
  var msg = undecided
    ? 'Discard this case? '+undecided+' alert(s) are still unreviewed and all adjudications '+
      'and attached documents will be lost.'
    : 'Discard this case? All adjudications and attached documents will be lost.';
  if(!window.confirm(msg)) return;
  CASE = null;
  try{ localStorage.removeItem(STORE_KEY); }catch(e){}
  $('stage2').classList.add('hide');
  $('exportNote').innerHTML = '';
};

/* ---------- start ---------- */
setTimeout(async function(){
  // An imported snapshot wins over the built-in one: it is newer by definition,
  // since the only way to get one is to export it from the hosted engine.
  try{
    var saved = await loadSnapshot();
    if(saved && Array.isArray(saved.rec) && saved.rec.length) adoptBundle(saved);
    else buildIndex();
  }catch(e){ buildIndex(); }

  var t0 = performance.now();
  if(!Object.keys(tokenIndex).length) buildIndex();
  var ms = Math.round(performance.now() - t0);

  renderProvenance();
  renderStaleness();
  refreshAuditPanel();

  $('boot').style.display = 'none';
  $('go').disabled = false;
  $('go').textContent = 'Screen name';

  var savedCase = loadCase();
  if(savedCase){
    CASE = savedCase;
    $('qName').value = CASE.query;
    $('qTh').value = CASE.threshold;
    $('thv').textContent = CASE.threshold + '%';
    $('qDob').value = CASE.dob || '';
    $('qNat').value = CASE.nationality || '';
    $('qCountry').value = CASE.country || '';
    $('qId').value = CASE.nationalId || '';
    $('qRef').value = CASE.reference || '';
    $('qNote').value = CASE.note || '';
    $('qOfficer').value = CASE.officer === 'not stated' ? '' : (CASE.officer || '');
    document.querySelectorAll('.qST').forEach(function(c){
      c.checked = CASE.subjectTypes.indexOf(c.value) >= 0;
    });
    renderCase();
    $('stage2').classList.remove('hide');
    $('meta').innerHTML = '<b>Restored a case in progress</b> - ' + esc(CASE.query) +
      ', opened ' + esc(berlin(new Date(CASE.created))) +
      '. Screen again to refresh the alert list, or carry on below.';
  } else {
    $('qName').value = 'Banco de Venezuela';
    runSearch();
    $('meta').innerHTML += ' | index built in ' + ms + ' ms';
  }
  $('qName').focus();
}, 30);
