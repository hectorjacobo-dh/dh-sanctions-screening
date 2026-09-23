/* ============================================================
   Matching engine — ported from backend/app/{normalize,matching}.py
   Kept in one file so the artifact template stays assemblable.
   ============================================================ */
'use strict';

/* The watchlist is embedded as JSON in a <script type="application/json"> tag
   and parsed at runtime, NOT written as a JavaScript object literal.
   Measured: a 4.5 MB literal takes the JS compiler tens of seconds to walk
   before a single line executes, so the page sits dead. JSON.parse of the
   same bytes is a fast dedicated path. */
var BUILTIN = JSON.parse(document.getElementById('watchlist-data').textContent);
var TAX = JSON.parse(document.getElementById('taxonomy-data').textContent);
/* Not const: an imported snapshot replaces these at boot and rebuilds every
   derived index. See adoptBundle(). */
var BUNDLE = BUILTIN, SRC = BUNDLE.src, REC = BUNDLE.rec;
var N_NAME = 0, N_SRC = 1, N_TYPE = 2, N_PROG = 3, N_GEO = 4, N_WID = 5, N_AKA = 6, N_DOB = 7;
var TYPE_FULL = { I:'Individual', E:'Entity', V:'Vessel', A:'Aircraft', U:'Unknown' };

/* ---------- normalisation ---------- */
var SUFFIXES = ('LTD|LIMITED|LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|PLC|LLP|LP|'+
  'HOLDINGS|HOLDING|GROUP|ENTERPRISES|ENTERPRISE|INTERNATIONAL|INTL|PRIVATE LIMITED|PVT LTD|PVT|'+
  'PTY LTD|PTY|PTE LTD|PTE|GMBH|MBH|AG|KG|KGAA|OHG|EG|GBR|UG|SE|SARL|S A R L|SAS|SASU|SA|S A|'+
  'SNC|SCS|SCA|EURL|SL|S L|SLU|SRL|S R L|SA DE CV|S A DE C V|SAC|EIRL|LTDA|CIA LTDA|CIA|SPA|'+
  'S P A|SRLS|SAPA|LDA|BV|B V|NV|N V|VOF|CV|AB|AS|A S|ASA|OY|OYJ|APS|IVS|HF|EHF|OU|SIA|UAB|'+
  'KFT|ZRT|NYRT|BT|SP Z OO|SRO|S R O|DOO|D O O|AD|EOOD|OOD|OOO|O O O|ZAO|OAO|PAO|AO|TOO|IP|'+
  'JSC|OJSC|CJSC|PJSC|WLL|W L L|SPC|FZE|FZC|FZ LLC|FZ|FZCO|DMCC|EST|ESTABLISHMENT|SAOG|SAOC|'+
  'QSC|QPSC|KSC|KSCC|BSC|SDN BHD|SDN|BHD|BERHAD|TBK|PERSERO|KK|YK|CO LTD|HK|LIMITADA|'+
  'SOCIEDAD DE RESPONSABILIDAD LIMITADA|SOCIEDAD ANONIMA|SOCIEDAD LIMITADA|SOCIEDAD|'+
  'RESPONSABILIDAD LIMITADA|TRADING|GENERAL TRADING|RESTAURANT|RESTAURANTS|CATERING|'+
  'FOODSTUFF|CAFE|CAFETERIA|KITCHEN|FOODS|FOOD').split('|');
var PREFIXES = 'PT|CV|UD|OOO|ZAO|OAO|PAO|AO|JSC|OJSC|CJSC|PJSC|TOO|MS|MESSRS|THE|AL|EL'.split('|');

/* Corporate boilerplate — carries no identity, so an unmatched one barely
   costs. Mirrors normalize.GENERIC_TOKENS. */
var GENERIC = {};
('PUBLIC PRIVATE OPEN CLOSED JOINT STOCK SHARE SHARES COMPANY COMPANIES CORPORATION CORP '+
 'INCORPORATED LIMITED LIABILITY UNLIMITED PARTNERSHIP ENTERPRISE ENTERPRISES FIRM CONCERN '+
 'COMBINE ASSOCIATION ORGANISATION ORGANIZATION SOCIETY UNION FEDERAL STATE NATIONAL REGIONAL '+
 'MUNICIPAL GENERAL CENTRAL UNITARY SUBSIDIARY AFFILIATE BRANCH DIVISION GROUP HOLDING HOLDINGS '+
 'INTERNATIONAL GLOBAL WORLDWIDE TRADING COMMERCIAL INDUSTRIAL INDUSTRIES INDUSTRY MANUFACTURING '+
 'PRODUCTION SERVICES SERVICE SOLUTIONS SYSTEMS SYSTEM TECHNOLOGIES TECHNOLOGY SCIENTIFIC '+
 'RESEARCH DEVELOPMENT MANAGEMENT INVESTMENT INVESTMENTS CAPITAL FINANCE FINANCIAL THE AND OF FOR'
).split(' ').forEach(function(t){ GENERIC[t] = 1; });

var SUF = {}, PRE = {}, MAXSUF = 1, MAXPRE = 1, _i, _p;
for (_i = 0; _i < SUFFIXES.length; _i++) {
  _p = SUFFIXES[_i].split(' '); SUF[_p.join(' ')] = 1;
  if (_p.length > MAXSUF) MAXSUF = _p.length;
  _p.forEach(function(t){ GENERIC[t] = 1; });
}
for (_i = 0; _i < PREFIXES.length; _i++) {
  _p = PREFIXES[_i].split(' '); PRE[_p.join(' ')] = 1;
  if (_p.length > MAXPRE) MAXPRE = _p.length;
  _p.forEach(function(t){ GENERIC[t] = 1; });
}
function isGenericToken(t){ return !!GENERIC[t]; }

var CYR = {'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'E','Ж':'ZH','З':'Z','И':'I',
  'Й':'I','К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R','С':'S','Т':'T','У':'U',
  'Ф':'F','Х':'KH','Ц':'TS','Ч':'CH','Ш':'SH','Щ':'SHCH','Ъ':'','Ы':'Y','Ь':'','Э':'E',
  'Ю':'YU','Я':'YA'};
var HAS_CYR = /[\u0400-\u04FF]/;   // Cyrillic block
var NON_ALNUM = /[^0-9A-Z]+/g;

function basicNorm(s){
  if (!s) return '';
  var t = String(s).toUpperCase();
  // Ranges use explicit \u escapes. A literal control-character range compiles
  // to an invalid regular expression, and ONE invalid regex literal aborts the
  // entire script before a single line runs.
  if (/[^\u0020-\u007E]/.test(t)) {
    t = t.normalize('NFKD').replace(/[\u0300-\u036F]/g, '');   // combining marks
    if (HAS_CYR.test(t)) {
      var o = '', c;
      for (var k = 0; k < t.length; k++) { c = t[k]; o += (CYR[c] !== undefined ? CYR[c] : c); }
      t = o;
    }
  }
  return t.replace(NON_ALNUM, ' ').trim();
}

function collapseInitials(toks){
  var out = [], run = [], j;
  for (j = 0; j < toks.length; j++) {
    if (toks[j].length === 1) { run.push(toks[j]); continue; }
    if (run.length) { out.push(run.length > 1 ? run.join('') : run[0]); run = []; }
    out.push(toks[j]);
  }
  if (run.length) out.push(run.length > 1 ? run.join('') : run[0]);
  return out;
}
function stripPre(t){ var ch=true,s; while(ch&&t.length>1){ ch=false;
  for(s=Math.min(MAXPRE,t.length-1);s>=1;s--){ if(PRE[t.slice(0,s).join(' ')]){t=t.slice(s);ch=true;break;} } } return t; }
function stripSuf(t){ var ch=true,s; while(ch&&t.length>1){ ch=false;
  for(s=Math.min(MAXSUF,t.length-1);s>=1;s--){ if(SUF[t.slice(t.length-s).join(' ')]){t=t.slice(0,t.length-s);ch=true;break;} } } return t; }

var normCache = Object.create(null);
function normalize(s){
  var hit = normCache[s];
  if (hit !== undefined) return hit;
  var base = basicNorm(s);
  if (!base) return (normCache[s] = '');
  base = base.replace(/\bAL (?=[A-Z])/g, 'AL');
  var toks = collapseInitials(base.split(' '));
  var core = stripSuf(stripPre(toks.slice()));
  return (normCache[s] = (core.length ? core.join(' ') : toks.join(' ')));
}
function initials(toks){ var u=[],j;
  for(j=0;j<toks.length;j++) if(toks[j].length>1) u.push(toks[j][0]);
  return u.length>=2 ? u.join('') : ''; }
function acronyms(s){
  var base = basicNorm(s); if (!base) return [];
  var toks = collapseInitials(base.split(' '));
  var views = [toks, stripPre(toks.slice()), stripSuf(toks.slice()), stripSuf(stripPre(toks.slice()))];
  var seen = Object.create(null), out = [], a, j;
  for (j=0;j<views.length;j++){ a = initials(views[j]); if(a && !seen[a]){ seen[a]=1; out.push(a); } }
  return out;
}
function isAcronymish(n){ var t=n.split(' ');
  return t.length===1 && t[0].length>=2 && t[0].length<=6 && /^[A-Z]+$/.test(t[0]); }

/* ---------- scoring (LCS-based ratio, as rapidfuzz) ---------- */
var rowA = new Uint16Array(256), rowB = new Uint16Array(256);
function lcs(a,b){
  var n=a.length,m=b.length,x,y,prev,cur,tmp;
  if(!n||!m) return 0;
  if(m+1>rowA.length){ rowA=new Uint16Array(m+1); rowB=new Uint16Array(m+1); }
  prev=rowA; cur=rowB;
  for(y=0;y<=m;y++) prev[y]=0;
  for(x=1;x<=n;x++){
    cur[0]=0;
    for(y=1;y<=m;y++){
      cur[y] = (a.charCodeAt(x-1)===b.charCodeAt(y-1)) ? prev[y-1]+1
             : (cur[y-1]>prev[y] ? cur[y-1] : prev[y]);
    }
    tmp=prev; prev=cur; cur=tmp;
  }
  return prev[m];
}
function ratio(a,b){ var t=a.length+b.length; return t ? (2*lcs(a,b)/t)*100 : 0; }
function sortedJoin(s){ return s.split(' ').sort().join(' '); }
function tokenSort(a,b){ return ratio(sortedJoin(a), sortedJoin(b)); }
function tokenSet(a,b){
  var ta=a.split(' '), tb=b.split(' '), sa=Object.create(null), sb=Object.create(null), j;
  for(j=0;j<ta.length;j++) sa[ta[j]]=1;
  for(j=0;j<tb.length;j++) sb[tb[j]]=1;
  var inter=[],da=[],db=[],k;
  for(k in sa){ if(sb[k]) inter.push(k); else da.push(k); }
  for(k in sb){ if(!sa[k]) db.push(k); }
  inter.sort(); da.sort(); db.sort();
  var si=inter.join(' ');
  var s1=(si+' '+da.join(' ')).trim(), s2=(si+' '+db.join(' ')).trim();
  var best=ratio(s1,s2);
  if(si) best=Math.max(best, ratio(si,s1), ratio(si,s2));
  return best;
}
/* Token-level alignment with prefix matching. This is what makes "gazprom"
   reach the 109 designations containing it; leftover tokens are weighted by
   whether they carry identity. Mirrors matching.token_alignment. */
function tokenAlignment(q, t){
  if(!q.length || !t.length) return {coverage:0, weight:0};
  var used = {}, total = 0, j, k, best, bestIdx, sc;
  for(j=0;j<q.length;j++){
    best=0; bestIdx=-1;
    for(k=0;k<t.length;k++){
      if(used[k]) continue;
      if(q[j]===t[k]) sc=1;
      else if(q[j].length>=5 && t[k].indexOf(q[j])===0) sc=0.94;
      else if(t[k].length>=5 && q[j].indexOf(t[k])===0) sc=0.94;
      else { var r=ratio(q[j],t[k]); sc = r>=85 ? r/100 : 0; }
      if(sc>best){ best=sc; bestIdx=k; }
    }
    if(bestIdx>=0 && best>0) used[bestIdx]=1;
    total+=best;
  }
  var weight=0;
  for(k=0;k<t.length;k++) if(!used[k]) weight += isGenericToken(t[k]) ? 0.15 : 1;
  return {coverage: total/q.length, weight: weight};
}
function acronymScore(qRaw,qn,tRaw,tn){
  var qa=isAcronymish(qn), ta=isAcronymish(tn);
  if(qa===ta) return 0;
  var short = qa?qn:tn, longRaw = qa?tRaw:qRaw;
  if(short.length<3) return 0;   // "BM" is not discriminative
  var e=acronyms(longRaw), best=0, r, j;
  for(j=0;j<e.length;j++){ if(e[j]===short) return 100; r=ratio(short,e[j]); if(r>best) best=r; }
  return best>=85 ? best : 0;
}
/* recallMode=true is manual search: an officer typed one name and will read
   every row. See matching.score_pair for why batch differs. */
function score(qRaw,qn,qTok,tRaw,tn){
  if(!qn||!tn) return null;
  var lr = Math.min(qn.length,tn.length)/Math.max(qn.length,tn.length);
  var base = tokenSort(qn,tn), codes = [];
  var tTok = tn.split(' ');

  var shortTok = qTok.length<=tTok.length ? qTok : tTok;
  var longTok  = shortTok===qTok ? tTok : qTok;
  var shorter  = shortTok===qTok ? qn : tn;
  var longSet = Object.create(null), j;
  for(j=0;j<longTok.length;j++) longSet[longTok[j]]=1;
  var all = shortTok.length>0;
  for(j=0;j<shortTok.length;j++) if(!longSet[shortTok[j]]){ all=false; break; }
  var contained = all;
  var discriminative = shortTok.length>=3 || shorter.length>=12;

  if(lr>=0.70 || (contained && discriminative)) base = Math.max(base, tokenSet(qn,tn));
  else codes.push('LENGTH_GATE_APPLIED');

  var al = tokenAlignment(qTok, tTok);
  var alignment = al.coverage<=0 ? 0 : Math.max(0, Math.min(100, 100*al.coverage - 3*al.weight));
  if(alignment>base){
    base = alignment;
    if(al.coverage>=0.999 && al.weight>0) codes.push('SEARCHED_NAME_WITHIN_DESIGNATION');
  }

  var acr = acronymScore(qRaw,qn,tRaw,tn);
  if(acr>base) base = acr;

  if(qn===tn){ base=100; codes.push('EXACT_NORMALISED_NAME'); }
  if(acr>=85) codes.push('ACRONYM_EXPANSION');
  if(contained && discriminative && lr<0.70) codes.push('DISCRIMINATIVE_NAME_CONTAINED');
  if(all && shortTok===tTok) codes.push('ALL_TOKENS_PRESENT');
  if(Math.min(qn.length,tn.length)<=4) codes.push('SHORT_NAME_LOW_CONFIDENCE');
  return { base: base, codes: codes };
}

/* ---------- blocking index ----------
   A linear scan of 57k designations per query is far too slow, so the
   watchlist is indexed by token and acronym on load using the CHEAP
   normalisation: the affix tokens it would strip are common enough to become
   stop-tokens anyway, so recall is unaffected and load stays fast. */
var tokenIndex = Object.create(null), acrIndex = Object.create(null), stopTokens = Object.create(null);
var MAX_CANDIDATES = 900;

function indexName(j, name){
  var toks = basicNorm(name).split(' '), k, tok, list;
  for(k=0;k<toks.length;k++){
    tok = toks[k]; if(!tok) continue;
    list = tokenIndex[tok];
    if(list===undefined) tokenIndex[tok]=[j];
    else if(list[list.length-1]!==j) list.push(j);
  }
}
/* Swap in a different snapshot and rebuild everything derived from it. */
function adoptBundle(bundle){
  BUNDLE = bundle;
  SRC = bundle.src;
  REC = bundle.rec;
  tokenIndex = Object.create(null);
  acrIndex = Object.create(null);
  stopTokens = Object.create(null);
  normCache = Object.create(null);
  buildIndex();
}

function buildIndex(){
  var j,k,arr,list;
  for(j=0;j<REC.length;j++){
    indexName(j, REC[j][N_NAME]);
    if(REC[j][N_AKA]) indexName(j, REC[j][N_AKA]);
    arr = acronyms(REC[j][N_NAME]);
    for(k=0;k<arr.length;k++){
      list = acrIndex[arr[k]];
      if(list===undefined) acrIndex[arr[k]]=[j];
      else if(list[list.length-1]!==j) list.push(j);
    }
  }
  var cap = Math.max(64, Math.floor(REC.length*0.02));
  for(var tok in tokenIndex) if(tokenIndex[tok].length>cap) stopTokens[tok]=tokenIndex[tok].length;
}
function candidates(rawQuery, qn){
  var toks = basicNorm(rawQuery).split(' ');
  var ev = Object.create(null), found = [], j, k, tok, list, w, id;
  for(j=0;j<toks.length;j++){
    tok = toks[j];
    if(!tok || stopTokens[tok]) continue;
    list = tokenIndex[tok]; if(!list) continue;
    w = list.length<=40 ? 6 : 3;
    for(k=0;k<list.length;k++){ id=list[k];
      if(ev[id]===undefined){ ev[id]=w; found.push(id); } else ev[id]+=w; }
  }
  var arr = acronyms(rawQuery);
  for(j=0;j<arr.length;j++){
    list = acrIndex[arr[j]]; if(!list) continue;
    for(k=0;k<list.length;k++){ id=list[k];
      if(ev[id]===undefined){ ev[id]=8; found.push(id); } else ev[id]+=8; }
  }
  if(isAcronymish(qn)){
    /* A single-token acronym query has no acronym OF ITS OWN, so the loop
       above finds nothing. It must be looked up AS an acronym — this is what
       lets "UAC" reach "PJSC United Aircraft Corporation". Mirrors
       BlockingIndex.candidates, which consults both indexes here. */
    list = acrIndex[qn];
    if(list) for(k=0;k<list.length;k++){ id=list[k];
      if(ev[id]===undefined){ ev[id]=8; found.push(id); } else ev[id]+=8; }
    list = tokenIndex[qn];
    if(list) for(k=0;k<list.length;k++){ id=list[k];
      if(ev[id]===undefined){ ev[id]=6; found.push(id); } else ev[id]+=6; }
  }
  /* Every query token too common to index: fall back to the rarest so a name
     built only of frequent words is still screenable. */
  if(!found.length){
    var rarest=null, best=Infinity;
    for(j=0;j<toks.length;j++){ tok=toks[j];
      if(tok && stopTokens[tok]!==undefined && stopTokens[tok]<best){ best=stopTokens[tok]; rarest=tok; } }
    if(rarest){ list=tokenIndex[rarest];
      for(k=0;k<list.length && k<4000;k++){ ev[list[k]]=1; found.push(list[k]); } }
  }
  if(found.length>MAX_CANDIDATES){
    found.sort(function(a,b){ return ev[b]-ev[a] || a-b; });
    found.length = MAX_CANDIDATES;
  }
  return found;
}

/* ---------- geography (subset sufficient for corroboration checks) ---------- */
var COUNTRY = {
  'BAHRAIN':'BH','KUWAIT':'KW','QATAR':'QA','OMAN':'OM','UNITED ARAB EMIRATES':'AE','UAE':'AE',
  'SAUDI ARABIA':'SA','JORDAN':'JO','IRAQ':'IQ','EGYPT':'EG','LEBANON':'LB','TURKEY':'TR',
  'PAKISTAN':'PK','BANGLADESH':'BD','SINGAPORE':'SG','MALAYSIA':'MY','THAILAND':'TH',
  'PHILIPPINES':'PH','TAIWAN':'TW','HONG KONG':'HK','JAPAN':'JP','SOUTH KOREA':'KR',
  'CAMBODIA':'KH','LAOS':'LA','MYANMAR':'MM','VIETNAM':'VN','GERMANY':'DE','AUSTRIA':'AT',
  'SWITZERLAND':'CH','SWEDEN':'SE','NORWAY':'NO','DENMARK':'DK','FINLAND':'FI','SPAIN':'ES',
  'PORTUGAL':'PT','ITALY':'IT','FRANCE':'FR','POLAND':'PL','CZECH REPUBLIC':'CZ','SLOVAKIA':'SK',
  'HUNGARY':'HU','ROMANIA':'RO','BULGARIA':'BG','CROATIA':'HR','SLOVENIA':'SI','SERBIA':'RS',
  'GREECE':'GR','NETHERLANDS':'NL','BELGIUM':'BE','LUXEMBOURG':'LU','IRELAND':'IE',
  'UNITED KINGDOM':'GB','UK':'GB','ENGLAND':'GB','UKRAINE':'UA','MOLDOVA':'MD','GEORGIA':'GE',
  'ARMENIA':'AM','AZERBAIJAN':'AZ','KAZAKHSTAN':'KZ','UZBEKISTAN':'UZ','ARGENTINA':'AR',
  'BOLIVIA':'BO','CHILE':'CL','COLOMBIA':'CO','COSTA RICA':'CR','ECUADOR':'EC','GUATEMALA':'GT',
  'HONDURAS':'HN','MEXICO':'MX','PANAMA':'PA','PARAGUAY':'PY','PERU':'PE','URUGUAY':'UY',
  'VENEZUELA':'VE','KENYA':'KE','NIGERIA':'NG','GHANA':'GH','MOROCCO':'MA','TUNISIA':'TN',
  'ALGERIA':'DZ','LIBYA':'LY','SUDAN':'SD','SOUTH AFRICA':'ZA','RUSSIA':'RU',
  'RUSSIAN FEDERATION':'RU','IRAN':'IR','NORTH KOREA':'KP','SYRIA':'SY','CUBA':'CU',
  'BELARUS':'BY','AFGHANISTAN':'AF','YEMEN':'YE','SOMALIA':'SO','ZIMBABWE':'ZW','MALI':'ML',
  'UNITED STATES':'US','USA':'US','CHINA':'CN','INDIA':'IN','INDONESIA':'ID','AUSTRALIA':'AU',
  'CANADA':'CA','BRAZIL':'BR','ISRAEL':'IL','CYPRUS':'CY','MALTA':'MT','SEYCHELLES':'SC',
  'CARACAS':'VE','MOSCOW':'RU','TEHRAN':'IR','DUBAI':'AE','HAMBURG':'DE','BERLIN':'DE'
};
var DEMONYM = {'RUSSIAN':'RU','IRANIAN':'IR','SYRIAN':'SY','CUBAN':'CU','BELARUSIAN':'BY',
  'VENEZUELAN':'VE','GERMAN':'DE','SPANISH':'ES','ITALIAN':'IT','FRENCH':'FR','BRITISH':'GB',
  'TURKISH':'TR','EGYPTIAN':'EG','SAUDI':'SA','EMIRATI':'AE','PAKISTANI':'PK','INDIAN':'IN',
  'CHINESE':'CN','CAMBODIAN':'KH','MEXICAN':'MX','COLOMBIAN':'CO'};
function resolveCountry(value){
  if(!value) return [];
  var raw = String(value).replace(/[\/;,|]/g,'\n'), out = {}, parts = raw.split('\n'), j, key, alias;
  for(j=0;j<parts.length;j++){
    key = basicNorm(parts[j]); if(!key) continue;
    if(COUNTRY[key]){ out[COUNTRY[key]]=1; continue; }
    if(DEMONYM[key]){ out[DEMONYM[key]]=1; continue; }
    for(alias in COUNTRY) if(alias.length>4 && key.indexOf(alias)>=0) out[COUNTRY[alias]]=1;
  }
  return Object.keys(out);
}
