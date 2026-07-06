'use strict';
/* ---------------- URL parsing ---------------- */
function parseStreamerUrl(raw){
  raw = raw.trim();
  if (!raw) return {err:'empty'};
  let u;
  try { u = new URL(raw.includes('://')?raw:'https://'+raw); }
  catch(e){ return {err:'nouser'}; }
  const host = u.hostname.replace(/^www\./,'');
  let siteKey = null;
  for (const [k,s] of Object.entries(SITES)){
    if (s.hosts.some(h=>host===h || host.endsWith('.'+h))) {siteKey=k; break;}
  }
  if (!siteKey) return {err:'unsupported', host};
  let user = u.pathname.split('/').filter(Boolean)[0] || '';
  if (siteKey==='myfreecams' && !user) user = (u.hash||'').replace(/^#\/?/,'');
  if (siteKey==='flirt4free' && u.searchParams.get('model')) user = u.searchParams.get('model');
  user = decodeURIComponent(user).replace(/[^\w.-]/g,'');
  const RESERVED = ['tag','tags','category','categories','p','login','signup','affiliates','terms'];
  if (!user || RESERVED.includes(user.toLowerCase())) return {err:'nouser'};
  return {site:siteKey, user};
}

/* ---------------- add streamer ---------------- */
function doAdd(siteKey, user){
  if (apiState.mode==='api'){ apiAddStreamer(siteKey, user); return; }
  const existing = streamers.find(s=>s.site===siteKey && s.user.toLowerCase()===user.toLowerCase());
  if (existing){
    if (existing.status==='stopped'){
      // re-adding an unmonitored entry means "monitor it again"
      existing.status='checking'; existing.lastCheckSec=0;
      setTimeout(()=>{
        if (streamers.includes(existing) && existing.status==='checking'){ existing.status='offline'; existing.lastCheckSec=5; render(); }
      }, 2200);
      toast(t('addDupResumed')(existing.user));
    } else {
      toast(t('addDup')(existing.user), 'warn');
    }
    addDraft = {url:'', user:''};
    nav({view:'detail', id:existing.id});
    return;
  }
  const orphanKey = siteKey+'/'+user.toLowerCase();
  const orphanN = ORPHANS[orphanKey]||0;
  const s = mkStreamer({site:siteKey, user, status:'checking', evening:[20,24], nfiles:orphanN, temps:[], addedDaysAgo:0, lastCheckSec:0, activity:1.0});
  if (!orphanN){ s.files=[]; }
  s.schedule = mkSchedule(s.seed,[20,24],false,orphanN?1.2:0.01);
  streamers.unshift(s);
  addMsg = null;
  addDraft = {url:'', user:''};
  toast(t('addOk')(user, SITES[siteKey].name));
  if (orphanN) toast(t('addOkLocal')(orphanN));
  // simulate first status check completing
  setTimeout(()=>{
    if (streamers.includes(s) && s.status==='checking'){ s.status='offline'; s.lastCheckSec=5; render(); }
  }, 2200);
  render();
}
function submitAddUrl(){
  const input = document.getElementById('addUrl');
  const res = parseStreamerUrl(input?input.value:'');
  if (res.err==='empty'){ addMsg={type:'err',text:t('addErrEmpty')}; render(); return; }
  if (res.err==='unsupported'){ addMsg={type:'err',text:t('addErrUnsupported')(res.host)}; render(); return; }
  if (res.err){ addMsg={type:'err',text:t('addErrNoUser')}; render(); return; }
  doAdd(res.site, res.user);
}
function submitAddManual(){
  const site = document.getElementById('addSite').value;
  const user = document.getElementById('addUser').value.trim().replace(/[^\w.-]/g,'');
  if (!user){ addMsg={type:'err',text:t('addErrEmptyManual')}; render(); return; }
  doAdd(site, user);
}


/* ---------------- disk widget / nav ---------------- */
function renderChrome(){
  const pct = DISK.used/DISK.total*100;
  document.getElementById('diskWidget').innerHTML = `
    <div class="disk-row"><span>${t('disk')}</span><b>${fmtSize(DISK.total-DISK.used)} ${t('diskFree')}</b></div>
    <div class="disk-bar"><i class="${pct>85?'hot':''}" style="width:${pct}%"></i></div>`;
  const nRec = streamers.filter(s=>s.status==='recording').length;
  const clockIC = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
  document.getElementById('nav').innerHTML = `
    <button class="nav-item ${(route.view==='dashboard'||route.view==='detail')?'active':''}" data-nav="dashboard">${IC.grid}<span>${t('navDashboard')}</span>${nRec?`<span class="nav-count" style="color:var(--rec)">●${nRec}</span>`:''}</button>
    <button class="nav-item ${route.view==='history'?'active':''}" data-nav="history">${clockIC}<span>${t('navHistory')}</span>${history.length?`<span class="nav-count">${history.length}</span>`:''}</button>
    <button class="nav-item ${route.view==='settings'?'active':''}" data-nav="settings">${IC.gear}<span>${t('navSettings')}</span></button>`;
  document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=t(el.dataset.i18n)});
  document.documentElement.lang = lang==='zh'?'zh-CN':'en';
}


/* ---------------- master render ---------------- */
function render(){
  // preserve scroll offset and focused-input caret across full re-renders;
  // deliberate resets (nav, thumbnail click) run after render() and still win
  const ae = document.activeElement;
  const focusId = ae && ae.tagName==='INPUT' && ae.id ? ae.id : null;
  const selPos = focusId && ae.selectionStart!=null ? ae.selectionStart : 0;
  const scroller = document.querySelector('.detail-scroll,.list-scroll,.settings-wrap');
  const scrollY = scroller ? scroller.scrollTop : 0;
  renderView();
  applyBatchbarClearance();   // reserve exact space so the floating batch bar never hides the last row
  syncApiBanner();            // show API loading/error state when running against a real backend
  const sc2 = document.querySelector('.detail-scroll,.list-scroll,.settings-wrap');
  if (sc2) sc2.scrollTop = scrollY;
  if (focusId){
    const el = document.getElementById(focusId);
    if (el){ el.focus(); try{ el.setSelectionRange(selPos, selPos); }catch(err){} }
  }
}
function renderView(){
  renderChrome();
  const main = document.getElementById('main');
  if (route.view==='detail'){
    const s = streamers.find(x=>x.id===route.id);
    if (!s){ route={view:'dashboard'}; }
    else { main.innerHTML = renderDetail(s); bindDetail(s); renderModal(); return; }
  }
  if (route.view==='settings'){ main.innerHTML = renderSettings(); renderModal(); return; }
  if (route.view==='history'){ main.innerHTML = renderHistory(); bindHistory(); renderModal(); return; }
  main.innerHTML = renderDashboard();
  bindDashboard();
  renderModal();
}

/* The batch bar floats over the bottom of the scroll area; its height varies with
 * viewport and language (it wraps on narrow screens), so reserve matching bottom
 * padding on the scroll container to keep the last row/temp file reachable. */
function applyBatchbarClearance(){
  const view = document.querySelector('.view');
  if (!view) return;
  const scroller = view.querySelector('.list-scroll, .detail-scroll');
  if (!scroller) return;
  const bar = view.querySelector('.batchbar');
  scroller.style.paddingBottom = bar ? (bar.offsetHeight + 32) + 'px' : '';
}

/* ---------------- playback helpers ---------------- */
function stopTick(){ if (player.timer){ clearInterval(player.timer); player.timer=null; } }
function setPlayerFile(f){
  player.fileId = f.id;
  player.pos = f.watchPct>0 && f.watchPct<100 ? Math.floor(f.dur*f.watchPct/100) : 0;
}
function startTick(s){
  stopTick();
  const cur = s.files.find(f=>f.id===player.fileId);
  if (!cur){ player.playing=false; return; }
  player.timer = setInterval(()=>{
    player.pos = Math.min(player.pos + (playerPrefs.speed||1), cur.dur);   // playback speed read live from prefs
    const seek=document.getElementById('pseek'), ptEl=document.getElementById('ptime');
    if (seek){ seek.value=player.pos; seek.style.setProperty('--pct',(player.pos/cur.dur*100)+'%'); }
    if (ptEl) ptEl.textContent=fmtDur(player.pos);
    cur.watchPct = Math.max(cur.watchPct, Math.round(player.pos/cur.dur*100));
    if (player.pos>=cur.dur){
      const idx = s.files.findIndex(f=>f.id===player.fileId);
      if (idx>=0 && idx < s.files.length-1) stepFile(s, idx, 1);   // auto-continue into the next (older) recording
      else { stopTick(); player.playing=false; render(); }
    }
  }, 1000);
}
function togglePlay(s){
  const v = document.getElementById('pvideo');
  if (v){ if (v.paused) v.play().catch(()=>{}); else v.pause(); return; }   // real media drives its own play/pause events
  const cur = s.files.find(f=>f.id===player.fileId);
  if (!cur) return;
  player.playing = !player.playing;
  if (player.playing){ cur.lastWatched=NOW; startTick(s); } else stopTick();
  render();
}
function stepFile(s, idx, dir){
  const nf = s.files[idx+dir];
  if (!nf) return;
  const v = document.getElementById('pvideo');
  if (v){   // rebuild <video> with the new file's URL; bindRealVideo resumes playback if it was playing
    const wasPlaying = !v.paused || player.playing;
    stopTick();
    setPlayerFile(nf);
    player.playing = wasPlaying;
    nf.lastWatched = NOW;
    render();
    return;
  }
  const wasPlaying = player.playing;
  stopTick();
  setPlayerFile(nf);
  player.playing = wasPlaying;
  if (wasPlaying) nf.lastWatched = NOW;
  render();
  if (wasPlaying) startTick(s);
}
function deleteFile(s, fid){
  const idx = s.files.findIndex(f=>f.id===fid);
  if (idx<0) return;
  const f = s.files[idx];
  if (apiState.mode==='api'){ apiDeleteFileFlow(s, f, idx); return; }   // real delete + authoritative refresh
  const wasCurrent = player.fileId===fid;
  const wasPlaying = player.playing;
  s.files.splice(idx,1);
  confirmDelId = null;
  toast(t('delToast')(fileName(s,f)), 'warn');
  if (wasCurrent){
    stopTick();
    const next = s.files[idx] || s.files[idx-1] || null;   // prefer the next-older recording
    if (next){ setPlayerFile(next); player.playing = wasPlaying; if (wasPlaying) next.lastWatched=NOW; }
    else player = {fileId:null, playing:false, pos:0, timer:null};
  }
  render();
  if (player.playing) startTick(s);
}
function batchDeleteMarked(s){
  if (apiState.mode==='api'){ apiBatchDeleteMarked(s); return; }   // real batch delete (skips recording file)
  const markedIds = new Set(s.files.filter(f=>f.marked).map(f=>f.id));
  if (!markedIds.size) return;
  const curIdx = s.files.findIndex(f=>f.id===player.fileId);
  const curMarked = markedIds.has(player.fileId);
  const wasPlaying = player.playing;
  let next = null;
  if (curMarked){
    for (let i=curIdx+1; i<s.files.length; i++) if (!markedIds.has(s.files[i].id)){ next=s.files[i]; break; }
    if (!next) for (let i=curIdx-1; i>=0; i--) if (!markedIds.has(s.files[i].id)){ next=s.files[i]; break; }
  }
  s.files = s.files.filter(f=>!markedIds.has(f.id));
  if (confirmDelId && markedIds.has(confirmDelId)) confirmDelId = null;
  if (curMarked){
    stopTick();
    if (next){ setPlayerFile(next); player.playing=wasPlaying; if (wasPlaying) next.lastWatched=NOW; }
    else player = {fileId:null, playing:false, pos:0, timer:null};
  }
  toast(t('batchDeletedToast')(markedIds.size), 'warn');
  render();
  if (player.playing) startTick(s);
}

/* ---------------- event wiring ---------------- */
document.addEventListener('click', e=>{
  const navBtn = e.target.closest('[data-nav]');
  if (navBtn){ nav({view:navBtn.dataset.nav}); return; }
  const langBtn = e.target.closest('[data-lang]');
  if (langBtn){ lang=langBtn.dataset.lang; localStorage.setItem('sm_lang',lang); render(); return; }
  const addModeBtn = e.target.closest('[data-addmode]');
  if (addModeBtn){ addMode=addModeBtn.dataset.addmode; addMsg=null; render();
    const f=document.getElementById(addMode==='url'?'addUrl':'addUser'); if(f)f.focus(); return; }

  const tog = e.target.closest('[data-toggle]');
  if (tog){
    e.stopPropagation();
    const s = streamers.find(x=>x.id===tog.dataset.toggle);
    if (s){
      if (apiState.mode==='api'){ apiToggleStreamer(s); return; }
      if (s.status==='paused'||s.status==='stopped'){ s.status='checking'; s.lastCheckSec=0; toast(t('resumedToast')(s.user));
        setTimeout(()=>{ if(streamers.includes(s) && s.status==='checking'){s.status='offline'; s.lastCheckSec=5; render();} }, 2000);
      } else { s.status='paused'; toast(t('pausedToast')(s.user)); }
      render();
    }
    return;
  }
  const ext = e.target.closest('[data-ext]');
  if (ext){
    e.stopPropagation();
    const s = streamers.find(x=>x.id===ext.dataset.ext);
    if (s) window.open(s.url || SITES[s.site].url(s.user), '_blank');
    return;
  }
  const rm = e.target.closest('[data-remove]');
  if (rm){
    const s = streamers.find(x=>x.id===rm.dataset.remove);
    modalState={type:'remove', id:rm.dataset.remove, delFiles: !!s && s.status==='stopped'};
    renderModal(); return;
  }

  // modal interactions
  if (e.target.closest('#modalCancel') || e.target.id==='modalBk'){ modalState=null; renderModal(); return; }
  const rmopt = e.target.closest('[data-rmopt]');
  if (rmopt){ modalState.delFiles = rmopt.dataset.rmopt==='del'; renderModal(); return; }
  if (e.target.closest('#modalConfirm')){
    if (modalState.type==='batchUnmon'){ modalState=null; renderModal(); batchUnmonitor(); return; }
    if (modalState.type==='batchPurge'){ modalState=null; renderModal(); batchPurge(); return; }
    if (modalState.type==='histDel'){
      const i=modalState.idx; modalState=null; renderModal();
      if (apiState.mode==='api'){ apiHistoryDelete(i); return; }
      removeHistoryAt(i); toast(t('histRemovedToast'), 'warn'); render();
      return;
    }
    const s = streamers.find(x=>x.id===modalState.id);
    if (modalState.type==='batch'){
      modalState=null; renderModal();
      if (s) batchDeleteMarked(s);
      return;
    }
    const purge = modalState.delFiles || (s && s.status==='stopped');
    modalState=null; renderModal();
    if (apiState.mode==='api'){ apiRemoveStreamer(s, purge); return; }
    if (purge){
      pushHistory(s);               // record the streamer before dropping it
      streamers = streamers.filter(x=>x.id!==s.id);
      toast(t('purgedToast')(s.user), 'warn');
      nav({view:'dashboard'});
    } else {
      s.status='stopped'; s.monitorState='unmonitored';   // keep the entry: local files remain associated with it
      toast(t('stopMonToast')(s.user));
      render();
    }
    return;
  }

  const open = e.target.closest('[data-open]');
  if (open && !e.target.closest('.row-actions') && !e.target.closest('.card-actions') && !e.target.closest('.rowcheck') && !e.target.closest('.sel-cell')){ nav({view:'detail', id:open.dataset.open}); return; }
});
document.addEventListener('keydown', e=>{
  if (e.key==='Escape'){
    if (modalState){ modalState=null; renderModal(); }
    else if (confirmDelId && route.view==='detail'){ confirmDelId=null; render(); }
    return;
  }
  if (route.view!=='detail' || modalState) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;   // leave browser/OS chords (Cmd+M, Alt+←…) alone
  const tag=(e.target.tagName||'').toLowerCase();
  if (tag==='input'||tag==='select'||tag==='textarea') return;
  const s = streamers.find(x=>x.id===route.id);
  if (!s) return;
  // space would re-activate a focused button (e.g. repeat a download) — reclaim it for the player
  if (tag==='button' && (e.key===' '||e.key==='Spacebar')) e.target.blur();
  handlePlayerHotkey(e, s);   // resolve the key against the configurable settings.hotkeys map
});

function bindDashboard(){
  document.querySelectorAll('[data-fs]').forEach(b=>b.addEventListener('click', ()=>{
    filters.status = b.dataset.fs; render();
  }));
  document.querySelectorAll('[data-site]').forEach(b=>b.addEventListener('click', ()=>{
    filters.site = b.dataset.site; render();
  }));
  // multiselect
  const selAll = document.getElementById('selAll');
  if (selAll){
    const list = sortList(applyFilters());
    const allSel = list.length>0 && list.every(s=>selected.has(s.id));
    selAll.indeterminate = !allSel && list.some(s=>selected.has(s.id));
    selAll.addEventListener('change', ()=>{
      if (selAll.checked) list.forEach(s=>selected.add(s.id));
      else list.forEach(s=>selected.delete(s.id));
      render();
    });
  }
  document.querySelectorAll('[data-sel]').forEach(cb=>cb.addEventListener('change', ()=>{
    if (cb.checked) selected.add(cb.dataset.sel); else selected.delete(cb.dataset.sel);
    render();
  }));
  const bPause = document.getElementById('batchPause');
  if (bPause) bPause.addEventListener('click', batchPause);
  const bResume = document.getElementById('batchResume');
  if (bResume) bResume.addEventListener('click', batchResume);
  const bUnmon = document.getElementById('batchUnmon');
  if (bUnmon) bUnmon.addEventListener('click', ()=>{ modalState={type:'batchUnmon'}; renderModal(); });
  const bPurge = document.getElementById('batchPurge');
  if (bPurge) bPurge.addEventListener('click', ()=>{ modalState={type:'batchPurge'}; renderModal(); });
  const bClear = document.getElementById('batchClear');
  if (bClear) bClear.addEventListener('click', ()=>{ selected.clear(); render(); });
  const q = document.getElementById('qFilter');
  if (q) q.addEventListener('input', ()=>{
    filters.q=q.value; render();   // render() preserves focus/caret generically
  });
  document.querySelectorAll('th.sortable').forEach(h=>h.addEventListener('click',()=>{
    const k=h.dataset.sort;
    if (sort.key===k) sort.dir*=-1; else {sort.key=k; sort.dir=1;}
    render();
  }));
  const cf = document.getElementById('clearFilters');
  if (cf) cf.addEventListener('click', ()=>{ filters={site:'all',status:'all',q:''}; render(); });

  const addUrl = document.getElementById('addUrl');
  if (addUrl){
    addUrl.addEventListener('input', ()=>{
      addDraft.url = addUrl.value;
      const parsed = document.getElementById('addParsed');
      const res = parseStreamerUrl(addUrl.value);
      if (!addUrl.value.trim()){ parsed.innerHTML=''; return; }
      if (res.site){
        parsed.innerHTML = `${IC.check} ${t('detected')} ${siteBadge(res.site)} <b style="font-family:var(--mono);font-size:11.5px">${esc(res.user)}</b>`;
        parsed.style.color='var(--online)';
      } else { parsed.innerHTML=''; }
    });
    addUrl.addEventListener('keydown', e=>{ if(e.key==='Enter') submitAddUrl(); });
    if (addUrl.value) addUrl.dispatchEvent(new Event('input'));   // recompute the site hint after re-render
  }
  const as = document.getElementById('addSubmit');
  if (as) as.addEventListener('click', submitAddUrl);
  const asm = document.getElementById('addSubmitManual');
  if (asm) asm.addEventListener('click', submitAddManual);
  const au = document.getElementById('addUser');
  if (au){
    au.addEventListener('input', ()=>{ addDraft.user = au.value; });
    au.addEventListener('keydown', e=>{ if(e.key==='Enter') submitAddManual(); });
  }

  const stopAll = document.getElementById('stopAll');
  if (stopAll) stopAll.addEventListener('click', ()=>{
    // 'stopped' entries are unmonitored by explicit user choice — leave them alone
    if (apiState.mode==='api'){
      apiBatchTargets('pause', streamerTargets(streamers.filter(s=>s.status!=='paused'&&s.status!=='stopped')), t('stopAllToast'));
      return;
    }
    streamers.forEach(s=>{ if(s.status!=='paused' && s.status!=='stopped') s.status='paused'; });
    toast(t('stopAllToast')); render();
  });
  const startAll = document.getElementById('startAll');
  if (startAll) startAll.addEventListener('click', ()=>{
    if (apiState.mode==='api'){
      apiBatchTargets('resume', streamerTargets(streamers.filter(s=>s.status==='paused')), t('startAllToast'));
      return;
    }
    streamers.forEach(s=>{ if(s.status==='paused'){ s.status='checking'; s.lastCheckSec=0;
      setTimeout(()=>{ if(streamers.includes(s) && s.status==='checking'){s.status='offline'; s.lastCheckSec=5; render();} }, 1800);
    }});
    toast(t('startAllToast')); render();
  });
}

function bindDetail(s){
  bindPlayer(s);   // control-bar interactions (seek jumps, skip, speed, volume, fullscreen)
  // list filter toggle (all / marked)
  document.querySelectorAll('[data-filefilter]').forEach(b=>b.addEventListener('click', ()=>{
    detailState.fileFilter = b.dataset.filefilter; render();
  }));
  const playFileById = id=>{
    const f = s.files.find(x=>x.id===id);
    if (!f) return;
    stopTick();
    player = {fileId:null, playing:false, pos:0, timer:null};
    setPlayerFile(f);
    confirmDelId=null;
    render();
    const box=document.querySelector('.detail-scroll'); if(box)box.scrollTop=0;
  };
  // play file — click thumbnail (outside action row) or the play button in the action row
  document.querySelectorAll('[data-playfile]').forEach(el=>el.addEventListener('click', e=>{
    if (e.target.closest('.acts')||e.target.closest('.confirm-del')) return;
    playFileById(el.dataset.playfile);
  }));
  document.querySelectorAll('[data-play]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    playFileById(b.dataset.play);
  }));
  // play/pause
  document.querySelectorAll('[data-pp]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    togglePlay(s);
  }));
  const seek = document.getElementById('pseek');
  if (seek) seek.addEventListener('input', ()=>{
    player.pos = +seek.value;
    seek.style.setProperty('--pct',(player.pos/(+seek.max)*100)+'%');
    const pt=document.getElementById('ptime'); if(pt)pt.textContent=fmtDur(player.pos);
    const v=document.getElementById('pvideo'); if(v){ try{ v.currentTime=player.pos; }catch(err){} }
  });
  // download / delete
  document.querySelectorAll('[data-dl]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    const f=s.files.find(x=>x.id===b.dataset.dl);
    if (!f) return;
    if (apiState.mode==='api' && f._url){   // stream the real file down as an attachment
      const a=document.createElement('a'); a.href=f._url; a.download=f._filename||''; document.body.appendChild(a); a.click(); a.remove();
      return;
    }
    toast(t('dlToast')(fileName(s,f)));
  }));
  document.querySelectorAll('[data-del]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation(); confirmDelId=b.dataset.del; render();
  }));
  document.querySelectorAll('[data-delyes]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    deleteFile(s, b.dataset.delyes);   // auto-advances playback to the next recording
  }));
  document.querySelectorAll('[data-delno]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation(); confirmDelId=null; render();
  }));
  // marking & batch actions
  document.querySelectorAll('[data-mark]').forEach(b=>b.addEventListener('click', e=>{
    e.stopPropagation();
    const f = s.files.find(x=>x.id===b.dataset.mark);
    if (f){ f.marked=!f.marked; render(); }
  }));
  const bd = document.getElementById('batchDel');
  if (bd) bd.addEventListener('click', ()=>{ modalState={type:'batch', id:s.id}; renderModal(); });
  const cm = document.getElementById('clearMarks');
  if (cm) cm.addEventListener('click', ()=>{ s.files.forEach(f=>f.marked=false); render(); });
  // temp files
  const doFix = tp=>{
    tp.state='fixing'; render();
    setTimeout(()=>{
      if (!streamers.includes(s) || !s.temps.some(x=>x.id===tp.id)) return;  // streamer removed / temp deleted meanwhile
      s.temps = s.temps.filter(x=>x.id!==tp.id);
      const dur = 600+Math.floor(rng(tp.id)()*3000);
      s.files.unshift({id:tp.id+'-fixed', dt:tp.dt, dur, size:tp.size, watchPct:0, marked:false, lastWatched:null});
      s.files.sort((a,b)=>b.dt-a.dt);
      toast(t('fixedToast')(tempName(s,tp)));
      render();
    }, 1400);
  };
  document.querySelectorAll('[data-fix]').forEach(b=>b.addEventListener('click', ()=>{
    const tp=s.temps.find(x=>x.id===b.dataset.fix);
    if (tp) doFix(tp);
  }));
  const fixAll = document.getElementById('fixAll');
  if (fixAll) fixAll.addEventListener('click', ()=>{
    s.temps.filter(x=>x.state==='fixable').forEach(doFix);
  });
  document.querySelectorAll('[data-tdel]').forEach(b=>b.addEventListener('click', ()=>{
    const tp=s.temps.find(x=>x.id===b.dataset.tdel);
    s.temps = s.temps.filter(x=>x.id!==b.dataset.tdel);
    toast(t('delToast')(tp?tempName(s,tp):''), 'warn');
    render();
  }));
  const cb = document.getElementById('cleanBroken');
  if (cb) cb.addEventListener('click', ()=>{
    const n = s.temps.filter(x=>x.state==='broken').length;
    s.temps = s.temps.filter(x=>x.state!=='broken');
    toast(t('cleanToast')(n), 'warn');
    render();
  });
}

render();
initDataSource();   // switch to real /api/v2 data when SM_CONFIG.useApi is set; otherwise stays on mock
