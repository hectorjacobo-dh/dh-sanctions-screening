/* ============================================================
   UI + case state + record export
   ============================================================ */
'use strict';

function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
}
function nf(n){ return Number(n||0).toLocaleString('en-GB'); }

/* Inline message box. Lives here with the other DOM helpers so every module
   can reach it — it was previously assumed to exist and did not, which threw
   ReferenceError inside the startup path and silently aborted the rest of it. */
function banner(el, kind, html){
  if(!el) return;
  el.innerHTML = html ? '<div class="banner ' + kind + '">' + html + '</div>' : '';
}
var TZ = 'Europe/Berlin';
function berlin(d){
  try{
    return new Intl.DateTimeFormat('en-GB',{timeZone:TZ,year:'numeric',month:'short',
      day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZoneName:'short'})
      .format(d).replace(',','');
  }catch(e){ return d.toISOString(); }
}
function isoUtc(d){ return d.toISOString().replace(/\.\d{3}Z$/,'Z'); }

/* ---------- case state (browser-local only) ---------- */
var CASE = null;
var STORE_KEY = 'dh_screening_case_v1';

function newCase(query){
  return {
    query: query,
    created: new Date().toISOString(),
    threshold: Number($('qTh').value),
    subjectTypes: Array.from(document.querySelectorAll('.qST:checked')).map(function(c){return c.value;}),
    dob: $('qDob').value.trim(), nationality: $('qNat').value.trim(),
    country: $('qCountry').value.trim(), nationalId: $('qId').value.trim(),
    reference: $('qRef').value.trim(), note: $('qNote').value.trim(),
    officer: ($('qOfficer').value.trim() || 'not stated'),
    hits: []
  };
}
function saveCase(){
  try{ localStorage.setItem(STORE_KEY, JSON.stringify(CASE)); }catch(e){}
}
function loadCase(){
  try{
    var raw = localStorage.getItem(STORE_KEY);
    if(!raw) return null;
    var c = JSON.parse(raw);
    return (c && c.query) ? c : null;
  }catch(e){ return null; }
}

/* ---------- search ---------- */
function activeTypes(){
  var boxes = document.querySelectorAll('.qST'), on = {}, any = false;
  for(var j=0;j<boxes.length;j++) if(boxes[j].checked){ on[boxes[j].value]=1; any=true; }
  return any ? on : null;
}

function runSearch(){
  var raw = $('qName').value.trim();
  if(!raw){ $('meta').textContent=''; $('out').innerHTML=''; return; }
  var threshold = Number($('qTh').value);
  var qn = normalize(raw), qTok = qn ? qn.split(' ') : [];
  var types = activeTypes();
  var t0 = performance.now();
  var ids = candidates(raw, qn), hits = [], j, rec, sc;
  for(j=0;j<ids.length;j++){
    rec = REC[ids[j]];
    if(types && !types[rec[N_TYPE]]) continue;
    sc = score(raw, qn, qTok, rec[N_NAME], normalize(rec[N_NAME]));
    if(sc && sc.base >= threshold) hits.push({ idx: ids[j], s: sc.base, c: sc.codes });
  }
  hits.sort(function(a,b){ return b.s-a.s || (REC[a.idx][N_NAME] < REC[b.idx][N_NAME] ? -1 : 1); });
  var ms = (performance.now()-t0).toFixed(0);

  $('meta').innerHTML = '<b>'+esc(raw)+'</b> normalised to <b>'+esc(qn||'—')+'</b> · '+
    nf(ids.length)+' candidates scored of '+nf(REC.length)+' designations · <b>'+
    nf(hits.length)+'</b> at or above '+threshold+'% · '+ms+' ms';

  window.LAST_HITS = hits;
  // A search is itself a compliance act: it evidences that a name was checked
  // on a given date against a given snapshot.
  appendAudit('NAME_SEARCH', {
    query: raw, normalized: qn, threshold: threshold,
    subject_types: Object.keys(types || {}),
    candidates_scored: ids.length, hits: hits.length,
    watchlist_snapshot: String(BUNDLE.snapshot || 'unknown'),
    designations: REC.length
  }).then(refreshAuditPanel);
  renderHits(hits.slice(0,60), hits.length);
  $('caseBar').classList.remove('hide');
  $('caseSummary').textContent = hits.length
    ? hits.length + ' alert(s) to adjudicate'
    : 'No designation matched — record it as a clean result';
}

function bandOf(s){ return s>=95 ? 'strong' : 'potential'; }

function renderHits(shown, total){
  if(!shown.length){
    $('out').innerHTML = '<div class="verdict"><div class="h">No match at '+
      $('qTh').value+'%</div><p>Nothing in the watchlist scores at or above the threshold. '+
      'Lower the fuzziness slider to widen the search before treating a name as clear. '+
      'Create the case anyway — "screened and clear" is the evidence due diligence exists '+
      'to produce, and it is worthless unrecorded.</p></div>';
    return;
  }
  var rows = shown.map(function(h){
    var r = REC[h.idx];
    return '<tr>'+
      '<td class="sev s-'+bandOf(h.s)+'"></td>'+
      '<td><div class="score">'+h.s.toFixed(0)+'</div><div class="band s-'+bandOf(h.s)+'">'+
        (h.s>=95?'Strong match':'Potential match')+'</div></td>'+
      '<td><div class="tname">'+esc(r[N_NAME])+'</div>'+
        (r[N_PROG]?'<div class="tprog">'+esc(r[N_PROG])+'</div>':'')+
        (r[N_AKA]?'<div class="taka">aka '+esc(r[N_AKA])+'</div>':'')+
        '<div class="codes">'+h.c.map(function(c){return '<span>'+esc(c)+'</span>';}).join('')+'</div></td>'+
      '<td><span class="pill t-'+esc(r[N_TYPE])+'">'+esc(TYPE_FULL[r[N_TYPE]]||'Unknown')+'</span></td>'+
      '<td><div class="src">'+esc(SRC[r[N_SRC]])+'</div>'+
        (r[N_WID]?'<div class="wid">'+esc(r[N_WID])+'</div>':'')+'</td>'+
      '<td><div class="geo">'+esc(r[N_GEO]||'—')+'</div>'+
        (r[N_DOB]?'<div class="wid">b. '+esc(r[N_DOB])+'</div>':'')+'</td>'+
    '</tr>';
  }).join('');
  $('out').innerHTML = '<div class="tablewrap"><table><thead><tr><th></th><th>Score</th>'+
    '<th>Designated party</th><th>Type</th><th>Source</th><th>Geography</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>'+
    (total>shown.length ? '<p class="meta">Showing the top '+shown.length+' of '+nf(total)+
      ' — raise the threshold to narrow.</p>' : '');
}

/* ---------- open the case for adjudication ---------- */
function openCase(){
  var raw = $('qName').value.trim();
  if(!raw) return;
  CASE = newCase(raw);
  (window.LAST_HITS||[]).slice(0,60).forEach(function(h){
    var r = REC[h.idx];
    CASE.hits.push({
      id: 'h'+h.idx,
      name: r[N_NAME], source: SRC[r[N_SRC]], type: TYPE_FULL[r[N_TYPE]]||'Unknown',
      programme: r[N_PROG], geography: r[N_GEO], dob: r[N_DOB], watchlistId: r[N_WID],
      aka: r[N_AKA], score: h.s, codes: h.c,
      decision:'', reason:'', rationale:'', grc:'', decidedAt:'', docs:[]
    });
  });
  saveCase();
  appendAudit('CASE_OPENED', {
    query: CASE.query, reference: CASE.reference, note: CASE.note,
    threshold: CASE.threshold, alerts: CASE.hits.length,
    identity_given: [CASE.dob, CASE.nationality, CASE.country, CASE.nationalId]
      .filter(Boolean).join(' | ') || 'none'
  }).then(refreshAuditPanel);
  renderCase();
  $('stage2').classList.remove('hide');
  $('stage2').scrollIntoView({behavior:'smooth', block:'start'});
}

function reasonOptions(selected, decision){
  var want = decision==='False Match' ? ['dismiss','any']
           : decision==='Positive Match' ? ['confirm','any']
           : ['dismiss','confirm','any'];
  return '<option value="">— select grounds —</option>' + TAX.reasons
    .filter(function(r){ return want.indexOf(r.applies_to)>=0; })
    .map(function(r){ return '<option value="'+esc(r.code)+'"'+(r.code===selected?' selected':'')+
      '>'+esc(r.label)+'</option>'; }).join('');
}
function guidanceFor(code){
  var r = TAX.reasons.filter(function(x){ return x.code===code; })[0];
  return r ? r.guidance : '';
}

function renderCase(){
  if(!CASE) return;
  var reviewed = CASE.hits.filter(function(h){ return h.decision; }).length;
  $('caseHead').innerHTML =
    '<div><strong>'+esc(CASE.query)+'</strong>'+
    (CASE.reference ? ' <span class="pill">'+esc(CASE.reference)+'</span>' : '')+
    '<div class="dim" style="font-size:12.5px;margin-top:4px">'+
      'threshold '+CASE.threshold+'% · scope '+esc(CASE.subjectTypes.join(', ')||'all')+
      ' · opened '+esc(berlin(new Date(CASE.created)))+
      ' · officer '+esc(CASE.officer)+'</div></div>'+
    '<div class="dim" style="font-size:12.5px">'+reviewed+' of '+CASE.hits.length+' adjudicated</div>';

  if(!CASE.hits.length){
    $('caseBody').innerHTML = '<div class="verdict"><div class="h">Clean result</div>'+
      '<p>No designation matched at '+CASE.threshold+'%. Export the record below — it states '+
      'the finding and its limits.</p></div>';
    return;
  }

  $('caseBody').innerHTML = CASE.hits.map(function(h, i){
    return '<div class="alert" data-h="'+esc(h.id)+'">'+
      '<div class="ahead">'+
        '<span class="score-chip s-'+bandOf(h.score)+'">'+h.score.toFixed(0)+'</span>'+
        '<div style="flex:1">'+
          '<div class="tname">'+esc(h.name)+'</div>'+
          '<div class="dim" style="font-size:12px">'+esc(h.source)+
            (h.watchlistId?' · '+esc(h.watchlistId):'')+' · '+esc(h.type)+
            (h.geography?' · '+esc(h.geography):'')+(h.dob?' · b. '+esc(h.dob):'')+'</div>'+
        '</div>'+
        '<span class="statechip '+(h.decision?'done':'')+'">'+
          esc(h.decision||'unreviewed')+'</span>'+
      '</div>'+
      '<div class="agrid">'+
        '<div>'+
          '<label class="fl">Decision</label>'+
          '<select data-f="decision">'+
            ['','Positive Match','Potential Match','False Match'].map(function(d){
              return '<option value="'+esc(d)+'"'+(d===h.decision?' selected':'')+'>'+
                     (d?esc(d):'Unreviewed')+'</option>'; }).join('')+
          '</select>'+
          '<label class="fl" style="margin-top:10px">Grounds</label>'+
          '<select data-f="reason">'+reasonOptions(h.reason, h.decision)+'</select>'+
          '<div class="guide" data-f="guide">'+esc(guidanceFor(h.reason))+'</div>'+
        '</div>'+
        '<div>'+
          '<label class="fl">Rationale — what you checked and concluded</label>'+
          '<textarea data-f="rationale" rows="4" placeholder="e.g. Verified trade licence 00123; incorporated 2019; UBO differs from the designated party.">'+esc(h.rationale)+'</textarea>'+
          '<label class="fl" style="margin-top:10px">Regional GRC input</label>'+
          '<textarea data-f="grc" rows="2">'+esc(h.grc)+'</textarea>'+
        '</div>'+
        '<div class="full">'+
          '<label class="fl">Evidence documents — read and cross-checked automatically</label>'+
          '<div data-f="docs">'+docsHtml(h)+'</div>'+
          '<div class="evdrop" data-f="drop">Attach a document — PDF, Word, Excel, email or text'+
            '<div class="dim" style="font-size:11.5px;margin-top:4px">Read in your browser and '+
            'compared against this designation. The file never leaves this machine. '+
            'Scans and photographs cannot be read without OCR and are flagged as such.</div></div>'+
          '<input type="file" data-f="file" class="hide" '+
            'accept=".pdf,.docx,.pptx,.xlsx,.xlsm,.txt,.csv,.md,.eml,.png,.jpg,.jpeg" />'+
          '<div data-f="err"></div>'+
          '<div class="row" style="margin-top:10px">'+
            '<button class="primary" data-f="save">Save this adjudication</button>'+
            '<span class="dim" data-f="state"></span>'+
          '</div>'+
        '</div>'+
      '</div>'+
    '</div>';
  }).join('');

  CASE.hits.forEach(function(h){ wireAlert(h); });
}

function docsHtml(h){
  if(!h.docs.length) return '<span class="dim" style="font-size:12.5px">No documents attached.</span>';
  return h.docs.map(function(d){
    var a = d.analysis || {};
    var cls = a.readable ? (a.confidence==='none' ? 'neutral' : 'corroborates') : 'unreadable';
    return '<div class="docwrap">'+
      '<div class="doc"><strong>'+esc(d.name)+'</strong>'+
        '<span class="dim">'+(d.size/1024).toFixed(0)+' KB · '+esc(d.at)+'</span>'+
        '<span class="h">sha256 '+esc((d.sha256||'').slice(0,24))+'…</span></div>'+
      '<div class="xc">'+
        '<div class="rec"><span class="vd '+cls+'">'+
          esc(a.readable ? 'read '+nf(d.chars)+' chars' : 'not readable')+'</span>'+
          '<span style="margin-left:8px">'+esc(a.recommendation||'')+'</span></div>'+
        (a.findings||[]).map(function(f){ return '<div class="f">'+
          '<span class="k">'+esc(f.check)+'</span>'+
          '<span class="vd '+esc(f.verdict)+'">'+esc(f.verdict)+'</span>'+
          '<span class="dim">'+esc(f.detail)+'</span></div>'; }).join('')+
        (a.suggested_reason_code ? '<div class="f"><span class="k">Suggested grounds</span>'+
          '<span class="clip">'+esc(a.suggested_reason_code)+'</span>'+
          '<span class="dim">You must still confirm this and state the rationale yourself.</span></div>' : '')+
      '</div></div>';
  }).join('');
}

/* Named for its input: art_store.js owns sha256Hex(string) for the audit
   chain. Two functions of the same name in different modules meant the later
   definition won and the chain hashed a string with the buffer variant. */
async function sha256Buffer(buf){
  var digest = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(digest)).map(function(b){
    return b.toString(16).padStart(2,'0'); }).join('');
}

function wireAlert(h){
  var el = document.querySelector('.alert[data-h="'+h.id+'"]');
  if(!el) return;
  var q = function(f){ return el.querySelector('[data-f="'+f+'"]'); };

  q('decision').onchange = function(){
    h.decision = q('decision').value;
    q('reason').innerHTML = reasonOptions(h.reason, h.decision);
    saveCase();
  };
  q('reason').onchange = function(){
    h.reason = q('reason').value;
    q('guide').textContent = guidanceFor(h.reason);
    saveCase();
  };
  q('rationale').oninput = function(){ h.rationale = q('rationale').value; saveCase(); };
  q('grc').oninput = function(){ h.grc = q('grc').value; saveCase(); };

  q('drop').onclick = function(){ q('file').click(); };
  q('file').onchange = async function(){
    var f = q('file').files[0];
    if(!f) return;
    q('err').innerHTML = '';
    q('state').textContent = 'reading ' + f.name + '…';
    try{
      if(f.size > 25*1024*1024) throw new Error('Documents are limited to 25 MB.');
      var buf = await f.arrayBuffer();
      var hash = await sha256Buffer(buf);
      var ex = await extractDocument(f);
      var analysis = crossCheck({
        text: ex.text, status: ex.status, detail: ex.detail,
        partnerName: CASE.query, targetName: h.name, targetDob: h.dob,
        targetGeography: h.geography, targetWatchlistId: h.watchlistId,
        targetRemarks: h.programme
      });
      h.docs.push({ name: f.name, size: f.size, sha256: hash,
                    at: berlin(new Date()), chars: (ex.text||'').length, analysis: analysis });
      saveCase();
      appendAudit('EVIDENCE_ATTACHED', {
        entity_ref: h.id,
        filename: f.name, bytes: f.size, sha256: hash,
        extraction_status: ex.status, extracted_chars: (ex.text||'').length,
        cross_check: analysis
      }).then(refreshAuditPanel);
      q('docs').innerHTML = docsHtml(h);
      q('state').textContent = '';
    }catch(err){
      q('err').innerHTML = '<div class="banner err">'+esc(err.message||String(err))+'</div>';
      q('state').textContent = '';
    }
    q('file').value = '';
  };

  q('save').onclick = function(){
    var problem = validateAdjudication(h.decision, h.reason, h.rationale);
    if(problem){
      q('err').innerHTML = '<div class="banner err">'+esc(problem)+'</div>';
      return;
    }
    q('err').innerHTML = '';
    h.decidedAt = new Date().toISOString();
    saveCase();
    appendAudit('ADJUDICATION_RECORDED', {
      entity_ref: h.id,
      partner: CASE.query, designation: h.name, source: h.source,
      watchlist_id: h.watchlistId, score: h.score,
      decision: h.decision, reason_code: h.reason,
      rationale: h.rationale, grc_input: h.grc,
      evidence_sha256: h.docs.map(function(d){ return d.sha256; })
    }).then(refreshAuditPanel);
    renderCase();
  };
}

/* ---------- the record ---------- */
async function recordLines(){
  var W = 78, out = [], rule = function(c){ return (c||'-').repeat(W); };
  var wrap = function(label, value, indent){
    indent = indent||16;
    value = String(value==null||value===''?'—':value);
    var words = value.split(' '), lines = [], cur = '';
    words.forEach(function(w){
      if((cur+' '+w).trim().length > W-indent){ lines.push(cur.trim()); cur = w; }
      else cur = (cur+' '+w).trim();
    });
    if(cur) lines.push(cur.trim());
    if(!lines.length) lines = ['—'];
    var pad = ' '.repeat(indent);
    var first = label.length < indent ? label + ' '.repeat(indent-label.length) + lines[0]
                                      : label + ' ' + lines[0];
    return [first].concat(lines.slice(1).map(function(l){ return pad+l; })).join('\n');
  };

  var now = new Date();
  var reviewed = CASE.hits.filter(function(h){ return h.decision; });
  var outstanding = CASE.hits.filter(function(h){ return !h.decision; });
  var ungrounded = reviewed.filter(function(h){ return !h.reason; });

  out.push(rule('='));
  out.push('DUE DILIGENCE RECORD'.padStart(Math.floor((W+20)/2)));
  out.push('Delivery Hero Group — Sanctions Screening'.padStart(Math.floor((W+41)/2)));
  out.push('Manual name screening (browser demonstrator)'.padStart(Math.floor((W+44)/2)));
  out.push(rule('='));
  out.push('');
  out.push(wrap('Party screened:', CASE.query));
  out.push(wrap('Name as matched:', normalize(CASE.query)));
  if(CASE.reference) out.push(wrap('Internal ref:', CASE.reference));
  var identity = [CASE.dob?'DOB '+CASE.dob:'', CASE.nationality?'nationality '+CASE.nationality:'',
                  CASE.country?'country '+CASE.country:'', CASE.nationalId?'ID '+CASE.nationalId:'']
                 .filter(Boolean).join(' · ');
  out.push(wrap('Identity given:', identity||'none'));
  if(CASE.note) out.push(wrap('Reason for check:', CASE.note));
  out.push(wrap('Screened by:', CASE.officer));
  out.push(wrap('Screened:', berlin(new Date(CASE.created))+'  ('+isoUtc(new Date(CASE.created))+')'));
  out.push(wrap('Record made:', berlin(now)+'  ('+isoUtc(now)+')'));
  out.push(wrap('Threshold:', CASE.threshold+'% similarity'));
  out.push(wrap('Scope:', CASE.subjectTypes.join(', ')||'all subject types'));
  out.push(wrap('Alerts raised:', String(CASE.hits.length)));
  out.push('');
  out.push('Watchlist snapshot screened against:');
  out.push('    '+nf(REC.length)+' designations, snapshot of '+BUNDLE.snapshot);
  SRC.forEach(function(s, i){
    var n = REC.filter(function(r){ return r[N_SRC]===i; }).length;
    out.push('    '+s.padEnd(16)+String(nf(n)).padStart(8)+' designations');
  });
  out.push('');
  out.push(rule());
  out.push('REVIEW SUMMARY');
  out.push(rule());
  out.push(wrap('Reviewed:', reviewed.length+' of '+CASE.hits.length+' alerts'));
  var byDecision = {};
  reviewed.forEach(function(h){ byDecision[h.decision] = (byDecision[h.decision]||0)+1; });
  Object.keys(byDecision).sort().forEach(function(d){
    out.push('    '+d.padEnd(24)+String(byDecision[d]).padStart(6));
  });
  if(outstanding.length) out.push('    '+'UNREVIEWED'.padEnd(24)+String(outstanding.length).padStart(6)+'   <-- outstanding');
  if(ungrounded.length){
    out.push('');
    out.push('!!  '+ungrounded.length+' decision(s) carry NO stated grounds.');
    out.push('    These are not defensible as recorded and should be revisited:');
    ungrounded.forEach(function(h){ out.push('      '+h.decision.padEnd(16)+h.name.slice(0,44)); });
  }
  out.push('');

  if(!CASE.hits.length){
    out.push(rule('='));
    out.push('RESULT: NO DESIGNATION MATCHED');
    out.push(rule('='));
    out.push(wrap('Finding:', CASE.query+' was screened against the watchlist snapshot recorded '+
      'above and produced no match at or above the '+CASE.threshold+'% similarity threshold, '+
      'within the scope \''+(CASE.subjectTypes.join(', ')||'all')+'\'.'));
    out.push('');
    out.push('Limits of this finding — it is valid only for:');
    out.push('    · the threshold applied ('+CASE.threshold+'%); a lower one may match');
    out.push('    · the subject types screened ('+(CASE.subjectTypes.join(', ')||'all')+')');
    out.push('    · the snapshot of '+BUNDLE.snapshot+'; designations change');
    out.push('    · the name and identifiers as supplied, exactly as given');
    out.push('');
  }

  if(reviewed.length){
    out.push(rule('='));
    out.push('ADJUDICATED ALERTS');
    out.push(rule('='));
    reviewed.forEach(function(h, i){
      out.push('');
      out.push('['+(i+1)+'] '+h.decision.toUpperCase());
      out.push(rule());
      out.push(wrap('Partner:', CASE.query));
      out.push(wrap('Designation:', h.name));
      out.push(wrap('List / ref:', h.source+' · '+(h.watchlistId||'—')+' · '+h.type));
      if(h.programme) out.push(wrap('Programme:', h.programme));
      if(h.geography||h.dob) out.push(wrap('Designation data:', (h.geography||'no country')+' · '+(h.dob||'no DOB')));
      out.push(wrap('Match score:', h.score.toFixed(0)+'%'));
      if(h.codes && h.codes.length) out.push(wrap('Engine reasons:', h.codes.join(', ')));
      out.push('');
      out.push(wrap('Grounds:', h.reason||'NOT STATED'));
      if(h.reason){
        var r = TAX.reasons.filter(function(x){ return x.code===h.reason; })[0];
        if(r) out.push(wrap('', r.label));
      }
      out.push(wrap('Rationale:', h.rationale));
      if(h.grc) out.push(wrap('Regional GRC:', h.grc));
      if(h.docs.length){
        out.push(wrap('Evidence:', h.docs.length+' document(s)'));
        h.docs.forEach(function(d){
          out.push('                - '+d.name);
          out.push('                  sha256 '+d.sha256);
          out.push('                  '+nf(d.size)+' bytes, read '+d.at);
          var a = d.analysis||{};
          if(!a.readable){
            out.push(wrap('', 'AUTOMATIC CROSS-CHECK: NOT READABLE — '+(a.extraction_detail||''), 18));
          } else {
            out.push(wrap('', 'automatic cross-check ('+nf(d.chars)+' chars read, confidence '+
                              (a.confidence||'none')+'):', 18));
            (a.findings||[]).forEach(function(f){
              out.push(wrap('', '['+f.verdict+'] '+f.check+' — '+f.detail, 20));
            });
          }
          out.push(wrap('', 'engine advised: '+(a.recommendation||''), 18));
        });
      } else {
        out.push(wrap('Evidence:', 'none attached'));
      }
      out.push(wrap('Decided by:', CASE.officer));
      out.push(wrap('Decided at:', h.decidedAt ? berlin(new Date(h.decidedAt))+'  ('+isoUtc(new Date(h.decidedAt))+')' : '—'));
    });
  }

  if(outstanding.length){
    out.push('');
    out.push(rule('='));
    out.push('OUTSTANDING — '+outstanding.length+' ALERT(S) NOT YET REVIEWED');
    out.push(rule('='));
    out.push('These alerts carry no decision. The review is incomplete and this');
    out.push('record must not be represented as a completed screening.');
    out.push('');
    outstanding.forEach(function(h){
      out.push('  '+String(h.score.toFixed(0)).padStart(5)+'%  '+h.name.slice(0,44).padEnd(46)+'['+h.source+']');
    });
  }

  // ---- audit chain -------------------------------------------------------
  var chain = await allAudit();
  var verdict = await verifyAudit();

  out.push('');
  out.push(rule('='));
  out.push('AUDIT TRAIL');
  out.push(rule('='));
  out.push(wrap('Verification:', verdict.valid
    ? 'CHAIN INTACT - ' + verdict.events_checked + ' event(s) recomputed and matched'
    : 'BROKEN at #' + verdict.broken_at + ' - ' + verdict.detail));
  if(verdict.head) out.push(wrap('Head hash:', verdict.head));
  out.push('');
  out.push('Each entry commits to the SHA-256 of the entry before it. Recompute the');
  out.push('chain from the records below to confirm none was altered or removed.');
  out.push('');
  chain.forEach(function(r){
    out.push('  #' + String(r.sequence).padStart(4) + '  ' + r.timestamp_utc + '  ' + r.action);
    out.push('        by   ' + r.actor);
    if(r.entity_ref) out.push('        ref  ' + r.entity_ref);
    out.push('        prev ' + r.previous_hash);
    out.push('        hash ' + r.record_hash);
    var keys = Object.keys(r.payload || {}).sort();
    keys.forEach(function(k){
      var v = r.payload[k];
      if(v === null || v === undefined || v === '' ) return;
      var text = (typeof v === 'object') ? JSON.stringify(v) : String(v);
      if(text.length > 150) text = text.slice(0,150) + '...';
      out.push('        ' + (k + ':').padEnd(22) + text);
    });
    out.push('');
  });

  out.push(rule('='));
  out.push('SCOPE OF THIS DOCUMENT');
  out.push(rule('='));
  out.push('WHAT IT ESTABLISHES');
  out.push('  Which names were screened, against which watchlist snapshot, at');
  out.push('  which threshold, on what grounds each alert was resolved, what');
  out.push('  evidence was relied on (by SHA-256), and who decided. The chain');
  out.push('  above makes an edited or deleted entry detectable by recomputation.');
  out.push('');
  out.push('WHAT IT DOES NOT ESTABLISH');
  out.push('  The chain is held in the browser that produced it. Anyone with');
  out.push('  developer tools on that machine could rewrite it end to end, so it');
  out.push('  is tamper-EVIDENT, not tamper-PROOF. The hosted engine shares that');
  out.push('  theoretical weakness - anyone with database access could too - but');
  out.push('  the bar is far higher there, and it refreshes the watchlists daily');
  out.push('  rather than relying on an imported snapshot.');
  out.push('');
  out.push('  Automatic document cross-checks are advisory. Every decision above');
  out.push('  was made and accepted by the named person, not by the engine.');
  out.push('');
  out.push('  Evidence digests are computed locally and can be re-verified against');
  out.push('  the original files. Timestamps show Europe/Berlin with UTC in');
  out.push('  brackets; UTC is the value the chain commits to.');
  out.push(rule('='));
  return out.join('\n')+'\n';
}

function download(name, text, mime){
  var blob = new Blob([text], {type: mime||'text/plain;charset=utf-8'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}
function slug(s){ return String(s).replace(/[^A-Za-z0-9]+/g,'_').slice(0,40); }

async function printableHtml(){
  /* A print-optimised page. The browser's own "Save as PDF" produces the PDF,
     which avoids shipping a PDF library the CSP would block anyway. */
  var body = esc(await recordLines());
  return '<!doctype html><html><head><meta charset="utf-8">'+
    '<title>Due Diligence Record — '+esc(CASE.query)+'</title><style>'+
    '@page{size:A4;margin:18mm}'+
    'body{font:10.5px/1.45 ui-monospace,Consolas,"Courier New",monospace;color:#111;margin:0}'+
    'pre{white-space:pre-wrap;word-wrap:break-word;margin:0}'+
    '.hint{font-family:system-ui,sans-serif;background:#fdf3e0;border:1px solid #e0c390;'+
    'padding:10px 12px;margin-bottom:14px;font-size:12px}'+
    '@media print{.hint{display:none}}'+
    '</style></head><body>'+
    '<div class="hint"><strong>To save as PDF:</strong> press Ctrl+P (Cmd+P on Mac) and choose '+
    '“Save as PDF” as the destination. This banner will not be printed.</div>'+
    '<pre>'+body+'</pre></body></html>';
}
