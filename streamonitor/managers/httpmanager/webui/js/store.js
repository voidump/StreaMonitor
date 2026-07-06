'use strict';
/* ---------------- state ---------------- */
let route = {view:'dashboard'};   // {view:'dashboard'|'detail'|'settings', id}
let filters = {site:'all', status:'all', q:''};
let sort = {key:'status', dir:1};
let addMode = 'url';
let addMsg = null;   // {type:'err'|'ok', text}
let addDraft = {url:'', user:''};  // survives timer-driven re-renders
let player = {fileId:null, playing:false, pos:0, timer:null};
let modalState = null;
let confirmDelId = null;
let selected = new Set();   // dashboard multiselect — streamer ids

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
  render();
  if (apiState.mode==='api' && to.view==='detail'){   // pull real videos on entering a detail
    const s2 = streamers.find(x=>x.id===to.id);
    if (s2 && !s2._detailLoaded) loadDetail(s2.site, s2.user);
  }
  if (apiState.mode==='api' && to.view==='history') loadHistory();   // pull real deletion history
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
  return base + path;
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
      lastCheckSec: tsToSecAgo(item.lastCheckTs),
      addedDaysAgo: null,
      _videoCount: item.videoCount!=null ? item.videoCount : null,
      _totalSize: item.totalSize!=null ? item.totalSize : null,
      files: [], temps: [], schedule: EMPTY_SCHEDULE, _detailLoaded:false,
    };
    if (prev && prev._detailLoaded){ s.files = prev.files; s.temps = prev.temps; s._detailLoaded = true; }
    return s;
  });
}

/* ---- fetch + poll ---- */
async function refreshStreamers(){
  try {
    const data = await apiGet('/streamers');
    streamers = mapStreamers(Array.isArray(data) ? data : (data && data.streamers) || []);
    apiState.error = false; apiState.loading = false;
    render();
    if (route.view==='detail'){ const s = streamers.find(x=>x.id===route.id); if (s) loadDetail(s.site, s.user); }
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
    const oldById = {}; s.files.forEach(f=>{ oldById[f.id]=f; });   // preserve marks/progress
    const vids = (data && data.videos) || [];
    s.files = vids.map(v=>{
      const id = v.filename; const old = oldById[id];
      return { id, dt: parseTs(v.mtime), dur: old?old.dur:0, size: v.size||0,
        watchPct: old?old.watchPct:0, marked: old?!!old.marked:false,
        lastWatched: old?old.lastWatched:null, _filename: v.filename, _url: v.url||'' };
    }).sort((a,b)=>b.dt-a.dt);
    s.temps = []; s._detailLoaded = true;
    if (data){ if (data.totalSize!=null) s._totalSize = data.totalSize; if (data.videoCount!=null) s._videoCount = data.videoCount; }
    if (route.view==='detail' && route.id===s.id && !player.fileId && s.files[0]) setPlayerFile(s.files[0]);
    apiState.error = false; render();
  } catch(e){
    apiState.error = true; render();
  }
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
        addedDate: parseTs(h.addedTs!=null ? h.addedTs : h.addedDate),
        removedDate: parseTs(h.removedTs!=null ? h.removedTs : (h.deletedTs!=null ? h.deletedTs : h.removedDate)),
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
    el.style.cssText += ';background:#2a1517;border:1px solid #7c383d;color:#ff8589';
    el.innerHTML = IC.alert+'<span>'+t('apiError')+'</span> <a href="#" id="apiRetry" style="color:#ffb3b6;text-decoration:underline;cursor:pointer">'+t('apiRetry')+'</a>';
    const r = el.querySelector('#apiRetry');
    if (r) r.onclick = ev=>{ ev.preventDefault(); apiState.loading=true; apiState.error=false; syncApiBanner(); refreshStreamers(); };
  } else {
    el.style.cssText += ';background:#12233b;border:1px solid #2f5075;color:#9ec5ef';
    el.innerHTML = '<span>'+t('apiLoading')+'</span>';
  }
}

