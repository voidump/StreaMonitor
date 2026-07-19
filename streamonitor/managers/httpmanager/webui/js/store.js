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
let detailState = {fileFilter:'all'};   // 'all'|'marked'

/* ---------------- history ops ---------------- */
function pushHistory(s){
  const added = new Date(NOW); if (s.addedDaysAgo!=null) added.setDate(added.getDate()-s.addedDaysAgo);
  history.unshift({
    user:s.user, site:s.site, url: s.url || SITES[s.site].url(s.user),
    avatarDataUrl: genAvatar(s.seed, s.user),
    addedDate: added, removedDate: new Date(NOW),
  });
}
function removeHistoryAt(i){ if (i>=0 && i<history.length) history.splice(i,1); }

/* Keep in sync with player.js PLAYER_HOTKEY_DEFAULTS — this table is the source
 * of truth the settings editor's "restore defaults" resets to. */
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
  if (player.timer){clearInterval(player.timer); player.timer=null;}
  player = {fileId:null, playing:false, pos:0, timer:null};
  confirmDelId = null;
  selected.clear();
  detailState.fileFilter = 'all';
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
  if (apiState.mode==='api' && to.view==='detail'){   // pull real videos + on-line pattern on entering a detail
    const s2 = streamers.find(x=>x.id===to.id);
    if (s2 && !s2._detailLoaded) loadDetail(s2.site, s2.user);
    if (s2 && !s2._schedLoaded) loadSchedule(s2.site, s2.user);
  }
  if (apiState.mode==='api' && to.view==='history') loadHistory();   // pull real deletion history
  if (to.view==='settings'){ loadDownloadDir(); loadRecResolution(); }   // read the current download dir + recording resolution (mock fills demo values)
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
 * Optional SM_CONFIG: apiBase (default '/api/v2'), listFrequency (s or ms),
 *   useApi:'auto' (probe first, keep mock if unreachable). No SM_CONFIG = mock. */
const apiState = { mode:'mock', loading:false, error:false, pollMs:30000, timer:null };

function apiUrl(path){
  const base = (window.SM_CONFIG && window.SM_CONFIG.apiBase) || '/api/v2';
  // Resolve against location.origin (never carries credentials) so that opening
  // the console via http://user:pass@host/ doesn't make fetch() throw
  // "Request cannot be constructed from a URL that includes credentials".
  return new URL(base + path, location.origin).toString();
}
async function apiGet(path){
  const r = await fetch(apiUrl(path), {headers:{'Accept':'application/json'}, credentials:'same-origin'});
  if (!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
async function probeApi(){
  try { const r = await fetch(apiUrl('/streamers'), {headers:{'Accept':'application/json'}, credentials:'same-origin'}); return r.ok; }
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
function ensureSite(item){
  const slug = item.siteslug || item.site || 'unknown';
  if (!SITES[slug]){   // register unknown sites so rendering never crashes
    SITES[slug] = { name:item.site||slug, abbr:String(slug).slice(0,3).toUpperCase(), color:'#6b7688', hosts:[], url:()=>'#', _dynamic:true };
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

/* ---- fetch + poll ---- */
async function refreshStreamers(){
  try {
    const data = await apiGet('/streamers');
    streamers = mapStreamers(Array.isArray(data) ? data : (data && data.streamers) || []);
    apiState.error = false; apiState.loading = false;
    if (pendingRoute && typeof resolvePendingRoute==='function'){ resolvePendingRoute(); return; }   // land a deep-linked view now that data is in
    render();
    if (route.view==='detail'){ const s = streamers.find(x=>x.id===route.id); if (s){ loadDetail(s.site, s.user); if (!s._schedLoaded) loadSchedule(s.site, s.user); } }
  } catch(e){
    apiState.loading = false; apiState.error = true;
    render();   // keep last data on screen; banner surfaces the failure
  }
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
      return { id, dt: parseTs(v.mtime), dur: v.duration!=null ? v.duration : (old?old.dur:0), size: v.size||0,
        watchPct: old?old.watchPct:0, marked: old?!!old.marked:false,
        lastWatched: old?old.lastWatched:null, _filename: v.filename, _url: v.url||'', _recording: !!v.recording };
    }).sort((a,b)=>b.dt-a.dt);
    s.temps = []; s._detailLoaded = true; s._filesSig = sig; s._detailError = false;
    if (route.view==='detail' && route.id===s.id && !player.fileId && s.files[0]) setPlayerFile(s.files[0]);
    apiState.error = false; render();
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
    if (f!=null && !isNaN(Number(f))){ const n = Number(f); apiState.pollMs = n>1000 ? n : n*1000; }
  } catch(e){ /* basesettings is optional */ }
}
function startPolling(){ stopPolling(); apiState.timer = setInterval(refreshStreamers, apiState.pollMs); }
function stopPolling(){ if (apiState.timer){ clearInterval(apiState.timer); apiState.timer=null; } }

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
  if (!r.ok) throw new Error('HTTP '+r.status);
  const txt = await r.text();
  return txt ? JSON.parse(txt) : null;
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
  const existing = streamers.find(s=>s.site===siteKey && s.user.toLowerCase()===user.toLowerCase());
  if (existing){
    if (existing.status==='stopped'){
      try { await apiSend('POST', streamerPath(existing)+'/reactivate'); toast(t('addDupResumed')(existing.user)); await refreshStreamers(); }
      catch(e){ toast(t('apiOpError'), 'err'); }
    } else {
      toast(t('addDup')(existing.user), 'warn');
    }
    addDraft = {url:'', user:''};
    nav({view:'detail', id:existing.id});
    return;
  }
  try {
    await apiSend('POST', '/streamers', {site:siteKey, username:user});
    addMsg = null; addDraft = {url:'', user:''};
    toast(t('addOk')(user, SITES[siteKey].name));
    await refreshStreamers();
  } catch(e){ toast(t('apiOpError'), 'err'); render(); }
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
    await apiSend('POST', '/history/'+idx+'/readd');
    toast(t('histReaddToast')(h.user));
    await loadHistory();
    await refreshStreamers();
    nav({view:'dashboard'});
  } catch(e){ toast(t('apiOpError'), 'err'); }
}
async function apiHistoryDelete(i){
  const h = history[i];
  const idx = h && h._idx!=null ? h._idx : i;
  try { await apiSend('DELETE', '/history/'+idx); toast(t('histRemovedToast'), 'warn'); await loadHistory(); }
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
    if (!r.ok){ downloadDir.saving = false; downloadDir.error = dlDirErrText(data); render(); return; }
    downloadDir.path = data.path || val; downloadDir.draft = downloadDir.path;
    downloadDir.saving = false; downloadDir.error = null;
    toast(t('dlDirSavedToast')); render();
  } catch(e){
    downloadDir.saving = false; downloadDir.error = t('dlDirErrGeneric'); render();
  }
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
    if (!r.ok){ recRes.saving = false; recRes.error = recResErrKey(data); recResSyncUi(); render(); return; }   // roll displayed value back to committed
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
  if (cfg.listFrequency!=null && !isNaN(Number(cfg.listFrequency))){ const n = Number(cfg.listFrequency); apiState.pollMs = n>1000 ? n : n*1000; }
  if (cfg.useApi === true){
    apiState.mode='api'; apiState.loading=true; streamers=[]; render();
    await loadBaseSettings(); await refreshStreamers(); startPolling();
  } else if (cfg.useApi === 'auto'){
    if (await probeApi()){
      apiState.mode='api'; apiState.loading=true; render();
      await loadBaseSettings(); await refreshStreamers(); startPolling();
    }   // else: stay on mock silently (no console noise)
  }   // default (no SM_CONFIG): mock only, no network calls
}

/* ---- loading / error banner (API mode only) ---- */
function syncApiBanner(){
  let el = document.getElementById('apiBanner');
  if (apiState.mode!=='api' || (!apiState.loading && !apiState.error)){ if (el) el.remove(); return; }
  if (!el){
    el = document.createElement('div'); el.id = 'apiBanner';
    el.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:400;display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:7px;font-size:12.5px;box-shadow:0 6px 20px rgba(0,0,0,.35)';
    document.body.appendChild(el);
  }
  if (apiState.error){
    el.style.cssText += ';background:var(--danger-bg);border:1px solid var(--danger-border-strong);color:var(--danger-text)';
    el.innerHTML = IC.alert+'<span>'+t('apiError')+'</span> <a href="#" id="apiRetry" style="color:#ffb3b6;text-decoration:underline;cursor:pointer">'+t('apiRetry')+'</a>';
    const r = el.querySelector('#apiRetry');
    if (r) r.onclick = ev=>{ ev.preventDefault(); apiState.loading=true; apiState.error=false; syncApiBanner(); refreshStreamers(); };
  } else {
    el.style.cssText += ';background:#12233b;border:1px solid #2f5075;color:#9ec5ef';
    el.innerHTML = '<span>'+t('apiLoading')+'</span>';
  }
}

