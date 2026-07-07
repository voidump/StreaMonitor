'use strict';
/* ================= player (v2) =================
 * Self-contained recording player: control bar render, seek/jump/speed/volume,
 * auto-continue playback, and a configurable hotkey engine reading settings.hotkeys.
 * Loaded last so it can define renderPlayer/bindPlayer/handlePlayerHotkey and
 * seed the DoD-faithful default hotkeys without touching store.js. */

/* ---- local icons (skip / muted) ---- */
const PIC = {
  prev:'<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zM20 6v12l-9-6z"/></svg>',
  next:'<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M16 6h2v12h-2zM4 6l9 6-9 6z"/></svg>',
  mute:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>',
};

/* ---- player-local i18n (append-free: keeps strings out of shared i18n.js) ---- */
const PLAYER_I18N = {
  zh:{ speed:'倍速', mute:'静音', unmute:'取消静音', prevVid:'上一个视频', nextVid:'下一个（更早）视频',
    back30:'后退 30 秒', back5:'后退 5 秒', fwd5:'前进 5 秒', fwd30:'前进 30 秒', volTitle:'音量',
    hints:'空格 播放/暂停 · ← → ±5秒 · Shift+← → ±30秒 · ↑ ↓ 音量 · [ ] 或 PgUp/PgDn 上/下一个 · M 标记' },
  en:{ speed:'Speed', mute:'Mute', unmute:'Unmute', prevVid:'Previous video', nextVid:'Next (older) video',
    back30:'Back 30s', back5:'Back 5s', fwd5:'Forward 5s', fwd30:'Forward 30s', volTitle:'Volume',
    hints:'Space play/pause · ← → ±5s · Shift+← → ±30s · ↑ ↓ volume · [ ] or PgUp/PgDn prev/next · M mark' },
};
const pt = k => (PLAYER_I18N[lang] || PLAYER_I18N.en)[k] || k;

/* ---- persistent playback prefs (speed / volume / muted) ---- */
function loadPlayerPrefs(){
  const d = {speed:1, volume:1, muted:false};
  try { const raw = localStorage.getItem('sm_player'); return raw ? Object.assign(d, JSON.parse(raw)) : d; }
  catch(e){ return d; }
}
function savePlayerPrefs(){ try { localStorage.setItem('sm_player', JSON.stringify(playerPrefs)); } catch(e){} }
let playerPrefs = loadPlayerPrefs();
const PLAYER_SPEEDS = [0.5, 1, 1.25, 1.5, 2];

/* ---- DoD-faithful default hotkeys, layered under any saved overrides ----
 * base table lives in store.js (DEFAULT_HOTKEYS); the player owns the engine,
 * so it re-derives settings.hotkeys with the richer default set (adds volume/mute
 * and Shift+Arrow / bracket bindings) while preserving user-saved keys. */
const PLAYER_HOTKEY_DEFAULTS = {
  playPause:'Space', seekBack:'ArrowLeft', seekFwd:'ArrowRight',
  jumpBack:'Shift+ArrowLeft', jumpFwd:'Shift+ArrowRight',
  volumeUp:'ArrowUp', volumeDown:'ArrowDown',
  prevVideo:'PageUp', nextVideo:'PageDown', mark:'m', mute:'x',
};
(function initHotkeys(){
  let saved = {};
  try { const raw = localStorage.getItem('sm_hotkeys'); if (raw) saved = JSON.parse(raw) || {}; } catch(e){}
  settings.hotkeys = Object.assign({}, PLAYER_HOTKEY_DEFAULTS, saved);
})();

/* ---- injected styles (keeps console.css untouched to avoid parallel conflicts) ---- */
(function injectPlayerStyles(){
  const css = `
.player{position:relative}
.pspeed-badge{position:absolute;top:8px;right:10px;background:rgba(13,17,23,.7);border:1px solid rgba(255,255,255,.15);color:#fff;font:600 11px/1 var(--mono);padding:3px 6px;border-radius:4px}
.pbar{background:var(--bg-raised);border-top:1px solid var(--border)}
.pseek-wrap{position:relative;padding:9px 12px 3px}
#pseek{display:block;width:100%;-webkit-appearance:none;appearance:none;height:5px;border-radius:3px;background:linear-gradient(to right,var(--accent) var(--pct,0%),#2a3546 var(--pct,0%));outline:none;cursor:pointer}
#pseek::-webkit-slider-thumb{-webkit-appearance:none;width:13px;height:13px;border-radius:50%;background:#dfe9f5;box-shadow:0 0 0 3px rgba(76,159,232,.25)}
.pseek-tip{position:absolute;top:-20px;transform:translateX(-50%);background:#0b0f15;border:1px solid var(--border-strong);color:var(--text);font:11px/1 var(--mono);padding:3px 6px;border-radius:4px;pointer-events:none;opacity:0;transition:opacity .12s;white-space:nowrap;z-index:5}
.pctrl{flex-wrap:wrap}
.pctrl .icon-btn:disabled{opacity:.35;cursor:not-allowed}
.pjump{display:inline-flex;gap:3px}
.pjump .jbtn{font-family:var(--mono);font-size:11px;color:var(--text-dim);border:1px solid var(--border-strong);border-radius:5px;padding:2px 7px;background:var(--bg-panel);transition:background .12s,color .12s}
.pjump .jbtn:hover{background:var(--bg-hover);color:var(--text)}
.pctrl .tsep{color:var(--text-faint);font-size:11px}
.pspacer{flex:1 1 8px}
.pspeed{background:var(--bg-panel);border:1px solid var(--border-strong);border-radius:5px;color:var(--text);font-size:11.5px;padding:3px 5px;cursor:pointer}
.pvol{display:inline-flex;align-items:center;gap:4px}
.pctrl .volrange{flex:none;width:72px;-webkit-appearance:none;appearance:none;height:4px;border-radius:2px;background:linear-gradient(to right,var(--accent) var(--vol,100%),#2a3546 var(--vol,100%));outline:none;cursor:pointer}
.pctrl .volrange::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;border-radius:50%;background:#dfe9f5}
.player video.frame{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.pctrl .icon-btn:disabled{opacity:.35;cursor:not-allowed}
.now-file .rec-note{color:var(--rec,#e0455a);font-weight:600}
.vthumb.recording .imgwrap{outline:2px solid var(--rec,#e0455a);outline-offset:-2px}
.vthumb .rec-badge{position:absolute;left:6px;top:6px;background:var(--rec,#e0455a);color:#fff;border-radius:3px;padding:1px 6px;font-size:10px;font-weight:600;display:inline-flex;align-items:center;gap:4px;z-index:2}
.vthumb .rec-badge::before{content:'';width:6px;height:6px;border-radius:50%;background:#fff;animation:recpulse 1.2s infinite}
@keyframes recpulse{50%{opacity:.3}}
@media (max-width:560px){
  .pjump{order:6}
  .pvol .volrange{width:56px}
  .now-file .kbd-hints{display:none}
}`;
  const el = document.createElement('style');
  el.id = 'player-styles';
  el.textContent = css;
  document.head.appendChild(el);
})();

/* ---- lightweight DOM sync (avoids full re-render during scrub / volume / autoplay tick) ---- */
function syncSeekUI(cur){
  const seek=document.getElementById('pseek'), tEl=document.getElementById('ptime');
  if (seek){ seek.value=player.pos; seek.style.setProperty('--pct',(player.pos/cur.dur*100)+'%'); }
  if (tEl) tEl.textContent=fmtDur(player.pos);
}
function syncVolUI(){
  const eff = playerPrefs.muted ? 0 : playerPrefs.volume;
  const vr=document.getElementById('pvolr');
  if (vr){ vr.value=eff; vr.style.setProperty('--vol',(eff*100)+'%'); }
  const mb=document.getElementById('pmute');
  if (mb){
    mb.innerHTML = (playerPrefs.muted||eff===0)?PIC.mute:IC.volume;
    mb.classList.toggle('muted', playerPrefs.muted);
    mb.title = playerPrefs.muted?pt('unmute'):pt('mute');
  }
}

/* ---- real <video> access (only present in API mode; null keeps mock path) ---- */
function pvideo(){ return document.getElementById('pvideo'); }

/* ---- player actions (shared by control bar + hotkey engine) ---- */
function seekBy(s, delta){
  const v = pvideo();
  if (v){ v.currentTime = Math.max(0, Math.min(v.duration || 0, (v.currentTime || 0) + delta)); return; }
  const cur = s.files.find(f=>f.id===player.fileId);
  if (!cur) return;
  player.pos = Math.max(0, Math.min(cur.dur, player.pos + delta));
  cur.watchPct = Math.max(cur.watchPct, Math.round(player.pos/cur.dur*100));
  syncSeekUI(cur);
}
function setVolume(v){
  playerPrefs.volume = Math.max(0, Math.min(1, v));
  if (playerPrefs.volume>0) playerPrefs.muted = false;
  savePlayerPrefs();
  const vid = pvideo();
  if (vid){ vid.muted = !!playerPrefs.muted; vid.volume = playerPrefs.muted ? 0 : playerPrefs.volume; }
  syncVolUI();
}
function adjustVolume(delta){ setVolume((playerPrefs.muted?0:playerPrefs.volume) + delta); }
function toggleMute(){
  playerPrefs.muted = !playerPrefs.muted; savePlayerPrefs();
  const vid = pvideo();
  if (vid){ vid.muted = !!playerPrefs.muted; vid.volume = playerPrefs.muted ? 0 : playerPrefs.volume; }
  syncVolUI();
}
function setSpeed(v){
  if (!PLAYER_SPEEDS.includes(v)) return;
  playerPrefs.speed = v; savePlayerPrefs();
  const vid = pvideo();
  if (vid){   // avoid a full render (would rebuild <video> and interrupt playback); patch in place
    vid.playbackRate = v;
    const box = document.getElementById('playerBox');
    if (box){
      let badge = box.querySelector('.pspeed-badge');
      if (v!==1){ if (!badge){ badge=document.createElement('div'); badge.className='pspeed-badge'; box.appendChild(badge); } badge.textContent = v+'×'; }
      else if (badge){ badge.remove(); }
    }
    const sel = document.getElementById('pspeed'); if (sel) sel.value = v;
    return;
  }
  render();   // refreshes the speed badge + <select>; the running tick reads playerPrefs.speed live
}
function togglePlayerFullscreen(){
  const box = document.getElementById('playerBox');
  if (!box) return;
  if (document.fullscreenElement) document.exitFullscreen && document.exitFullscreen();
  else box.requestFullscreen && box.requestFullscreen();
}

/* ---- render: video frame + mature control bar + now-playing strip ---- */
function renderPlayer(s){
  const cur = s.files.find(f=>f.id===player.fileId) || null;
  if (!cur) return `<div class="player-empty">${s.files.length?t('noFileSelected'):t('noRecordings')}</div>`;
  const idx = s.files.findIndex(f=>f.id===cur.id);
  const hasPrev = idx>0, hasNext = idx>=0 && idx<s.files.length-1;   // newest-first: next = older
  const pct = cur.dur ? (player.pos/cur.dur*100).toFixed(1) : 0;
  const eff = playerPrefs.muted ? 0 : playerPrefs.volume;
  const speed = playerPrefs.speed;
  const real = apiState.mode==='api' && cur._url;   // real <video> element vs mock frame simulation
  const media = real
    ? `<video id="pvideo" class="frame" src="${esc(cur._url)}" preload="metadata" playsinline${playerPrefs.muted?' muted':''}></video>`
    : `<img class="frame" src="${genThumb(cur.id, 704, 396)}" alt=""><div class="veil"></div><div class="bigplay"><button class="pbtn" data-pp="1">${IC.playLg}</button></div>`;
  return `
    <div class="player ${player.playing?'playing':''}" id="playerBox">
      ${media}
      ${speed!==1?`<div class="pspeed-badge">${speed}×</div>`:''}
    </div>
    <div class="pbar">
      <div class="pseek-wrap">
        <input type="range" id="pseek" min="0" max="${cur.dur}" step="1" value="${player.pos}" style="--pct:${pct}%">
        <div class="pseek-tip" id="pseekTip"></div>
      </div>
      <div class="pctrl">
        <button class="icon-btn" data-pp="1" title="${t('player')}">${player.playing?IC.pause:IC.play}</button>
        <button class="icon-btn" data-pstep="-1" title="${pt('prevVid')}" ${hasPrev?'':'disabled'}>${PIC.prev}</button>
        <button class="icon-btn" data-pstep="1" title="${pt('nextVid')}" ${hasNext?'':'disabled'}>${PIC.next}</button>
        <span class="pjump">
          <button class="jbtn" data-pseekjmp="-30" title="${pt('back30')}">-30</button>
          <button class="jbtn" data-pseekjmp="-5" title="${pt('back5')}">-5</button>
          <button class="jbtn" data-pseekjmp="5" title="${pt('fwd5')}">+5</button>
          <button class="jbtn" data-pseekjmp="30" title="${pt('fwd30')}">+30</button>
        </span>
        <span class="time" id="ptime">${fmtDur(player.pos)}</span>
        <span class="tsep">/</span>
        <span class="time" id="ptotal">${fmtDur(cur.dur)}</span>
        <span class="pspacer"></span>
        <div class="pvol" title="${pt('volTitle')}">
          <button class="icon-btn ${playerPrefs.muted?'muted':''}" id="pmute" title="${playerPrefs.muted?pt('unmute'):pt('mute')}">${(playerPrefs.muted||eff===0)?PIC.mute:IC.volume}</button>
          <input type="range" class="volrange" id="pvolr" min="0" max="1" step="0.05" value="${eff}" style="--vol:${eff*100}%">
        </div>
        <select class="pspeed" id="pspeed" title="${pt('speed')}">
          ${PLAYER_SPEEDS.map(v=>`<option value="${v}" ${v===speed?'selected':''}>${v}×</option>`).join('')}
        </select>
        <button class="icon-btn ${cur.marked?'markon':''}" data-mark="${cur.id}" title="${cur.marked?t('unmark'):t('mark')}">${cur.marked?IC.markFill:IC.mark}</button>
        <button class="icon-btn" data-dl="${cur.id}" title="${t('download')}">${IC.down}</button>
        <button class="icon-btn" data-pfull="1" title="${t('fullscreen')}">${IC.full}</button>
      </div>
    </div>
    <div class="now-file">
      <span style="color:var(--text-faint)">${t('nowPlaying')}</span>
      <span class="fname">${fileName(s,cur)}</span>
      ${cur._recording?`<span>·</span><span class="rec-note">${t('recInProgress')}</span>`:''}
      <span>·</span><span style="font-family:var(--mono);font-size:11.5px">${dateWd(cur.dt)} ${timeShort(cur.dt)}</span>
      <span>·</span><span>${fmtSize(cur.size)}</span>
      ${cur.watchPct>0&&cur.watchPct<100?`<span>·</span><span>${t('watchProg')} ${cur.watchPct}%</span>`:''}
      <span class="kbd-hints">${pt('hints')}</span>
    </div>`;
}

/* ---- bind: control-bar interactions unique to the player (data-pp/-mark/-dl stay in bindDetail) ---- */
function bindPlayer(s){
  document.querySelectorAll('[data-pstep]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    const idx = s.files.findIndex(f=>f.id===player.fileId);
    stepFile(s, idx, +b.dataset.pstep);
  }));
  document.querySelectorAll('[data-pseekjmp]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    seekBy(s, +b.dataset.pseekjmp);
  }));
  const vol = document.getElementById('pvolr');
  if (vol) vol.addEventListener('input', ()=> setVolume(+vol.value));
  const mute = document.getElementById('pmute');
  if (mute) mute.addEventListener('click', e=>{ e.stopPropagation(); toggleMute(); });
  const sp = document.getElementById('pspeed');
  if (sp) sp.addEventListener('change', ()=> setSpeed(+sp.value));
  const full = document.querySelector('[data-pfull]');
  if (full) full.addEventListener('click', e=>{ e.stopPropagation(); togglePlayerFullscreen(); });
  // hover-scrub tooltip over the seek bar
  const seek = document.getElementById('pseek'), tip = document.getElementById('pseekTip');
  if (seek && tip){
    const cur = s.files.find(f=>f.id===player.fileId);
    seek.addEventListener('mousemove', e=>{
      const r = seek.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (e.clientX-r.left)/r.width));
      tip.textContent = fmtDur(Math.round(frac*(cur?cur.dur:0)));
      tip.style.left = (frac*r.width) + 'px';
      tip.style.opacity = '1';
    });
    seek.addEventListener('mouseleave', ()=>{ tip.style.opacity='0'; });
  }
  bindRealVideo(s);   // API mode only: hook the real <video> element to the control bar
}

/* ---- real-video wiring: drives the shared control bar off actual media events ----
 * Only active in API mode (a #pvideo element exists). Playback state lives on the
 * media element; we patch the DOM in place so play/pause/seek never trigger a full
 * render() that would rebuild <video> and interrupt playback. */
function updatePlayIcon(){
  const b = document.querySelector('.pctrl [data-pp]');
  if (b) b.innerHTML = player.playing ? IC.pause : IC.play;
  const box = document.getElementById('playerBox');
  if (box) box.classList.toggle('playing', player.playing);
}
function bindRealVideo(s){
  const v = pvideo();
  if (!v || v._smBound) return;   // node is preserved across re-renders (see grabPlayingVideo) — bind exactly once
  v._smBound = true;
  // resolve the current streamer/file live: list polls swap streamer objects, so a
  // closure over `s`/`cur` would go stale on the long-lived <video>'s listeners.
  const curStreamer = ()=> streamers.find(x=>x.id===route.id) || s;
  const curFile = ()=>{ const st = curStreamer(); return st && st.files.find(f=>f.id===player.fileId); };
  v.playbackRate = playerPrefs.speed || 1;
  v.muted = !!playerPrefs.muted;
  v.volume = playerPrefs.muted ? 0 : playerPrefs.volume;
  v.addEventListener('loadedmetadata', ()=>{
    const cur = curFile();
    if (cur && isFinite(v.duration) && v.duration>0){
      cur.dur = Math.floor(v.duration);
      const seek = document.getElementById('pseek'); if (seek) seek.max = cur.dur;
      const tot = document.getElementById('ptotal'); if (tot) tot.textContent = fmtDur(cur.dur);
    }
    if (player.pos>0 && Math.abs((v.currentTime||0)-player.pos)>0.4){ try { v.currentTime = player.pos; } catch(e){} }
    if (player.playing) v.play().catch(()=>{});   // resume across re-renders (mark toggle, poll, etc.)
  });
  v.addEventListener('timeupdate', ()=>{
    const cur = curFile();
    player.pos = v.currentTime;
    if (cur){ if (cur.dur) cur.watchPct = Math.max(cur.watchPct||0, Math.round(player.pos/cur.dur*100)); syncSeekUI(cur); }
  });
  v.addEventListener('play', ()=>{ player.playing = true; updatePlayIcon(); });
  v.addEventListener('pause', ()=>{ player.playing = false; updatePlayIcon(); });
  v.addEventListener('ended', ()=>{
    const st = curStreamer();
    const idx = st ? st.files.findIndex(f=>f.id===player.fileId) : -1;
    if (st && idx>=0 && idx < st.files.length-1) stepFile(st, idx, 1);   // auto-continue into the next (older) recording
    else { player.playing = false; updatePlayIcon(); }
  });
  v.addEventListener('click', e=>{ e.stopPropagation(); togglePlay(curStreamer()); });
}

/* ---- hotkey engine: normalise the event, then resolve it against settings.hotkeys ---- */
function hotkeyToken(e){
  let key = e.key;
  if (key===' ' || key==='Spacebar') key = 'Space';
  else if (key.length===1) key = key.toLowerCase();
  return (e.shiftKey && key!=='Shift') ? 'Shift+'+key : key;
}
function handlePlayerHotkey(e, s){
  const hk = settings.hotkeys || {};
  const token = hotkeyToken(e);
  if (!token) return false;
  const ALT = { prevVideo:['['], nextVideo:[']'] };   // bracket alternates, always available
  const hit = act => hk[act]===token || (ALT[act] && ALT[act].includes(token));
  const idx = s.files.findIndex(f=>f.id===player.fileId);
  if (hit('playPause')){ e.preventDefault(); togglePlay(s); return true; }
  if (hit('jumpBack')){ e.preventDefault(); seekBy(s,-30); return true; }
  if (hit('jumpFwd')){ e.preventDefault(); seekBy(s,30); return true; }
  if (hit('seekBack')){ e.preventDefault(); seekBy(s,-5); return true; }
  if (hit('seekFwd')){ e.preventDefault(); seekBy(s,5); return true; }
  if (hit('volumeUp')){ e.preventDefault(); adjustVolume(0.1); return true; }
  if (hit('volumeDown')){ e.preventDefault(); adjustVolume(-0.1); return true; }
  if (hit('prevVideo')){ e.preventDefault(); stepFile(s, idx, -1); return true; }
  if (hit('nextVideo')){ e.preventDefault(); stepFile(s, idx, 1); return true; }
  if (hit('mute')){ e.preventDefault(); toggleMute(); return true; }
  if (hit('mark')){ const f=s.files[idx]; if (f){ f.marked=!f.marked; render(); } return true; }
  return false;
}
