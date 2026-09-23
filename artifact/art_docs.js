/* ============================================================
   Document text extraction and cross-check, entirely in-browser.
   Ported from backend/app/{doc_extract,doc_crosscheck}.py

   Files never leave the machine: everything below runs on an ArrayBuffer read
   from the local disk. There is no upload.

   No external library is used because the artifact CSP blocks every CDN, so
   ZIP and PDF are decoded with DecompressionStream, which browsers ship
   natively. Where that cannot cope, the officer is offered a paste-the-text
   box rather than being told the document is useless.
   ============================================================ */
'use strict';

var IMAGE_EXT = /\.(png|jpe?g|gif|webp|tiff?)$/i;
var LEGACY_EXT = /\.(doc|xls|ppt|msg)$/i;

async function inflate(bytes, format){
  var ds = new DecompressionStream(format);
  var stream = new Blob([bytes]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/* ---------- ZIP (docx / xlsx / pptx are zip archives of XML) ---------- */
async function zipEntries(bytes){
  var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  var out = {}, i = 0, dec = new TextDecoder();
  while (i < bytes.length - 4) {
    if (dv.getUint32(i, true) !== 0x04034b50) { i++; continue; }
    var method = dv.getUint16(i + 8, true);
    var compSize = dv.getUint32(i + 18, true);
    var nameLen = dv.getUint16(i + 26, true);
    var extraLen = dv.getUint16(i + 28, true);
    var name = dec.decode(bytes.subarray(i + 30, i + 30 + nameLen));
    var dataStart = i + 30 + nameLen + extraLen;
    if (compSize === 0 || dataStart + compSize > bytes.length) { i = dataStart; continue; }
    var raw = bytes.subarray(dataStart, dataStart + compSize);
    try {
      out[name] = method === 0 ? raw : await inflate(raw, 'deflate-raw');
    } catch (e) { /* skip an entry we cannot inflate */ }
    i = dataStart + compSize;
  }
  return out;
}

function xmlText(bytes, tag){
  var xml = new TextDecoder().decode(bytes);
  var re = new RegExp('<' + tag + '[^>]*>([^<]*)</' + tag + '>', 'g');
  var out = [], m;
  while ((m = re.exec(xml)) !== null) out.push(m[1]);
  return out.join(' ');
}
function unescapeXml(s){
  return s.replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
          .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&apos;/g,"'");
}

/* ---------- PDF (text layer only) ---------- */
async function pdfText(bytes){
  var dec = new TextDecoder('latin1');
  var raw = dec.decode(bytes);
  var chunks = [], pos = 0, pages = (raw.match(/\/Type\s*\/Page[^s]/g) || []).length;

  while (true) {
    var s = raw.indexOf('stream', pos);
    if (s < 0) break;
    var e = raw.indexOf('endstream', s);
    if (e < 0) break;
    var start = s + 6;
    while (raw[start] === '\r' || raw[start] === '\n') start++;
    var slice = bytes.subarray(start, e);
    var header = raw.slice(Math.max(0, s - 400), s);
    var text = null;
    if (/\/FlateDecode/.test(header)) {
      try { text = new TextDecoder('latin1').decode(await inflate(slice, 'deflate')); }
      catch (err) { text = null; }
    } else {
      text = dec.decode(slice);
    }
    if (text) chunks.push(text);
    pos = e + 9;
  }

  // Pull the operands of the text-showing operators out of the content streams.
  var pieces = [];
  chunks.forEach(function(content){
    var re = /\((?:\\.|[^\\()])*\)/g, m;
    while ((m = re.exec(content)) !== null) {
      var lit = m[0].slice(1, -1)
        .replace(/\\([nrtbf])/g, ' ')
        .replace(/\\([0-7]{1,3})/g, function(_, o){ return String.fromCharCode(parseInt(o, 8)); })
        .replace(/\\(.)/g, '$1');
      if (lit.trim()) pieces.push(lit);
    }
  });
  return { text: pieces.join(' '), pages: pages };
}

function tidy(s){
  return String(s || '').replace(/\r\n?/g,'\n').replace(/[ \t]+/g,' ')
    .replace(/\n{3,}/g,'\n\n').trim().slice(0, 400000);
}

/* Returns {text, status, detail}. status: ok | empty | unsupported | error */
async function extractDocument(file){
  var name = file.name || '';
  if (IMAGE_EXT.test(name)) {
    return { text:'', status:'unsupported',
      detail:'Images cannot be read without OCR, which is not available in a browser. '+
             'This document provides no automatic corroboration and must be reviewed by eye.' };
  }
  if (LEGACY_EXT.test(name)) {
    return { text:'', status:'unsupported',
      detail:'Legacy Office formats use a proprietary binary layout that cannot be read here. '+
             'Re-save as .docx / .xlsx or as PDF to enable automatic cross-checking.' };
  }

  var bytes = new Uint8Array(await file.arrayBuffer());
  try {
    if (/\.pdf$/i.test(name)) {
      var got = await pdfText(bytes);
      var text = tidy(got.text);
      if (!text) {
        return { text:'', status:'empty',
          detail:'The PDF has ' + (got.pages || '?') + ' page(s) but no extractable text — '+
                 'most likely a scan or photograph, which cannot be read without OCR.' };
      }
      return { text: text, status:'ok', detail: (got.pages || '?') + ' page(s)' };
    }
    if (/\.(docx|pptx)$/i.test(name)) {
      var entries = await zipEntries(bytes);
      var parts = [];
      Object.keys(entries).forEach(function(k){
        if (/word\/document\.xml$/.test(k)) parts.push(xmlText(entries[k], 'w:t'));
        if (/ppt\/slides\/slide\d+\.xml$/.test(k)) parts.push(xmlText(entries[k], 'a:t'));
      });
      var t = tidy(unescapeXml(parts.join(' ')));
      return t ? { text:t, status:'ok', detail:'office document' }
               : { text:'', status:'empty', detail:'The document contains no text.' };
    }
    if (/\.(xlsx|xlsm)$/i.test(name)) {
      var ent = await zipEntries(bytes);
      var bits = [];
      Object.keys(ent).forEach(function(k){
        if (/sharedStrings\.xml$/.test(k)) bits.push(xmlText(ent[k], 't'));
        if (/xl\/worksheets\/sheet\d+\.xml$/.test(k)) bits.push(xmlText(ent[k], 't'));
      });
      var x = tidy(unescapeXml(bits.join(' ')));
      return x ? { text:x, status:'ok', detail:'spreadsheet' }
               : { text:'', status:'empty', detail:'The workbook contains no text values.' };
    }
    // txt / csv / md / eml — plain text on the wire
    var plain = tidy(new TextDecoder('utf-8').decode(bytes));
    return plain ? { text:plain, status:'ok', detail:'plain text' }
                 : { text:'', status:'empty', detail:'The file is empty.' };
  } catch (err) {
    return { text:'', status:'error', detail:String(err && err.message || err).slice(0,180) };
  }
}

/* ============================================================
   Cross-check — mirrors doc_crosscheck.cross_check
   ============================================================ */
var DATE_RE = [
  /\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b/g,
  /\b(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\b/g,
  /\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})\b/gi
];
var YEAR_RE = /\b(1[89]\d{2}|20\d{2})\b/g;
var ID_RE = /\b(?=[A-Z0-9]{6,20}\b)(?=[^\s]*\d)[A-Z0-9]{6,20}\b/g;

function distinctiveTokens(name){
  return normalize(name).split(' ').filter(function(t){ return t.length>2 && !isGenericToken(t); });
}
function namePresence(name, haystack){
  var toks = distinctiveTokens(name);
  if(!toks.length) return { present:false, coverage:0, missing:[] };
  var found=[], missing=[];
  toks.forEach(function(t){
    if(haystack.indexOf(t)>=0){ found.push(t); return; }
    if(t.length>=6){
      // tolerate one transcription slip on a long token
      var hit=false;
      for(var i=0;i+t.length<=haystack.length;i++){
        if(haystack[i]===t[0] && ratio(t, haystack.substr(i,t.length))>=88){ hit=true; break; }
      }
      if(hit){ found.push(t); return; }
    }
    missing.push(t);
  });
  var cov = found.length/toks.length;
  return { present: cov>=0.75, coverage: cov, missing: missing };
}
function yearsIn(text){
  var out={}, m, j;
  for(j=0;j<DATE_RE.length;j++){
    DATE_RE[j].lastIndex=0;
    while((m=DATE_RE[j].exec(text))!==null){
      for(var g=1;g<m.length;g++) if(/^\d{4}$/.test(m[g])) out[m[g]]=1;
    }
  }
  YEAR_RE.lastIndex=0;
  while((m=YEAR_RE.exec(text))!==null) out[m[1]]=1;
  return Object.keys(out);
}

function crossCheck(opts){
  var findings = [], result = {
    readable: opts.status==='ok' && !!(opts.text||'').trim(),
    extraction_status: opts.status, extraction_detail: opts.detail,
    findings: findings, recommendation:'', suggested_reason_code:'',
    confidence:'none', requires_human_confirmation:true
  };

  if(!result.readable){
    findings.push({ check:'Document readability', verdict:'unreadable',
      detail: opts.detail || 'No text could be extracted, so nothing in this document has been verified automatically.' });
    result.recommendation = 'This document could not be read, so it provides NO automatic '+
      'corroboration. Review it by eye before relying on it, and do not treat its presence '+
      'as evidence in itself.';
    return result;
  }

  var hay = basicNorm(opts.text);

  var tgt = namePresence(opts.targetName, hay);
  findings.push(tgt.present
    ? { check:'Designated party named in document', verdict:'contradicts',
        detail:"The designation '"+opts.targetName+"' appears in this document ("+
               Math.round(tgt.coverage*100)+"% of its distinctive terms). A document that "+
               "names the designated party tends to support a match, not clear it." }
    : { check:'Designated party named in document', verdict:'corroborates',
        detail:"No mention of '"+opts.targetName+"' found"+
               (tgt.missing.length ? " (missing: "+tgt.missing.slice(0,5).join(', ')+")" : "")+
               ". Consistent with an unrelated party, though absence is weak evidence on its own." });

  var ptr = namePresence(opts.partnerName, hay);
  findings.push({ check:'Partner named in document', verdict: ptr.present?'corroborates':'neutral',
    detail:"'"+opts.partnerName+"' "+(ptr.present?'appears':'does not appear')+" in this document ("+
           Math.round(ptr.coverage*100)+"% of distinctive terms)."+
           (ptr.present?'':' A document that does not name the partner may not be about them.') });

  var docYears = yearsIn(opts.text);
  var tgtYears = (opts.targetDob||'').match(YEAR_RE) || [];
  if(tgtYears.length && docYears.length){
    var overlap = tgtYears.filter(function(y){ return docYears.indexOf(y)>=0; });
    findings.push({ check:'Date comparison', verdict: overlap.length?'contradicts':'corroborates',
      detail: overlap.length
        ? 'Designation year(s) '+tgtYears.join(', ')+' also appear in the document — this points towards the same subject.'
        : 'Designation year(s) '+tgtYears.join(', ')+' do not appear. Document years found: '+docYears.slice(0,8).join(', ')+'.' });
  } else if(docYears.length){
    findings.push({ check:'Date comparison', verdict:'neutral',
      detail:'Document contains year(s) '+docYears.slice(0,8).join(', ')+
             ' but the designation records no date, so they cannot be compared.' });
  }

  ID_RE.lastIndex = 0;
  var ids = {}, m;
  while((m=ID_RE.exec(hay))!==null) ids[m[0]]=1;
  var idList = Object.keys(ids);
  var blob = basicNorm((opts.targetWatchlistId||'')+' '+(opts.targetRemarks||''));
  var shared = idList.filter(function(i){ return blob.indexOf(i)>=0; });
  if(shared.length){
    findings.push({ check:'Identifier comparison', verdict:'contradicts',
      detail:'Identifier(s) '+shared.slice(0,4).join(', ')+' appear both in this document and on '+
             'the designation. Verify carefully — this points towards the same subject.' });
  } else if(idList.length){
    findings.push({ check:'Identifier comparison', verdict:'neutral',
      detail:idList.length+' identifier(s) found in the document (e.g. '+idList.slice(0,3).join(', ')+
             '); none matches the designation record. Note the designation may simply not publish one.' });
  }

  var tc = resolveCountry(opts.targetGeography), dc = resolveCountry(opts.text.slice(0,20000));
  if(tc.length && dc.length){
    var both = tc.filter(function(c){ return dc.indexOf(c)>=0; });
    findings.push({ check:'Country comparison', verdict: both.length?'contradicts':'corroborates',
      detail: both.length ? 'Both refer to '+both.join(', ')+'.'
        : 'Designation country '+tc.join(', ')+' vs document '+dc.slice(0,5).join(', ')+
          '. Note a designated party can operate through a third country, so this is weak grounds alone.' });
  }

  var against = findings.filter(function(f){ return f.verdict==='contradicts'; }).length;
  var supporting = findings.filter(function(f){ return f.verdict==='corroborates'; }).length;
  if(against){
    result.recommendation = against+' finding(s) point TOWARDS this being the designated party. '+
      'Do not clear this alert on the strength of this document without resolving them.';
    result.confidence = against>1 ? 'moderate' : 'low';
  } else if(supporting>=2){
    result.recommendation = supporting+' finding(s) are consistent with an unrelated party. '+
      'This may support a dismissal — confirm the document is genuine and actually concerns '+
      'this partner, then state the grounds yourself.';
    result.suggested_reason_code = ptr.present ? 'DIFFERENT_ENTITY_SAME_NAME' : '';
    result.confidence = 'low';
  } else {
    result.recommendation = 'Nothing decisive found. This document neither corroborates nor '+
      'contradicts the designation on its own.';
  }
  return result;
}

/* ---------- dismissal validation — mirrors dismissal.validate ---------- */
function validateAdjudication(decision, reasonCode, rationale){
  decision = (decision||'').trim(); reasonCode = (reasonCode||'').trim(); rationale = (rationale||'').trim();
  if(!decision) return null;
  var byCode = {}; TAX.reasons.forEach(function(r){ byCode[r.code]=r; });

  if(TAX.decisions_requiring_reason.indexOf(decision)>=0 && !reasonCode)
    return "A decision of '"+decision+"' requires a reason code. An alert cannot be cleared "+
           "or escalated without stated grounds.";
  if(reasonCode && !byCode[reasonCode]) return "Unknown reason code '"+reasonCode+"'.";
  if(TAX.requires_rationale.indexOf(reasonCode)>=0 && !rationale)
    return "Reason '"+byCode[reasonCode].label+"' requires a written rationale — these grounds "+
           "are not self-evidencing.";
  if(reasonCode){
    var applies = byCode[reasonCode].applies_to;
    if(applies==='dismiss' && decision==='Positive Match')
      return "Reason '"+byCode[reasonCode].label+"' is grounds for clearing an alert, not for confirming one.";
    if(applies==='confirm' && decision==='False Match')
      return "Reason '"+byCode[reasonCode].label+"' confirms a match; it cannot support a False Match.";
  }
  return null;
}
