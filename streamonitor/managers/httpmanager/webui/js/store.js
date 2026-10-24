'use strict';
/* ---------------- state ---------------- */
let route = {view:'dashboard'};   // {view:'dashboard'|'detail'|'settings', id}
let pendingRoute = null;   // deep-linked detail route awaiting API data; resolved once streamers load
let filters = {site:'all', status:'all', q:''};
let sort = {key:'status', dir:1};
let addMode = 'url';
let addMsg = null;   // {type:'err'|'ok', text}
let addDraft = {url:'', user:''};  // survives timer-driven re-renders
let player = {fileId:null, playing:false, pos:0, timer:null};
let modalState = null;
let confirmDelId = null;
let selected = new Set();   // dashboard multiselect — streamer ids
let selMode = false;   // NB9: selection mode — checkboxes stay hidden until explicitly entered or an item is checked
function loadViewMode(){ try { return localStorage.getItem('sm_viewmode')==='card' ? 'card' : 'list'; } catch(e){ return 'list'; } }
function saveViewMode(v){ try { localStorage.setItem('sm_viewmode', v); } catch(e){} }
let viewMode = loadViewMode();   // dashboard 'list'|'card' view preference (persisted)

/* ---------------- extended state (v2 scaffolding — data only, no UI yet) ---------------- */
let history = [];   // {user, site, url, avatarDataUrl, addedDate, removedDate}
/* P2: the detail video list renders in pages of DETAIL_PAGE_SIZE; visibleFiles is the
 * current window size, grown when the grid's sentinel scrolls into view and reset
 * whenever the displayed set changes (entering a detail, switching a filter). */
const DETAIL_PAGE_SIZE = 30;
let detailState = {fileFilter:'all', clsFilter:'all', visibleFiles:DETAIL_PAGE_SIZE};   // fileFilter 'all'|'marked'; clsFilter 'all'|'non-adult'|'adult'|'unknown'|'unanalyzed'|'recycle'
function resetDetailFileWindow(){ detailState.visibleFiles = DETAIL_PAGE_SIZE; }
let clsMenuFileId = null;   // detail: file whose classification menu is open (null = none)

/* ---------------- history ops ---------------- */
function pushHistory(s){
  const added = new Date(NOW); if (s.addedDaysAgo!=null) added.setDate(added.getDate()-s.addedDaysAgo);
  history.unshift({
    user:s.user, site:s.site, url: s.url || siteMeta(s.site).url(s.user),
    avatarDataUrl: genAvatar(s.seed, s.user),
    addedDate: added, removedDate: new Date(NOW),
  });
}
function removeHistoryAt(i){ if (i>=0 && i<history.length) history.splice(i,1); }

/* THE single source of truth for player hotkey defaults (player.js references
 * it; store.js loads first). Also what "restore defaults" resets to. */
const DEFAULT_HOTKEYS = {
  playPause:'Space', seekBack:'ArrowLeft', seekFwd:'ArrowRight',
  jumpBack:'Shift+ArrowLeft', jumpFwd:'Shift+ArrowRight',
  volumeUp:'ArrowUp', volumeDown:'ArrowDown',
  prevVideo:'PageUp', nextVideo:'PageDown', mute:'x', mark:'m',
};
/* display order for the settings hotkey editor */
const HOTKEY_ACTIONS = ['playPause','seekBack','seekFwd','jumpBack','jumpFwd','volumeUp','volumeDown','prevVideo','nextVideo','mute','mark'];
function loadHotkeys(){
  try {
    const raw = localStorage.getItem('sm_hotkeys');
    return raw ? Object.assign({}, DEFAULT_HOTKEYS, JSON.parse(raw)) : Object.assign({}, DEFAULT_HOTKEYS);
  } catch(e){ return Object.assign({}, DEFAULT_HOTKEYS); }
}
function saveHotkeys(hk){ localStorage.setItem('sm_hotkeys', JSON.stringify(hk)); }
let settings = {hotkeys: loadHotkeys()};
/* server-side download directory (NB4). draft survives timer-driven re-renders,
 * mirroring addDraft; render() preserves the input's focus + caret by id. */
let downloadDir = {path:'', draft:'', loading:false, saving:false, error:null, loaded:false};
/* server-side min-video-size cleanup threshold (MB); mirrors downloadDir */
let minVid = {value:null, draft:'', loading:false, saving:false, error:null, loaded:false};
/* login password change (server-side). The drafts are never persisted anywhere
 * and are cleared as soon as a save succeeds; error holds an i18n KEY. */
const PWD_MIN_LENGTH = 4, PWD_MAX_LENGTH = 128;
const USER_MIN_LENGTH = 1, USER_MAX_LENGTH = 64;
let pwd = {current:'', next:'', confirm:'', minLength:PWD_MIN_LENGTH, maxLength:PWD_MAX_LENGTH,
           username:'', usernameDraft:'', usernameMinLength:USER_MIN_LENGTH, usernameMaxLength:USER_MAX_LENGTH,
           saving:false, error:null, loaded:false};
/* server-side NSFW analysis toggle; error holds an i18n KEY (see recRes note).
 * modelPresent/runtimeAvailable are readiness info only — the toggle stays
 * usable without them (analysis then degrades to 'unknown' labels). */
const NSFW_ACTIONS = ['mark_only','recycle','delete'];
let nsfw = {enabled:null, action:'mark_only', recycleRetentionDays:7, scanIntervalHours:6, threshold:0.7,
            retentionDraft:'7', scanDraft:'6', thresholdDraft:'0.7',
            modelPresent:false, runtimeAvailable:false, loading:false, saving:false, error:null, loaded:false};

/* server-side recording resolution (v3.3). committed resolution/preference mirror
 * the last server-confirmed value; sel/custom/pref are the editable UI drafts,
 * rebuilt from committed on load and rolled back on save failure. Change-to-save. */
const REC_RES_PRESETS = ['1080','720','480','360','240'];
let recRes = {resolution:'max', preference:'closest', sel:'max', custom:'', pref:'closest', loading:false, saving:false, error:null, loaded:false};
function recResSyncUi(){
  const r = recRes.resolution;
  if (r==='max'){ recRes.sel='max'; recRes.custom=''; }
  else if (REC_RES_PRESETS.includes(String(r))){ recRes.sel=String(r); recRes.custom=''; }
  else { recRes.sel='custom'; recRes.custom=String(r); }
  recRes.pref = recRes.preference || 'closest';
}

/* ---- hotkey editing (read/write + conflict detection) ----
 * saveHotkeys(hk) needs the map passed explicitly (W1 note). */
function hotkeyConflict(action, token){
  for (const a of Object.keys(settings.hotkeys)){
    if (a!==action && settings.hotkeys[a]===token) return a;
  }
  return null;
}
function rebindHotkey(action, token){
  const clash = hotkeyConflict(action, token);
  if (clash) return {ok:false, conflict:clash};
  settings.hotkeys[action] = token;
  saveHotkeys(settings.hotkeys);
  return {ok:true};
}
function resetHotkeys(){
  settings.hotkeys = Object.assign({}, DEFAULT_HOTKEYS);
  saveHotkeys(settings.hotkeys);
}

function nav(to){
  if (typeof stopLive==='function') stopLive();   // abort any live stream before leaving/switching (no ghost connections)
  if (typeof clearDurBackfill==='function') clearDurBackfill();   // cancel any pending duration re-pull from the view we're leaving
  if (player.timer){clearInterval(player.timer); player.timer=null;}
  player = {fileId:null, playing:false, pos:0, timer:null};
  confirmDelId = null;
  selected.clear();
  detailState.fileFilter = 'all';
  detailState.clsFilter = 'all';
  resetDetailFileWindow();
  clsMenuFileId = null;
  if (to.view==='detail'){
    const s = streamers.find(x=>x.id===to.id);
    const latest = s && s.files[0];
    if (latest){
      player.fileId = latest.id;
      player.pos = latest.watchPct>0&&latest.watchPct<100 ? Math.floor(latest.dur*latest.watchPct/100) : 0;
    }
  }
  route = to;
  if (typeof writeRouteHash==='function') writeRouteHash(to);   // keep the URL hash in sync so views are linkable / refresh-safe
  render();
  if (to.view==='detail') ensureNsfwLoaded();   // the manual-override warning needs the current handling policy
  if (apiState.mode==='api' && to.view==='detail'){   // pull real videos + on-line pattern on entering a detail
    const s2 = streamers.find(x=>x.id===to.id);
    if (s2 && !s2._detailLoaded) loadDetail(s2.site, s2.user);
    if (s2 && !s2._schedLoaded) loadSchedule(s2.site, s2.user);
  }
  if (apiState.mode==='api' && to.view==='history') loadHistory();   // pull real deletion history
  if (to.view!=='settings'){ stopNsfwBackfillPoll(); stopNsfwModelDownloadPoll(); }   // never keep polling the backfill/model-download workers from another view
  if (to.view==='settings'){ loadDownloadDir(); loadMinVideoSize(); loadRecResolution(); loadNsfw(); if (auth.enabled) loadPasswordSettings(); }   // read the current download dir + cleanup threshold + recording resolution + NSFW toggle + password bounds (mock fills demo values)
  const sc=document.querySelector('.detail-scroll,.list-scroll,.settings-wrap');
  if(sc)sc.scrollTop=0;
}

/* ---------------- toast ---------------- */
function toast(text, type='ok'){
  const el = document.createElement('div');
  el.className = 'toast '+type;
  el.textContent = text;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>{el.style.transition='opacity .3s';el.style.opacity='0';setTimeout(()=>el.remove(),320)}, 3400);
}

/* ================= v2 API adapter (B1B) =================
 * Read-only: fetches the dashboard list + detail videos from /api/v2 and polls
 * at WEB_LIST_FREQUENCY. Falls back to the bundled mock data when no backend is
 * configured, so the standalone prototype keeps working with zero console noise.
 * Enable against a real backend by injecting, before the app scripts:
 *   <script>window.SM_CONFIG = { useApi: true };</script>
 * Optional SM_CONFIG: apiBase (default '/api/v2'), listFrequency (SECONDS —
 *   same contract as the backend's listFrequency field),
 *   useApi:'auto' (probe first, keep mock if unreachable). No SM_CONFIG = mock. */
const apiState = { mode:'mock', loading:false, error:false, pollMs:30000, timer:null };

function apiUrl(path){
  const base = (window.SM_CONFIG && window.SM_CONFIG.apiBase) || '/api/v2';
  // Resolve against location.origin (never carries credentials) so that opening
  // the console via http://user:pass@host/ doesn't make fetch() throw
  // "Request cannot be constructed from a URL that includes credentials".
  return new URL(base + path, location.origin).toString();
}
/* Session auth (v3.4): the backend answers 401 JSON {"error":"unauthorized"} on
 * /api/v2/* once the session Cookie is gone. Any API response carrying a 401
 * sends the console to the login page, guarded so concurrent in-flight requests
 * (and the poll timer) can't queue up repeated navigations or bounce us while
 * already sitting on /login. */
const LOGIN_PATH = '/login';
const LOGOUT_PATH = '/logout';
const auth = { enabled:false, redirecting:false, loggingOut:false };
function handleUnauthorized(){
  if (auth.redirecting) return true;
  auth.redirecting = true;
  stopPolling();
  stopEventStream();
  if (location.pathname !== LOGIN_PATH) location.replace(LOGIN_PATH);
  return true;
}
/* returns true when the response was a 401 and a redirect was triggered */
function checkUnauthorized(r){ return r && r.status===401 ? handleUnauthorized() : false; }
const API_TIMEOUT_MS = 15000;
async function apiGet(path){
  // AbortController timeout: a backend that stops answering must fail the
  // request (surfacing the error banner) instead of pinning it forever.
  const ctl = new AbortController();
  const timer = setTimeout(()=>ctl.abort(), API_TIMEOUT_MS);
  let r;
  try {
    r = await fetch(apiUrl(path), {headers:{'Accept':'application/json'}, credentials:'same-origin', signal:ctl.signal});
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok){
    checkUnauthorized(r);
    const err = new Error('HTTP '+r.status); err.status = r.status; throw err;
  }
  return r.json();
}
/* settings page "log out": POST /logout, then back to the login page. */
async function doLogout(){
  if (auth.loggingOut) return;
  auth.loggingOut = true; render();
  try {
    const r = await fetch(new URL(LOGOUT_PATH, location.origin).toString(), {
      method:'POST', credentials:'same-origin', headers:{'Accept':'application/json'},
    });
    if (!r.ok && r.status!==401){ auth.loggingOut = false; toast(t('logoutErr'), 'err'); render(); return; }
    auth.redirecting = true; stopPolling(); stopEventStream();
    location.replace(LOGIN_PATH);
  } catch(e){
    auth.loggingOut = false; toast(t('logoutErr'), 'err'); render();
  }
}
async function probeApi(){
  try {
    const r = await fetch(apiUrl('/streamers'), {headers:{'Accept':'application/json'}, credentials:'same-origin'});
    checkUnauthorized(r);   // session gone: go to /login instead of silently falling back to mock
    return r.ok;
  }
  catch(e){ return false; }
}

/* ---- contract → view-model mapping ---- */
const EMPTY_SCHEDULE = Array.from({length:7},()=>Array(24).fill(0));
function parseTs(v){
  if (v==null) return new Date(0);
  const n = typeof v==='number' ? v : Number(v);
  if (!isNaN(n) && (typeof v==='number' || String(v).trim()!=='')) return new Date(n<1e12 ? n*1000 : n);
  const d = new Date(v); return isNaN(d.getTime()) ? new Date(0) : d;
}
function tsToSecAgo(ts){
  if (ts==null) return 0;
  return Math.max(0, Math.floor((Date.now()-parseTs(ts).getTime())/1000));
}
function normStatus(raw){
  if (raw==null) return 'offline';
  const key = String(raw).toLowerCase();
  if (STATUS[key]) return key;   // already a frontend status key
  const map = {
    '200':'online', public:'online',
    '403':'private', private:'private',
    '404':'offline', offline:'offline',
    '410':'long_offline', long_offline:'long_offline',
    '429':'ratelimit', ratelimit:'ratelimit',
    '3':'error', error:'error',
    '400':'error', notexist:'error',
    '1403':'restricted', restricted:'restricted',
    '2':'offline', notrunning:'offline',
    '1':'checking', unknown:'checking',
  };
  return map[key] || 'offline';
}
function resolveStatus(item){
  const ms = String(item.monitorState||'active').toLowerCase();
  if (ms==='stopped') return 'stopped';
  if (item.running===false && ms==='active') return 'paused';   // paused = monitored but worker stopped
  if (item.recording) return 'recording';
  return normStatus(item.status);
}
/* Keeps item.site/siteslug as the entry's site id (the backend slug — what every
 * API path expects). Only sites with no SITES entry at all get a dynamic one, so a
 * known slug like "SC" no longer duplicates the static StripChat entry. */
function ensureSite(item){
  const slug = item.siteslug || item.site || 'unknown';
  if (!siteKeyOf(slug)){   // register unknown sites so rendering never crashes
    registerDynamicSite(slug, item.site);
  }
  return slug;
}
function mapStreamers(list){
  const prevById = {};
  streamers.forEach(s=>{ prevById[s.id]=s; });   // carry loaded detail across list polls
  return (list||[]).map(item=>{
    const slug = ensureSite(item);
    const user = item.username || item.user || '';
    const id = slug+'/'+user;
    const prev = prevById[id];
    const ms = String(item.monitorState||'active').toLowerCase();
    const s = {
      id, seed:id, site:slug, user, flag:'', url:item.url||'',
      status: resolveStatus(item),
      monitorState: ms==='stopped' ? 'unmonitored' : 'active',
      _monitorState: ms, _running: !!item.running, _statusText: item.statusText||'',
      recBytes: item.recordingBytes||0, recSince:null,
      recSpeedBps: item.recordingSpeedBps!=null ? item.recordingSpeedBps : null,   // live speed (bytes/s), null when unavailable
      recResolution: item.recordingResolution!=null ? item.recordingResolution : null,   // short side px, null when unknown
      _liveAvailable: !!item.liveAvailable,   // watch-while-recording tail is readable (L2 live viewer)
      lastCheckSec: tsToSecAgo(item.lastCheckTs),
      addedDaysAgo: null,
      _videoCount: item.videoCount!=null ? item.videoCount : null,
      _totalSize: item.totalSize!=null ? item.totalSize : null,
      _lastRecordedTs: item.lastRecordedTs!=null ? item.lastRecordedTs : null,
      files: [], temps: [], schedule: EMPTY_SCHEDULE, _detailLoaded:false, _detailError:false,
      _sched: null, _schedLoaded: false,   // /schedule payload (heat + honest metrics); lazily loaded with detail
    };
    if (prev && prev._detailLoaded){ s.files = prev.files; s.temps = prev.temps; s._detailLoaded = true; s._filesSig = prev._filesSig; }   // carry the file signature so loadDetail's guard can early-return while a video plays
    if (prev && prev._schedLoaded){ s.schedule = prev.schedule; s._sched = prev._sched; s._schedLoaded = true; }   // carry real schedule across list polls so the sparkline stays truthful
    return s;
  });
}

/* ---- fetch + poll ----
 * Two-part staleness strategy:
 * - Every refreshStreamers() call gets a generation number; a response only
 *   applies while its generation is still the newest, so a slow poll response
 *   from BEFORE a write can never overwrite the post-write list an explicit
 *   refresh just fetched.
 * - Only the poll timer skips while a request is in flight (pollRefresh);
 *   explicit refreshes after writes and the error-banner retry always fire. */
let refreshSeq = 0;
let refreshInFlight = false;
async function refreshStreamers(){
  const seq = ++refreshSeq;
  refreshInFlight = true;
  try {
    const data = await apiGet('/streamers');
    if (seq !== refreshSeq) return;   // superseded by a newer refresh
    streamers = mapStreamers(Array.isArray(data) ? data : (data && data.streamers) || []);
    apiState.error = false; apiState.loading = false;
    if (pendingRoute && typeof resolvePendingRoute==='function'){ resolvePendingRoute(); return; }   // land a deep-linked view now that data is in
    render();
    if (route.view==='detail'){ const s = streamers.find(x=>x.id===route.id); if (s){ loadDetail(s.site, s.user); if (!s._schedLoaded) loadSchedule(s.site, s.user); } }
  } catch(e){
    if (seq !== refreshSeq) return;   // a newer refresh owns the UI state now
    apiState.loading = false; apiState.error = true;
    render();   // keep last data on screen; banner surfaces the failure
  } finally {
    if (seq === refreshSeq) refreshInFlight = false;
  }
}
function pollRefresh(){ if (!refreshInFlight) refreshStreamers(); }
/* V37C: short-cycle duration backfill. When the detail payload still has finished
 * (non-recording) files with an unknown duration, re-pull once after a short delay so
 * the "--:--" placeholders resolve to real times. Single self-arming timer (deduped),
 * stops once all durations are present or the user leaves this detail — never stacks
 * with the 30s list poll into a request storm. */
let detailDurTimer = null;
function clearDurBackfill(){ if (detailDurTimer){ clearTimeout(detailDurTimer); detailDurTimer = null; } }
function scheduleDurBackfill(s){
  clearDurBackfill();
  if (!(route.view==='detail' && route.id===s.id)) return;
  const needs = s.files.some(f=>!f._recording && f.dur==null);
  if (!needs) return;
  detailDurTimer = setTimeout(()=>{
    detailDurTimer = null;
    if (route.view==='detail' && route.id===s.id) loadDetail(s.site, s.user);
  }, 5000);
}
async function loadDetail(site, user){
  try {
    const data = await apiGet('/streamers/'+encodeURIComponent(site)+'/'+encodeURIComponent(user));
    const s = streamers.find(x=>x.id===site+'/'+user);
    if (!s) return;
    const vids = (data && data.videos) || [];
    const sig = vids.map(v=>v.filename+':'+(v.size||0)+':'+(v.recording?1:0)).join('|');
    if (data){ if (data.totalSize!=null) s._totalSize = data.totalSize; if (data.videoCount!=null) s._videoCount = data.videoCount; }
    const vidEl = document.getElementById('pvideo');
    if (s._detailLoaded && s._filesSig===sig && vidEl && !vidEl.paused){
      apiState.error = false; return;   // playing a real video and nothing changed — don't rebuild the <video>
    }
    const oldById = {}; s.files.forEach(f=>{ oldById[f.id]=f; });   // preserve marks/progress
    s.files = vids.map(v=>{
      const id = v.filename; const old = oldById[id];
      return { id, dt: parseTs(v.mtime), dur: v.duration!=null ? v.duration : (old && old.dur!=null ? old.dur : null), size: v.size||0,
        watchPct: old?old.watchPct:0, marked: old?!!old.marked:false,
        lastWatched: old?old.lastWatched:null, _filename: v.filename, _url: v.url||'', _recording: !!v.recording,
        _cls: v.classification||null, _inRecycle: !!v.inRecycle };
    }).sort((a,b)=>b.dt-a.dt);
    s.temps = []; s._detailLoaded = true; s._filesSig = sig; s._detailError = false;
    if (route.view==='detail' && route.id===s.id && !player.fileId && s.files[0]) setPlayerFile(s.files[0]);
    // duration updates flow through this non-playing rebuild + render; the "playing → don't
    // rebuild <video>" guard above stays intact. Short-cycle re-pull while durations backfill.
    apiState.error = false; render();
    scheduleDurBackfill(s);
  } catch(e){
    const s = streamers.find(x=>x.id===site+'/'+user);
    if (s && !s._detailLoaded) s._detailError = true;   // no data yet: surface an inline error + retry instead of a forever spinner
    apiState.error = true; render();
  }
}
/* pull the real on-line pattern (/schedule) for one streamer: heat + honest
 * metrics (commonHours / avgSessionMin / days7OnlineHours / sampleDays). Non-
 * critical — a failure just leaves EMPTY_SCHEDULE and the "gathering data" state.
 * Only meaningful in API mode; mock keeps its generated mkSchedule heat. */
async function loadSchedule(site, user){
  try {
    const data = await apiGet('/streamers/'+encodeURIComponent(site)+'/'+encodeURIComponent(user)+'/schedule');
    const s = streamers.find(x=>x.id===site+'/'+user);
    if (!s || !data) return;
    s.schedule = Array.isArray(data.heat) ? data.heat : EMPTY_SCHEDULE;
    s._sched = {
      observedSinceTs: data.observedSinceTs!=null ? data.observedSinceTs : null,
      sampleDays: data.sampleDays!=null ? data.sampleDays : 0,
      days7OnlineHours: data.days7OnlineHours!=null ? data.days7OnlineHours : null,
      avgSessionMin: data.avgSessionMin!=null ? data.avgSessionMin : null,
      commonHours: Array.isArray(data.commonHours) ? data.commonHours : null,
    };
    s._schedLoaded = true;
    render();
  } catch(e){ /* schedule is non-critical; leave EMPTY_SCHEDULE + accumulating state */ }
}
/* retry a failed initial detail load — invoked from the inline retry button in the detail view */
function retryDetail(){
  const s = streamers.find(x=>x.id===route.id);
  if (!s) return;
  s._detailError = false; render();   // back to the spinner while we re-fetch
  loadDetail(s.site, s.user);
}
async function loadBaseSettings(){
  try {
    const b = await apiGet('/basesettings');
    if (!b) return;
    const disk = b.disk || b.diskSpace || b.space || null;
    if (disk){
      if (disk.total!=null) DISK.total = disk.total;
      if (disk.used!=null) DISK.used = disk.used;
      else if (disk.free!=null && disk.total!=null) DISK.used = disk.total - disk.free;
    }
    const f = b.listFrequency!=null ? b.listFrequency : (b.webListFrequency!=null ? b.webListFrequency : b.WEB_LIST_FREQUENCY);
    // Contract: the backend field is SECONDS, always. The old n>1000 unit
    // heuristic flipped e.g. 1800s into 1.8s polling — a request storm.
    if (f!=null && !isNaN(Number(f))){ const n = Number(f); if (n>0) apiState.pollMs = Math.max(2000, n*1000); }
    // Only show the settings "log out" entry when the backend actually requires
    // a password; missing flag (older backend / no password) means no button.
    auth.enabled = b.authEnabled===true;
  } catch(e){ /* basesettings is optional */ }
}
function startPolling(){ stopPolling(); apiState.timer = setInterval(pollRefresh, apiState.pollMs); }
function stopPolling(){ if (apiState.timer){ clearInterval(apiState.timer); apiState.timer=null; } }

/* ================= live state push (SSE, P2) =================
 * /api/v2/events streams lightweight {site,user,status,running,recording,
 * monitorState} changes, so a recording start/stop reaches the dashboard in
 * about a second instead of waiting for the list poll. Events only patch the
 * matching entry of `streamers` and re-render (the dashboard render is keyed
 * and incremental); nothing here re-pulls the list on its own.
 * Polling is never replaced or slowed: the list poll keeps the configured
 * cadence while the stream is up, because numbers the events deliberately don't
 * carry (recording bytes/speed/resolution, videoCount/totalSize) only refresh
 * with it. After SSE_MAX_RETRIES failed reconnects the console stops trying and
 * stays on plain polling. */
const SSE_PATH = '/events';
const SSE_MAX_RETRIES = 5;
const sse = {es:null, open:false, fails:0, retryTimer:null, refreshTimer:null};

function startEventStream(){
  if (apiState.mode!=='api' || sse.es || auth.redirecting) return;
  if (typeof window==='undefined' || typeof window.EventSource!=='function') return;   // no SSE support: polling only
  let es;
  try { es = new EventSource(apiUrl(SSE_PATH), {withCredentials:true}); }
  catch(e){ return; }
  sse.es = es;
  es.onopen = ()=>{ sse.open = true; sse.fails = 0; if (!apiState.timer) startPolling(); };   // keep the configured cadence; only re-arm if the poll isn't running
  es.addEventListener('state', applyStateEvent);
  es.addEventListener('ping', ()=>{});   // heartbeat: keeps the connection (and its liveness check) honest
  // Covers a dropped connection, a 503 over the server-side connection cap and
  // a stream the backend closed. We always close and reconnect ourselves so the
  // backoff is ours, not EventSource's fixed retry.
  es.onerror = ()=>{ sseDown(); };
}
function stopEventStream(){
  if (sse.retryTimer){ clearTimeout(sse.retryTimer); sse.retryTimer = null; }
  if (sse.refreshTimer){ clearTimeout(sse.refreshTimer); sse.refreshTimer = null; }
  if (sse.es){ try { sse.es.close(); } catch(e){} sse.es = null; }
  sse.open = false;
}
function sseDown(){
  if (sse.es){ try { sse.es.close(); } catch(e){} sse.es = null; }
  sse.open = false;
  sse.fails++;
  if (auth.redirecting) return;
  startPolling();   // fall back to the normal cadence while the stream is down
  if (sse.fails > SSE_MAX_RETRIES) return;   // give up: plain polling from here on
  const delay = Math.min(60000, 2000 * Math.pow(2, sse.fails-1));
  if (sse.retryTimer) clearTimeout(sse.retryTimer);
  sse.retryTimer = setTimeout(()=>{ sse.retryTimer = null; startEventStream(); }, delay);
}
/* a recording start/stop also changes file counts / live availability, which the
 * event payload deliberately doesn't carry — pull them once, debounced */
function scheduleSseRefresh(){
  if (sse.refreshTimer) return;
  sse.refreshTimer = setTimeout(()=>{ sse.refreshTimer = null; pollRefresh(); }, 2000);
}
function applyStateEvent(ev){
  let data = null;
  try { data = JSON.parse(ev.data); } catch(e){ return; }
  const changed = (data && data.changed) || [];
  const removed = (data && data.removed) || [];
  if (removed.length){ pollRefresh(); return; }   // membership change: authoritative re-pull
  let dirty = false, recToggled = false, unknown = false;
  changed.forEach(item=>{
    const s = streamers.find(x=>x.id===(item.site||'')+'/'+(item.user||''));
    if (!s){ unknown = true; return; }   // streamer we don't know yet (just added)
    const ms = String(item.monitorState||'active').toLowerCase();
    const next = resolveStatus(item);
    if (s.status===next && s._running===!!item.running && s._monitorState===ms) return;
    if ((s.status==='recording') !== (next==='recording')) recToggled = true;
    s.status = next; s._running = !!item.running; s._monitorState = ms;
    s.monitorState = ms==='stopped' ? 'unmonitored' : 'active';
    dirty = true;
  });
  if (unknown){ pollRefresh(); return; }
  if (dirty) render();
  if (recToggled) scheduleSseRefresh();
}

/* ================= v2 write operations (B2B) =================
 * Wire dashboard/detail/history actions to the real /api/v2 write endpoints.
 * Each op does an optimistic local tweak where cheap, calls the backend, then
 * re-pulls the authoritative list; on failure it reverts and shows an error
 * toast. All of these are only reached when apiState.mode==='api' — mock mode
 * keeps its original in-memory behaviour untouched. */
async function apiSend(method, path, body){
  const opts = {method, headers:{'Accept':'application/json'}, credentials:'same-origin'};
  if (body!==undefined){ opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(body); }
  const r = await fetch(apiUrl(path), opts);
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch(e){ data = null; }
  if (!r.ok){
    checkUnauthorized(r);
    const err = new Error('HTTP '+r.status);
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}
/* backend error payloads are {error:"..."} — fall back to the generic toast text */
function apiErrText(e){
  const msg = e && e.data && e.data.error;
  return (typeof msg==='string' && msg.trim()) ? msg : t('apiOpError');
}
function streamerPath(s){ return '/streamers/'+encodeURIComponent(s.site)+'/'+encodeURIComponent(s.user); }
function streamerTargets(list){ return list.map(s=>({site:s.site, user:s.user})); }
function selectedTargets(){ return streamerTargets(streamers.filter(s=>selected.has(s.id))); }

/* pause / resume / stop-recording(archive) / reactivate — one toggle button */
async function apiToggleStreamer(s){
  const wasStopped = s.status==='stopped';
  const wasPaused = s.status==='paused';
  const action = wasStopped ? 'reactivate' : wasPaused ? 'resume' : 'pause';
  const prev = s.status;
  s.status = action==='pause' ? 'paused' : 'checking';   // optimistic
  render();
  try {
    await apiSend('POST', streamerPath(s)+'/'+action);
    toast(action==='pause' ? t('pausedToast')(s.user) : t('resumedToast')(s.user));
    await refreshStreamers();
  } catch(e){
    s.status = prev; render();
    toast(t('apiOpError'), 'err');
  }
}

/* remove modal confirm: purge = 彻底删除 (DELETE + files + history); else 停止录播（保留档案） */
async function apiRemoveStreamer(s, purge){
  try {
    if (purge){
      await apiSend('DELETE', streamerPath(s), {deleteFiles:true});
      toast(t('purgedToast')(s.user), 'warn');
      await loadHistory();
      await refreshStreamers();
      nav({view:'dashboard'});
    } else {
      await apiSend('POST', streamerPath(s)+'/stop-recording');
      toast(t('stopMonToast')(s.user));
      await refreshStreamers();
    }
  } catch(e){ toast(t('apiOpError'), 'err'); render(); }
}

/* batch: action ∈ pause|resume|stopRecording|purge */
async function apiBatchTargets(action, targets, toastText, warn){
  if (!targets.length) return;
  try {
    await apiSend('POST', '/batch', {action, targets});
    if (action==='stopRecording' || action==='purge') selected.clear();
    toast(toastText, warn?'warn':'ok');
    if (action==='purge') await loadHistory();
    await refreshStreamers();
  } catch(e){ toast(t('apiOpError'), 'err'); render(); }
}

/* add streamer (manual/url both resolve to site+user); re-adds an archived entry via reactivate */
async function apiAddStreamer(siteKey, user){
  // list entries carry the backend siteslug in s.site, so match on the slug
  const key = siteKeyOf(siteKey);
  const meta = key ? SITES[key] : null;
  const slug = (meta && meta.slug) || siteKey;
  const siteName = (meta && meta.name) || slug;
  const existing = streamers.find(s=>s.site.toLowerCase()===String(slug).toLowerCase() && s.user.toLowerCase()===user.toLowerCase());
  if (existing){
    if (existing.status==='stopped'){
      try { await apiSend('POST', streamerPath(existing)+'/reactivate'); toast(t('addDupResumed')(existing.user)); await refreshStreamers(); }
      catch(e){ toast(apiErrText(e), 'err'); }
    } else {
      toast(t('addDup')(existing.user), 'warn');
    }
    addDraft = {url:'', user:''};
    nav({view:'detail', id:existing.id});
    return;
  }
  try {
    await apiSend('POST', '/streamers', {site:slug, username:user});
    addMsg = null; addDraft = {url:'', user:''};
    toast(t('addOk')(user, siteName));
    await refreshStreamers();
  } catch(e){ addMsg = {type:'err', text:apiErrText(e)}; toast(apiErrText(e), 'err'); render(); }
}

/* ---- history (GET list, readd, delete) ---- */
async function loadHistory(){
  try {
    const data = await apiGet('/history');
    const list = Array.isArray(data) ? data : (data && data.history) || [];
    history = list.map((h,i)=>{
      const slug = ensureSite(h);
      const user = h.username || h.user || '';
      return {
        _idx: h.idx!=null ? h.idx : i,
        /* raw identity for index-shift-safe delete/readd (the backend verifies
         * it against the entry at _idx and falls back to identity lookup) */
        _identity: {site: h.site, username: h.username, deletedAt: h.deletedAt},
        user, site: slug, url: h.url || '',
        avatarDataUrl: genAvatar(slug+'/'+user, user),
        addedDate: (h.addedTs!=null || h.addedDate!=null) ? parseTs(h.addedTs!=null ? h.addedTs : h.addedDate) : null,
        removedDate: parseTs(h.removedTs!=null ? h.removedTs : (h.deletedTs!=null ? h.deletedTs : (h.deletedAt!=null ? h.deletedAt : h.removedDate))),
      };
    });
    if (route.view==='history') render();
  } catch(e){ apiState.error = true; render(); }
}
async function apiHistoryReadd(idx, h){
  try {
    await apiSend('POST', '/history/'+idx+'/readd', h && h._identity ? h._identity : undefined);
    toast(t('histReaddToast')(h.user));
    await loadHistory();
    await refreshStreamers();
    nav({view:'dashboard'});
  } catch(e){ toast(t('apiOpError'), 'err'); }
}
async function apiHistoryDelete(i){
  const h = history[i];
  const idx = h && h._idx!=null ? h._idx : i;
  try { await apiSend('DELETE', '/history/'+idx, h && h._identity ? h._identity : undefined); toast(t('histRemovedToast'), 'warn'); await loadHistory(); }
  catch(e){ toast(t('apiOpError'), 'err'); }
}

/* ---- download directory (server-side, NB4): read + validate-and-save ---- */
async function loadDownloadDir(){
  if (apiState.mode!=='api'){   // mock: show a representative path, no backend call
    if (!downloadDir.loaded){ downloadDir.path = MOCK_DOWNLOAD_DIR; downloadDir.draft = MOCK_DOWNLOAD_DIR; downloadDir.loaded = true; }
    return;
  }
  downloadDir.loading = true; downloadDir.error = null; render();
  try {
    const d = await apiGet('/settings/download-dir');
    downloadDir.path = (d && d.path) || '';
    downloadDir.draft = downloadDir.path;
    downloadDir.loaded = true; downloadDir.loading = false; downloadDir.error = null;
    render();
  } catch(e){
    downloadDir.loading = false; downloadDir.error = t('dlDirLoadErr'); render();
  }
}
function dlDirErrText(data){
  const map = {empty:'dlDirErrEmpty', create_failed:'dlDirErrCreate', not_dir:'dlDirErrNotDir', not_writable:'dlDirErrNotWritable'};
  const key = data && map[data.code];
  if (key) return t(key);
  return (data && data.error) || t('dlDirErrGeneric');
}
async function saveDownloadDir(){
  const val = (downloadDir.draft||'').trim();
  if (!val){ downloadDir.error = t('dlDirErrEmpty'); render(); return; }
  if (apiState.mode!=='api'){   // mock: accept locally, show success toast
    downloadDir.path = val; downloadDir.draft = val; downloadDir.error = null;
    toast(t('dlDirSavedToast')); render(); return;
  }
  downloadDir.saving = true; downloadDir.error = null; render();
  try {
    const r = await fetch(apiUrl('/settings/download-dir'), {
      method:'PUT', credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body: JSON.stringify({path: val}),
    });
    const txt = await r.text();
    const data = txt ? JSON.parse(txt) : {};
    if (!r.ok){ checkUnauthorized(r); downloadDir.saving = false; downloadDir.error = dlDirErrText(data); render(); return; }
    downloadDir.path = data.path || val; downloadDir.draft = downloadDir.path;
    downloadDir.saving = false; downloadDir.error = null;
    toast(t('dlDirSavedToast')); render();
  } catch(e){
    downloadDir.saving = false; downloadDir.error = t('dlDirErrGeneric'); render();
  }
}

/* ---- min-video-size cleanup threshold (server-side): read + validate-and-save ---- */
async function loadMinVideoSize(){
  if (apiState.mode!=='api'){   // mock: show a representative value, no backend call
    if (!minVid.loaded){ minVid.value = MOCK_MIN_VIDEO_SIZE_MB; minVid.draft = String(MOCK_MIN_VIDEO_SIZE_MB); minVid.loaded = true; }
    return;
  }
  minVid.loading = true; minVid.error = null; render();
  try {
    const d = await apiGet('/settings/min-video-size');
    minVid.value = (d && d.minVideoSizeMb!=null) ? d.minVideoSizeMb : 0;
    minVid.draft = String(minVid.value);
    minVid.loaded = true; minVid.loading = false; minVid.error = null;
    render();
  } catch(e){
    minVid.loading = false; minVid.error = t('minVidLoadErr'); render();
  }
}
async function saveMinVideoSize(){
  const raw = (minVid.draft||'').trim();
  const n = Number(raw);
  if (raw==='' || !isFinite(n) || n<0 || n>10240){ minVid.error = t('minVidErrInvalid'); render(); return; }
  if (apiState.mode!=='api'){   // mock: accept locally, show success toast
    minVid.value = n; minVid.draft = String(n); minVid.error = null;
    toast(t('minVidSavedToast')); render(); return;
  }
  minVid.saving = true; minVid.error = null; render();
  try {
    const r = await fetch(apiUrl('/settings/min-video-size'), {
      method:'PUT', credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body: JSON.stringify({minVideoSizeMb: n}),
    });
    const txt = await r.text();
    const data = txt ? JSON.parse(txt) : {};
    if (!r.ok){ checkUnauthorized(r); minVid.saving = false; minVid.error = (data && data.code==='invalid') ? t('minVidErrInvalid') : ((data && data.error) || t('minVidErrGeneric')); render(); return; }
    minVid.value = (data && data.minVideoSizeMb!=null) ? data.minVideoSizeMb : n;
    minVid.draft = String(minVid.value);
    minVid.saving = false; minVid.error = null;
    toast(t('minVidSavedToast')); render();
  } catch(e){
    minVid.saving = false; minVid.error = t('minVidErrGeneric'); render();
  }
}

/* ---- account (user name + password) change (server-side) ----
 * GET reports the current user name, whether auth is on and the length bounds —
 * the password itself never leaves the backend. The inputs are validated
 * locally so a typo never reaches the API. */
async function loadPasswordSettings(){
  if (apiState.mode!=='api'){ pwd.loaded = true; return; }   // mock: bounds stay at the defaults
  try {
    const d = await apiGet('/settings/password');
    if (d && d.minLength!=null) pwd.minLength = d.minLength;
    if (d && d.maxLength!=null) pwd.maxLength = d.maxLength;
    if (d && d.usernameMinLength!=null) pwd.usernameMinLength = d.usernameMinLength;
    if (d && d.usernameMaxLength!=null) pwd.usernameMaxLength = d.usernameMaxLength;
    if (d && typeof d.username==='string'){
      pwd.username = d.username;
      if (!pwd.usernameDraft) pwd.usernameDraft = d.username;   // never clobber what is being typed
    }
    if (d && d.authEnabled!=null) auth.enabled = d.authEnabled===true;
    pwd.loaded = true; render();
  } catch(e){ /* the panel keeps working with the default bounds */ }
}
async function savePassword(){
  const cur = pwd.current, next = pwd.next, confirm = pwd.confirm;
  const user = (pwd.usernameDraft||'').trim();
  const userChanged = user !== pwd.username;
  const pwdChanged = !!(next || confirm);
  if (!cur){ pwd.error = 'pwdErrCurrentRequired'; render(); return; }
  if (!userChanged && !pwdChanged){ pwd.error = 'pwdErrNothing'; render(); return; }
  if (userChanged && (user.length < pwd.usernameMinLength || user.length > pwd.usernameMaxLength)){
    pwd.error = 'pwdErrUsername'; render(); return;
  }
  if (pwdChanged){
    if (next.length < pwd.minLength || next.length > pwd.maxLength){ pwd.error = 'pwdErrLength'; render(); return; }
    if (next !== confirm){ pwd.error = 'pwdErrMismatch'; render(); return; }
  }
  if (apiState.mode!=='api'){   // mock: accept locally, show success toast
    if (userChanged) pwd.username = user;
    pwd.usernameDraft = pwd.username;
    pwd.current = pwd.next = pwd.confirm = ''; pwd.error = null;
    toast(t('pwdSavedToast')); render(); return;
  }
  const body = {currentPassword: cur};
  if (userChanged) body.newUsername = user;
  if (pwdChanged) body.newPassword = next;
  pwd.saving = true; pwd.error = null; render();
  try {
    const r = await fetch(apiUrl('/settings/password'), {
      method:'PUT', credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const txt = await r.text();
    const data = txt ? JSON.parse(txt) : {};
    if (!r.ok){
      checkUnauthorized(r);
      pwd.saving = false;
      const code = data && data.code;
      pwd.error = code==='wrong_password' ? 'pwdErrWrongCurrent'
                : code==='invalid' ? 'pwdErrLength'
                : code==='invalid_username' ? 'pwdErrUsername'
                : code==='disabled' ? 'pwdErrDisabled' : 'pwdErrGeneric';
      toast(t(pwd.error), 'err'); render(); return;
    }
    if (data && typeof data.username==='string') pwd.username = data.username;
    else if (userChanged) pwd.username = user;
    pwd.usernameDraft = pwd.username;
    pwd.current = pwd.next = pwd.confirm = '';
    pwd.saving = false; pwd.error = null;
    toast(t('pwdSavedToast')); render();
  } catch(e){
    pwd.saving = false; pwd.error = 'pwdErrGeneric'; toast(t('pwdErrGeneric'), 'err'); render();
  }
}

/* ---- NSFW analysis toggle (server-side): read + change-to-save ---- */
function _applyNsfwPayload(d){
  nsfw.enabled = !!(d && d.enabled);
  if (d && NSFW_ACTIONS.includes(d.action)) nsfw.action = d.action;
  if (d && d.recycleRetentionDays!=null) nsfw.recycleRetentionDays = d.recycleRetentionDays;
  if (d && d.scanIntervalHours!=null) nsfw.scanIntervalHours = d.scanIntervalHours;
  if (d && d.threshold!=null) nsfw.threshold = d.threshold;
  nsfw.retentionDraft = String(nsfw.recycleRetentionDays);
  nsfw.scanDraft = String(nsfw.scanIntervalHours);
  nsfw.thresholdDraft = String(nsfw.threshold);
  nsfw.modelPresent = !!(d && d.modelPresent);
  nsfw.runtimeAvailable = !!(d && d.runtimeAvailable);
}
async function loadNsfw(){
  if (apiState.mode!=='api'){   // mock: show a representative state, no backend call
    if (!nsfw.loaded){ Object.assign(nsfw, MOCK_NSFW); nsfw.retentionDraft = String(nsfw.recycleRetentionDays); nsfw.scanDraft = String(nsfw.scanIntervalHours); nsfw.thresholdDraft = String(nsfw.threshold); nsfw.loaded = true; }
    if (nsfw.enabled){ loadNsfwBackfill(); loadNsfwModels(); }
    return;
  }
  nsfw.loading = true; nsfw.error = null; render();
  try {
    const d = await apiGet('/settings/nsfw');
    _applyNsfwPayload(d);
    nsfw.loaded = true; nsfw.loading = false; nsfw.error = null;
    render();
    if (nsfw.enabled){ loadNsfwBackfill(); loadNsfwModels(); }   // both blocks are only shown while NSFW is on
  } catch(e){
    nsfw.loading = false; nsfw.error = 'nsfwLoadErr'; render();
  }
}
/* the detail view needs the current handling policy to warn before a manual
 * non-adult override; pull it once (settings payload only, no backfill poll) */
async function ensureNsfwLoaded(){
  if (nsfw.loaded) return;
  if (apiState.mode!=='api'){
    Object.assign(nsfw, MOCK_NSFW);
    nsfw.retentionDraft = String(nsfw.recycleRetentionDays);
    nsfw.scanDraft = String(nsfw.scanIntervalHours);
    nsfw.thresholdDraft = String(nsfw.threshold);
    nsfw.loaded = true; return;
  }
  try { _applyNsfwPayload(await apiGet('/settings/nsfw')); nsfw.loaded = true; } catch(e){ /* keep defaults; the warning just won't show */ }
}
/* patch may contain enabled / action / recycleRetentionDays / scanIntervalHours /
 * threshold; the full current config is always sent so the server state stays coherent. */
async function saveNsfw(patch){
  const body = {
    enabled: nsfw.enabled===null ? false : nsfw.enabled,
    action: nsfw.action,
    recycleRetentionDays: nsfw.recycleRetentionDays,
    scanIntervalHours: nsfw.scanIntervalHours,
    threshold: nsfw.threshold,
  };
  Object.assign(body, patch||{});
  const toggled = !patch || ('enabled' in patch);
  if (apiState.mode!=='api'){   // mock: accept locally, show success toast
    Object.assign(nsfw, body);
    nsfw.retentionDraft = String(nsfw.recycleRetentionDays);
    nsfw.scanDraft = String(nsfw.scanIntervalHours);
    nsfw.thresholdDraft = String(nsfw.threshold);
    nsfw.error = null;
    if (toggled){
      if (nsfw.enabled){ loadNsfwBackfill(); loadNsfwModels(); }
      else { stopNsfwBackfillPoll(); finishMockNsfwBackfill(); stopNsfwModelDownloadPoll(); finishMockNsfwModelDl(); }
    }
    toast(t(toggled ? (nsfw.enabled ? 'nsfwOnToast' : 'nsfwOffToast') : 'nsfwSavedToast')); render(); return;
  }
  nsfw.saving = true; nsfw.error = null; render();
  try {
    const r = await fetch(apiUrl('/settings/nsfw'), {
      method:'PUT', credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const txt = await r.text();
    const data = txt ? JSON.parse(txt) : {};
    if (!r.ok){ checkUnauthorized(r); nsfw.saving = false; nsfw.error = (data && data.code==='invalid') ? 'nsfwErrInvalid' : ((data && data.error) || 'nsfwErrGeneric'); render(); return; }
    _applyNsfwPayload(data);
    nsfw.saving = false; nsfw.error = null;
    if (toggled){   // switching NSFW off stops the worker server-side too
      if (nsfw.enabled){ loadNsfwBackfill(); loadNsfwModels(); }
      else { stopNsfwBackfillPoll(); nsfwBackfill.running = false; nsfwBackfill.currentFile = null; stopNsfwModelDownloadPoll(); }
    }
    toast(t(toggled ? (nsfw.enabled ? 'nsfwOnToast' : 'nsfwOffToast') : 'nsfwSavedToast')); render();
  } catch(e){
    nsfw.saving = false; nsfw.error = 'nsfwErrGeneric'; render();
  }
}

/* ---- NSFW backfill (historic-file analysis) ----
 * Server-owned serial worker; the console only starts/cancels it and polls the
 * progress payload {running,total,done,failed,current_file,started_ts,pending}.
 * The poll timer only lives while the settings view is open (see nav/stopNsfwBackfillPoll)
 * so leaving the page never keeps requesting. error holds an i18n KEY. */
const NSFW_BACKFILL_PATH = '/settings/nsfw/backscan';
let nsfwBackfill = {running:false, total:0, done:0, failed:0, currentFile:null, pending:null,
                    starting:false, cancelling:false, error:null, loaded:false, timer:null, mockTimer:null};
function _applyBackfillPayload(d){
  nsfwBackfill.running = !!(d && d.running);
  nsfwBackfill.total = (d && d.total!=null) ? d.total : 0;
  nsfwBackfill.done = (d && d.done!=null) ? d.done : 0;
  nsfwBackfill.failed = (d && d.failed!=null) ? d.failed : 0;
  nsfwBackfill.currentFile = (d && d.current_file) ? d.current_file : null;
  nsfwBackfill.pending = (d && d.pending!=null) ? d.pending : null;
  nsfwBackfill.loaded = true;
}
async function loadNsfwBackfill(){
  if (apiState.mode!=='api'){   // mock: representative pending count, no backend call
    if (!nsfwBackfill.loaded){ nsfwBackfill.pending = MOCK_NSFW_BACKFILL_PENDING; nsfwBackfill.loaded = true; }
    syncNsfwBackfillPoll();
    return;
  }
  try {
    _applyBackfillPayload(await apiGet(NSFW_BACKFILL_PATH+'/progress'));
    nsfwBackfill.error = null;
  } catch(e){
    nsfwBackfill.loaded = true; nsfwBackfill.error = 'nsfwBackfillLoadErr';
  }
  syncNsfwBackfillPoll();
  render();
}
async function startNsfwBackfill(){
  if (nsfwBackfill.starting || nsfwBackfill.running) return;
  if (apiState.mode!=='api'){   // mock: walk a fake queue so the UI can be exercised standalone
    const total = nsfwBackfill.pending || 0;
    if (!total){ toast(t('nsfwBackfillNoneToast')); return; }
    nsfwBackfill.running = true; nsfwBackfill.total = total; nsfwBackfill.done = 0; nsfwBackfill.failed = 0;
    nsfwBackfill.currentFile = 'mock_' + (total) + '.mp4'; nsfwBackfill.error = null;
    if (nsfwBackfill.mockTimer) clearInterval(nsfwBackfill.mockTimer);
    nsfwBackfill.mockTimer = setInterval(()=>{
      nsfwBackfill.done++;
      nsfwBackfill.currentFile = 'mock_' + (nsfwBackfill.total - nsfwBackfill.done) + '.mp4';
      if (nsfwBackfill.done>=nsfwBackfill.total) finishMockNsfwBackfill();
      render();
    }, 1200);
    render(); return;
  }
  nsfwBackfill.starting = true; nsfwBackfill.error = null; render();
  try {
    _applyBackfillPayload(await apiSend('POST', NSFW_BACKFILL_PATH+'/start'));
    nsfwBackfill.starting = false;
    syncNsfwBackfillPoll(); render();
  } catch(e){
    nsfwBackfill.starting = false;
    nsfwBackfill.error = (e && e.data && e.data.code==='disabled') ? 'nsfwBackfillDisabledErr' : 'nsfwBackfillErrGeneric';
    render();
  }
}
async function cancelNsfwBackfill(){
  if (nsfwBackfill.cancelling) return;
  if (apiState.mode!=='api'){ finishMockNsfwBackfill(); toast(t('nsfwBackfillCancelledToast'), 'warn'); render(); return; }
  nsfwBackfill.cancelling = true; nsfwBackfill.error = null; render();
  try {
    _applyBackfillPayload(await apiSend('POST', NSFW_BACKFILL_PATH+'/cancel'));
    nsfwBackfill.cancelling = false;
    syncNsfwBackfillPoll(); render();
  } catch(e){
    nsfwBackfill.cancelling = false; nsfwBackfill.error = 'nsfwBackfillErrGeneric'; render();
  }
}
function finishMockNsfwBackfill(){
  if (nsfwBackfill.mockTimer){ clearInterval(nsfwBackfill.mockTimer); nsfwBackfill.mockTimer = null; }
  nsfwBackfill.pending = Math.max(0, (nsfwBackfill.total||0) - (nsfwBackfill.done||0));
  nsfwBackfill.running = false; nsfwBackfill.currentFile = null;
}
/* poll while a backfill is running and the settings view is open; the same call
 * also refreshes the pending count once the worker settles. */
function syncNsfwBackfillPoll(){
  const want = route.view==='settings' && nsfw.enabled && nsfwBackfill.running && apiState.mode==='api';
  if (want && !nsfwBackfill.timer){
    nsfwBackfill.timer = setInterval(pollNsfwBackfill, 2000);
  } else if (!want && nsfwBackfill.timer){
    clearInterval(nsfwBackfill.timer); nsfwBackfill.timer = null;
  }
}
async function pollNsfwBackfill(){
  if (route.view!=='settings'){ stopNsfwBackfillPoll(); return; }
  const wasRunning = nsfwBackfill.running;
  try {
    _applyBackfillPayload(await apiGet(NSFW_BACKFILL_PATH+'/progress'));
    nsfwBackfill.error = null;
  } catch(e){ return; }   // transient failure: keep the last known state and retry on the next tick
  if (wasRunning && !nsfwBackfill.running) toast(t('nsfwBackfillDoneToast'));
  syncNsfwBackfillPoll();
  render();
}
function stopNsfwBackfillPoll(){
  if (nsfwBackfill.timer){ clearInterval(nsfwBackfill.timer); nsfwBackfill.timer = null; }
  if (nsfwBackfill.mockTimer){ clearInterval(nsfwBackfill.mockTimer); nsfwBackfill.mockTimer = null; }
}

/* ---- NSFW model management ----
 * Local .onnx files in the model directory plus the curated download catalog.
 * Activate/delete are confirmed through a modal; the download runs server-side
 * in one background thread and is polled like the backfill worker (settings
 * view only, see stopNsfwModelDownloadPoll). error holds an i18n KEY. */
const NSFW_MODELS_PATH = '/settings/nsfw/models';
let nsfwModels = {models:[], catalog:[], modelsDir:'', busyFile:null,
                  error:null, loaded:false};
let nsfwModelDl = {running:false, catalogId:null, filename:null, totalBytes:0, doneBytes:0,
                   starting:null, cancelling:false, error:null, timer:null, mockTimer:null};
function _applyModelsPayload(d){
  nsfwModels.models = (d && Array.isArray(d.models)) ? d.models : [];
  nsfwModels.catalog = (d && Array.isArray(d.catalog)) ? d.catalog : [];
  nsfwModels.modelsDir = (d && d.modelsDir) || '';
  nsfwModels.loaded = true;
}
function _modelErrKey(e){
  const code = e && e.data && e.data.code;
  if (code==='active') return 'nsfwModelErrActive';
  if (code==='not_found') return 'nsfwModelErrNotFound';
  if (code==='exists') return 'nsfwModelErrExists';
  if (code==='insufficient_space') return 'nsfwModelErrSpace';
  if (code==='unknown_model') return 'nsfwModelErrUnknown';
  return 'nsfwModelErrGeneric';
}
async function loadNsfwModels(){
  if (apiState.mode!=='api'){   // mock: representative local files + catalog, no backend call
    if (!nsfwModels.loaded){
      nsfwModels.models = MOCK_NSFW_MODELS.models.map(m=>Object.assign({}, m));
      nsfwModels.catalog = MOCK_NSFW_MODELS.catalog.map(c=>Object.assign({}, c));
      nsfwModels.modelsDir = MOCK_NSFW_MODELS.modelsDir;
      nsfwModels.loaded = true;
    }
    return;
  }
  try {
    _applyModelsPayload(await apiGet(NSFW_MODELS_PATH));
    nsfwModels.error = null;
  } catch(e){
    nsfwModels.loaded = true; nsfwModels.error = 'nsfwModelsLoadErr';
  }
  loadNsfwModelDownload();
  render();
}
async function activateNsfwModel(file){
  if (nsfwModels.busyFile) return;
  if (apiState.mode!=='api'){   // mock: flip the active flag locally
    nsfwModels.models.forEach(m=>{ m.active = m.file===file; });
    nsfwModels.error = null;
    toast(t('nsfwModelActivatedToast')(file)); render(); return;
  }
  nsfwModels.busyFile = file; nsfwModels.error = null; render();
  try {
    _applyModelsPayload(await apiSend('POST', NSFW_MODELS_PATH+'/activate', {file}));
    nsfwModels.busyFile = null;
    toast(t('nsfwModelActivatedToast')(file)); render();
  } catch(e){
    nsfwModels.busyFile = null; nsfwModels.error = _modelErrKey(e); render();
  }
}
async function deleteNsfwModel(file){
  if (nsfwModels.busyFile) return;
  if (apiState.mode!=='api'){   // mock: drop it from the local list
    nsfwModels.models = nsfwModels.models.filter(m=>m.file!==file);
    nsfwModels.error = null;
    toast(t('nsfwModelDeletedToast')(file), 'warn'); render(); return;
  }
  nsfwModels.busyFile = file; nsfwModels.error = null; render();
  try {
    _applyModelsPayload(await apiSend('DELETE', NSFW_MODELS_PATH+'/'+encodeURIComponent(file)));
    nsfwModels.busyFile = null;
    toast(t('nsfwModelDeletedToast')(file), 'warn'); render();
  } catch(e){
    nsfwModels.busyFile = null; nsfwModels.error = _modelErrKey(e); render();
  }
}
function _applyModelDlPayload(d){
  nsfwModelDl.running = !!(d && d.running);
  nsfwModelDl.catalogId = (d && d.catalogId) || null;
  nsfwModelDl.filename = (d && d.filename) || null;
  nsfwModelDl.totalBytes = (d && d.totalBytes!=null) ? d.totalBytes : 0;
  nsfwModelDl.doneBytes = (d && d.doneBytes!=null) ? d.doneBytes : 0;
  nsfwModelDl.error = (d && d.error) || null;   // the server state is authoritative, a stale local error must clear
}
async function loadNsfwModelDownload(){
  if (apiState.mode!=='api'){ syncNsfwModelDlPoll(); return; }
  try {
    _applyModelDlPayload(await apiGet(NSFW_MODELS_PATH+'/download/progress'));
  } catch(e){ /* transient: keep the last known state */ }
  syncNsfwModelDlPoll();
}
async function downloadNsfwModel(catalogId){
  if (nsfwModelDl.starting || nsfwModelDl.running) return;
  const entry = nsfwModels.catalog.find(c=>c.id===catalogId);
  if (apiState.mode!=='api'){   // mock: walk a fake byte counter so the UI can be exercised standalone
    const total = (entry && entry.sizeBytes) || 1;
    nsfwModelDl.running = true; nsfwModelDl.catalogId = catalogId;
    nsfwModelDl.filename = (entry && entry.filename) || (catalogId + '.onnx');
    nsfwModelDl.totalBytes = total; nsfwModelDl.doneBytes = 0; nsfwModelDl.error = null;
    if (nsfwModelDl.mockTimer) clearInterval(nsfwModelDl.mockTimer);
    nsfwModelDl.mockTimer = setInterval(()=>{
      nsfwModelDl.doneBytes = Math.min(total, nsfwModelDl.doneBytes + Math.ceil(total/12));
      if (nsfwModelDl.doneBytes>=total){
        finishMockNsfwModelDl();
        if (entry){
          entry.downloaded = true;
          nsfwModels.models.push({file:nsfwModelDl.filename, sizeBytes:total, mtime:Math.floor(Date.now()/1000), active:false});
        }
        toast(t('nsfwModelDlDoneToast'));
      }
      render();
    }, 700);
    render(); return;
  }
  nsfwModelDl.starting = catalogId; nsfwModelDl.error = null; render();
  try {
    _applyModelDlPayload(await apiSend('POST', NSFW_MODELS_PATH+'/download', {catalogId}));
    nsfwModelDl.starting = null;
    syncNsfwModelDlPoll(); render();
  } catch(e){
    nsfwModelDl.starting = null; nsfwModelDl.error = t(_modelErrKey(e)); render();
  }
}
async function cancelNsfwModelDownload(){
  if (nsfwModelDl.cancelling) return;
  if (apiState.mode!=='api'){ finishMockNsfwModelDl(); toast(t('nsfwModelDlCancelledToast'), 'warn'); render(); return; }
  nsfwModelDl.cancelling = true; render();
  try {
    _applyModelDlPayload(await apiSend('POST', NSFW_MODELS_PATH+'/download/cancel'));
    nsfwModelDl.cancelling = false;
    syncNsfwModelDlPoll(); render();
  } catch(e){
    nsfwModelDl.cancelling = false; nsfwModelDl.error = t('nsfwModelErrGeneric'); render();
  }
}
function finishMockNsfwModelDl(){
  if (nsfwModelDl.mockTimer){ clearInterval(nsfwModelDl.mockTimer); nsfwModelDl.mockTimer = null; }
  nsfwModelDl.running = false;
}
function syncNsfwModelDlPoll(){
  const want = route.view==='settings' && nsfw.enabled && nsfwModelDl.running && apiState.mode==='api';
  if (want && !nsfwModelDl.timer){
    nsfwModelDl.timer = setInterval(pollNsfwModelDownload, 2000);
  } else if (!want && nsfwModelDl.timer){
    clearInterval(nsfwModelDl.timer); nsfwModelDl.timer = null;
  }
}
async function pollNsfwModelDownload(){
  if (route.view!=='settings'){ stopNsfwModelDownloadPoll(); return; }
  const wasRunning = nsfwModelDl.running;
  try {
    _applyModelDlPayload(await apiGet(NSFW_MODELS_PATH+'/download/progress'));
  } catch(e){ return; }   // transient failure: keep the last known state and retry on the next tick
  if (wasRunning && !nsfwModelDl.running){
    if (!nsfwModelDl.error) toast(t('nsfwModelDlDoneToast'));
    loadNsfwModels();   // the new file only shows up once the download settles
  }
  syncNsfwModelDlPoll();
  render();
}
function stopNsfwModelDownloadPoll(){
  if (nsfwModelDl.timer){ clearInterval(nsfwModelDl.timer); nsfwModelDl.timer = null; }
  if (nsfwModelDl.mockTimer){ clearInterval(nsfwModelDl.mockTimer); nsfwModelDl.mockTimer = null; }
}

/* ---- recording resolution (server-side, v3.3): read + change-to-save ---- */
async function loadRecResolution(){
  if (apiState.mode!=='api'){   // mock: keep the default (max/closest), no backend call
    if (!recRes.loaded){ recRes.loaded = true; recResSyncUi(); }
    return;
  }
  recRes.loading = true; recRes.error = null; render();
  try {
    const d = await apiGet('/settings/recording-resolution');
    recRes.resolution = (d && d.resolution!=null) ? d.resolution : 'max';
    recRes.preference = (d && d.preference) || 'closest';
    recResSyncUi();
    recRes.loaded = true; recRes.loading = false; recRes.error = null;
    render();
  } catch(e){
    recRes.loading = false; recRes.error = 'recResLoadErr'; render();
  }
}
// recRes.error holds an i18n KEY (not translated text) so it re-translates live
// when the language changes; the settings view resolves it with t() at render.
// A raw server-provided message falls through t() unchanged (not in the dict).
function recResErrKey(data){
  const map = {invalid_resolution:'recResErrRange', invalid_preference:'recResErrPref', missing:'recResErrMissing'};
  const key = data && map[data.code];
  if (key) return key;
  return (data && data.error) || 'recResErrGeneric';
}
function recResBuildPayload(){
  if (recRes.sel==='max') return {payload:{resolution:'max', preference:recRes.pref}};
  if (recRes.sel==='custom'){
    const raw = (recRes.custom||'').trim();
    const n = parseInt(raw, 10);
    if (!/^[0-9]+$/.test(raw) || isNaN(n) || n<144 || n>9999) return {error:'recResErrRange'};
    return {payload:{resolution:n, preference:recRes.pref}};
  }
  return {payload:{resolution:parseInt(recRes.sel, 10), preference:recRes.pref}};
}
async function saveRecResolution(){
  const built = recResBuildPayload();
  if (built.error){ recRes.error = built.error; render(); return; }   // invalid custom value: keep draft so the user can fix it
  const payload = built.payload;
  if (apiState.mode!=='api'){   // mock: accept locally, show success toast, no request
    recRes.resolution = payload.resolution; recRes.preference = payload.preference;
    recResSyncUi(); recRes.error = null;
    toast(t('recResSavedToast')); render(); return;
  }
  recRes.saving = true; recRes.error = null; render();
  try {
    const r = await fetch(apiUrl('/settings/recording-resolution'), {
      method:'PUT', credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const txt = await r.text();
    const data = txt ? JSON.parse(txt) : {};
    if (!r.ok){ checkUnauthorized(r); recRes.saving = false; recRes.error = recResErrKey(data); recResSyncUi(); render(); return; }   // roll displayed value back to committed
    recRes.resolution = (data && data.resolution!=null) ? data.resolution : payload.resolution;
    recRes.preference = (data && data.preference) || payload.preference;
    recResSyncUi();
    recRes.saving = false; recRes.error = null;
    toast(t('recResSavedToast')); render();
  } catch(e){
    recRes.saving = false; recRes.error = 'recResErrGeneric'; recResSyncUi(); render();
  }
}

/* ---- video deletion (single + batch), with recording-file protection ---- */
async function apiDeleteVideo(s, file){
  try { await apiSend('DELETE', streamerPath(s)+'/videos/'+encodeURIComponent(file._filename||file.id)); return true; }
  catch(e){ return false; }
}
async function apiDeleteFileFlow(s, file, idx){
  confirmDelId = null;
  if (file._recording){ toast(t('recDelBlocked'), 'warn'); render(); return; }   // backend rejects too (409)
  const wasCurrent = player.fileId===file.id;
  const ok = await apiDeleteVideo(s, file);
  if (!ok){ toast(t('apiOpError'), 'err'); render(); return; }
  toast(t('delToast')(fileName(s, file)), 'warn');
  if (wasCurrent){
    stopTick();
    const next = s.files[idx+1] || s.files[idx-1] || null;   // prefer the next-older recording
    if (next) setPlayerFile(next); else player = {fileId:null, playing:false, pos:0, timer:null};
  }
  await loadDetail(s.site, s.user);   // authoritative refresh from disk
}
async function apiBatchDeleteMarked(s){
  const marked = s.files.filter(f=>f.marked);
  if (!marked.length) return;
  const deletable = marked.filter(f=>!f._recording);
  const skipped = marked.length - deletable.length;
  if (deletable.some(f=>f.id===player.fileId)){ stopTick(); player = {fileId:null, playing:false, pos:0, timer:null}; }
  let okN = 0, failN = 0;
  for (const f of deletable){ if (await apiDeleteVideo(s, f)) okN++; else failN++; }
  if (okN) toast(t('batchDeletedToast')(okN), 'warn');
  if (failN) toast(t('apiOpError'), 'err');
  if (skipped) toast(t('recDelSkipped')(skipped), 'warn');
  await loadDetail(s.site, s.user);
}
/* ---- manual classification override (改判) ----
 * Writes a manual label for one video. The backend stores it with
 * source='manual' (never overwritten by the analyzer) and, when the file sits
 * in the recycle bin and the new label is not 'non-adult', restores it to the
 * original directory and answers restored=true. Marking a file non-adult only
 * changes the label — the configured NSFW action runs on the next scheduled
 * scan (the caller warns about that before getting here). */
async function apiSetClassification(s, file, label){
  clsMenuFileId = null;
  const labelText = label==='adult' ? t('clsAdult') : label==='non-adult' ? t('clsNonAdult') : t('clsUnknown');
  if (apiState.mode!=='api'){   // mock: flip the local label, un-grey a recycled entry
    file._cls = label;
    const restored = file._inRecycle && label!=='non-adult';
    if (restored) file._inRecycle = false;
    toast(restored ? t('clsRestoredToast')(labelText) : t('clsSetToast')(labelText));
    render(); return;
  }
  try {
    const res = await apiSend('POST', '/videos/classification',
      {site:s.site, user:s.user, filename:file._filename||file.id, label});
    const restored = !!(res && res.restored);
    file._cls = label;
    if (restored) file._inRecycle = false;
    toast(restored ? t('clsRestoredToast')(labelText) : t('clsSetToast')(labelText));
    render();
    await loadDetail(s.site, s.user);   // authoritative refresh (new path/mtime after a restore)
  } catch(e){
    toast(apiErrText(e), 'err'); render();
  }
}
/* purge: delete every recorded video for one streamer (recording-file protected server-side) */
async function apiPurgeStreamerVideos(s){
  const onDetail = route.view==='detail' && route.id===s.id;
  try {
    const res = await apiSend('DELETE', streamerPath(s)+'/videos');
    const deleted = res && res.deleted!=null ? res.deleted : 0;
    const skipped = res && res.skipped!=null ? res.skipped : 0;
    if (onDetail && player.fileId){ stopTick(); player = {fileId:null, playing:false, pos:0, timer:null}; }
    if (deleted) toast(t('purgeVideosToast')(deleted), 'warn'); else toast(t('purgeVideosNoneToast'), 'warn');
    if (skipped) toast(t('recDelSkipped')(skipped), 'warn');
    if (onDetail) await loadDetail(s.site, s.user); else await refreshStreamers();
  } catch(e){ toast(t('apiOpError'), 'err'); render(); }
}

async function initDataSource(){
  const cfg = window.SM_CONFIG || {};
  // SM_CONFIG.listFrequency is SECONDS (same contract as the backend field).
  if (cfg.listFrequency!=null && !isNaN(Number(cfg.listFrequency))){ const n = Number(cfg.listFrequency); if (n>0) apiState.pollMs = Math.max(2000, n*1000); }
  if (cfg.useApi === true){
    apiState.mode='api'; apiState.loading=true; streamers=[]; render();
    await startApiMode();
  } else if (cfg.useApi === 'auto'){
    if (await probeApi()){
      apiState.mode='api'; apiState.loading=true; render();
      await startApiMode();
    }   // else: stay on mock silently (no console noise)
  }   // default (no SM_CONFIG): mock only, no network calls
}
/* Both requests go out together and the first paint only waits on /streamers,
 * so a slow/failing basesettings can no longer delay the list. Its results
 * (disk widget, auth flag) are folded in with one render once it lands, and
 * polling starts after it has had its say on apiState.pollMs. */
async function startApiMode(){
  const base = loadBaseSettings();
  await refreshStreamers();
  await base;
  render();
  startPolling();
  startEventStream();   // push state changes; the poll above stays as the fallback
}

/* ---- loading / error banner (API mode only) ----
 * P1: styling lives in console.css (.api-banner) so the banner, the dashboard
 * skeleton and the in-list state blocks stay visually consistent. The banner is
 * for background refreshes; when the dashboard already renders the state inline
 * (first load / load failure with no data) it stays hidden instead of doubling up. */
function syncApiBanner(){
  let el = document.getElementById('apiBanner');
  const inline = typeof dashboardOwnsApiState==='function' && dashboardOwnsApiState();
  if (apiState.mode!=='api' || inline || (!apiState.loading && !apiState.error)){ if (el) el.remove(); return; }
  if (!el){
    el = document.createElement('div'); el.id = 'apiBanner';
    document.body.appendChild(el);
  }
  el.className = 'api-banner ' + (apiState.error ? 'err' : 'load');
  if (apiState.error){
    el.innerHTML = IC.alert+'<span>'+t('apiError')+'</span> <a href="#" id="apiRetry">'+t('apiRetry')+'</a>';
    const r = el.querySelector('#apiRetry');
    if (r) r.onclick = ev=>{ ev.preventDefault(); apiState.loading=true; apiState.error=false; syncApiBanner(); refreshStreamers(); };
  } else {
    el.innerHTML = '<span class="spin"></span><span>'+t('apiLoading')+'</span>';
  }
}

