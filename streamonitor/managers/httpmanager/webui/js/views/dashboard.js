'use strict';
/* ---------------- shared renderers ---------------- */
/* NB11: in API mode show the site's real favicon (fetched + disk-cached by the
 * backend); onerror falls back to the colored dot. Mock mode keeps the colored
 * dot and makes zero network calls. */
function siteBadgeIconError(img){
  img.onerror = null;
  const span = document.createElement('span');
  span.innerHTML = img.dataset.dot;
  const dot = span.firstElementChild;
  if (dot) img.replaceWith(dot);
}
function siteBadge(siteKey){
  const s = SITES[siteKey];
  const dot = `<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="${s.color}"/></svg>`;
  let icon = dot;
  if (typeof apiState!=='undefined' && apiState.mode==='api'){
    const src = apiUrl('/sites/'+encodeURIComponent(siteKey)+'/favicon');
    icon = `<img src="${src}" width="12" height="12" style="width:12px;height:12px;border-radius:2px;object-fit:contain" alt="" data-dot="${dot.replace(/"/g,'&quot;')}" onerror="siteBadgeIconError(this)">`;
  }
  return `<span class="sitebadge" style="color:${s.color};background:${s.color}1a;border-color:${s.color}33" title="${s.name}">
    ${icon}${s.abbr}</span>`;
}
/* Recording extras appended after the size: " · 速度 · 清晰度". Each piece is
 * omitted individually when unavailable so there is never a NaN/undefined gap. */
function recMeta(s){
  const parts = [];
  const speed = fmtSpeed(s.recSpeedBps);
  if (speed) parts.push(speed);
  if (s.recResolution) parts.push(s.recResolution+'p');
  return parts.map(x=>' · '+x).join('');
}
function statusChip(s){
  const st = STATUS[s.status] || STATUS.offline;   // guard unexpected status values
  const dotStyle = st.ring
    ? `background:transparent;border:1.5px solid ${st.ring}`
    : `background:${st.color}`;
  const bytes = s.status==='recording'
    ? `<span class="rec-bytes" title="${t('recInfoTitle')}">${fmtSize(s.recBytes||0)}${s.recSince?` · ${fmtDur(Math.floor((NOW-s.recSince)/1000))}`:''}${recMeta(s)}</span>` : '';
  return `<span class="status-chip"><span class="dot ${st.pulse?'pulse':''}" style="${dotStyle}"></span>${st.label()}</span>${bytes}`;
}
function sparkline(s){
  const {dayTotals} = schedStats(s.schedule);
  const max = Math.max(...dayTotals, 1);
  const wd = t('weekdays');
  return `<span class="spark" title="${wd.map((w,i)=>w+':'+dayTotals[i]).join(' ')}">${
    dayTotals.map(v=>{
      const hpx = Math.max(2, Math.round(v/max*18));
      const cls = v/max>0.66?'hi':v/max>0.33?'md':'';
      return `<i class="${cls}" style="height:${hpx}px"></i>`;
    }).join('')
  }</span>`;
}
/* NB12: in API mode show the model's real on-site avatar (proxied + disk-cached
 * by the backend); onerror falls back to the generated placeholder. Mock mode
 * keeps the generated avatar and makes zero network calls. */
function avatarImgError(img){
  img.onerror = null;
  img.src = genAvatar(img.dataset.seed, img.dataset.user);
}
function avatarEl(s, lg){
  const px = lg?40:30;
  const dims = `width="${px}" height="${px}" style="width:100%;height:100%;object-fit:cover"`;
  if (typeof apiState!=='undefined' && apiState.mode==='api'){
    const src = apiUrl('/streamers/'+encodeURIComponent(s.site)+'/'+encodeURIComponent(s.user)+'/avatar');
    return `<span class="avatar ${lg?'lg':''}"><img src="${src}" data-seed="${s.seed}" data-user="${s.user}" ${dims} alt="" onerror="avatarImgError(this)"></span>`;
  }
  return `<span class="avatar ${lg?'lg':''}"><img src="${genAvatar(s.seed,s.user)}" ${dims} alt=""></span>`;
}
/* NB13: card cover with a three-level fallback — most recent recording
 * thumbnail (NB6 /thumb) > model avatar (NB12) > generated placeholder. Mock
 * mode uses the generated placeholder directly (zero network). */
function cardCoverError(img){
  if (img.dataset.coverStage==='thumb' && img.dataset.coverAvatar){
    img.dataset.coverStage='avatar';
    img.src = img.dataset.coverAvatar;
    return;
  }
  img.onerror = null;
  img.dataset.coverStage='placeholder';
  img.src = genThumb(img.dataset.coverSeed);
}
function cardCover(s){
  if (typeof apiState!=='undefined' && apiState.mode==='api'){
    const av = apiUrl('/streamers/'+encodeURIComponent(s.site)+'/'+encodeURIComponent(s.user)+'/avatar');
    const recent = (s.files||[]).find(f=>f._filename);
    if (recent){
      const ver = s._lastRecordedTs!=null ? s._lastRecordedTs : (recent.dt&&recent.dt.getTime?Math.floor(recent.dt.getTime()/1000):0);   // busts the immutable thumb cache when a newer recording lands
      const src = apiUrl('/streamers/'+encodeURIComponent(s.site)+'/'+encodeURIComponent(s.user)+'/videos/'+encodeURIComponent(recent._filename)+'/thumb')+'?v='+ver;
      return `<img class="card-cover-img" src="${src}" alt="" loading="lazy" data-cover-stage="thumb" data-cover-avatar="${av}" data-cover-seed="${esc(s.seed)}" onerror="cardCoverError(this)">`;
    }
    return `<img class="card-cover-img" src="${av}" alt="" loading="lazy" data-cover-stage="avatar" data-cover-seed="${esc(s.seed)}" onerror="cardCoverError(this)">`;
  }
  return `<img class="card-cover-img" src="${genThumb(s.seed)}" alt="">`;
}
/* NB13/NB15: most recent recording time for the card meta row. API mode carries
 * lastRecordedTs on the list item (epoch seconds/ms, tsToSecAgo adapts); the mock
 * prototype has none, so fall back to the loaded files[0], then a dash. */
function lastRecAgo(s){
  if (s._lastRecordedTs!=null) return fmtAgo(tsToSecAgo(s._lastRecordedTs));
  const f = (s.files||[])[0];
  return f ? fmtAgo(Math.max(0, Math.floor((NOW - f.dt)/1000))) : '—';
}

/* ---------------- dashboard ---------------- */
function statusRank(s){ return STATUS_ORDER.indexOf(s.status); }
function applyFilters(){
  return streamers.filter(s=>{
    if (filters.site!=='all' && s.site!==filters.site) return false;
    if (!filterMatch(filters.status, s.status)) return false;
    // unmonitored entries with no local files stay hidden unless explicitly viewing 'stopped'
    if (s.status==='stopped' && fileCount(s)===0 && filters.status!=='stopped') return false;
    if (filters.q && !s.user.toLowerCase().includes(filters.q.toLowerCase())) return false;
    return true;
  });
}
function sortList(list){
  const dir = sort.dir;
  const key = sort.key;
  return [...list].sort((a,b)=>{
    let r=0;
    if (key==='status') r = statusRank(a)-statusRank(b) || a.user.localeCompare(b.user);
    else if (key==='name') r = a.user.localeCompare(b.user);
    else if (key==='size') r = sizeOf(b)-sizeOf(a);
    else if (key==='files') r = fileCount(b)-fileCount(a);
    else if (key==='checked') r = a.lastCheckSec-b.lastCheckSec;
    else if (key==='added') r = (a.addedDaysAgo==null?Infinity:a.addedDaysAgo)-(b.addedDaysAgo==null?Infinity:b.addedDaysAgo);
    return r*dir;
  });
}
function renderDashboard(){
  const list = sortList(applyFilters());
  const nRec = streamers.filter(s=>s.status==='recording').length;
  const nOn = streamers.filter(s=>s.status==='online'||s.status==='private').length;
  const chips = FILTER_CHIPS.map(c=>{
    const n = streamers.filter(s=>filterMatch(c,s.status)).length;
    const meta = STATUS[c==='error'?'error':c];
    const dot = meta.ring ? `background:transparent;border:1.5px solid ${meta.ring}` : `background:${meta.color}`;
    return `<button class="chip ${filters.status===c?'on':''}" data-fs="${c}">
      <span class="dot" style="${dot}"></span>${c==='error'?t('errGroup'):meta.label()} <span class="n">${n}</span></button>`;
  }).join('');
  const siteChips = Object.entries(SITES).filter(([k])=>streamers.some(s=>s.site===k))
    .map(([k,st])=>{
      const n = streamers.filter(x=>x.site===k).length;
      return `<button class="chip ${filters.site===k?'on':''}" data-site="${k}"><span class="dot" style="background:${st.color}"></span>${st.name} <span class="n">${n}</span></button>`;
    }).join('');
  const allSel = list.length>0 && list.every(s=>selected.has(s.id));
  const selArr = streamers.filter(s=>selected.has(s.id));
  const selSize = fmtSize(selArr.reduce((a,s)=>a+sizeOf(s),0));
  const inSel = selMode || selArr.length>0;   // NB9: selection mode active

  const th = (key,label,cls='')=>{
    const arr = sort.key===key ? `<span class="arrow">${sort.dir>0?'▲':'▼'}</span>`:'';
    return `<th class="sortable ${cls}" data-sort="${key}">${label}${arr}</th>`;
  };

  const rows = list.map(s=>{
    const off = s.status==='paused'||s.status==='stopped';
    return `<tr class="${s.status==='recording'?'recording':''}${s.status==='stopped'?' stopped':''}${selected.has(s.id)?' selrow':''}" data-open="${s.id}">
      <td class="sel-cell"><input type="checkbox" class="rowcheck" data-sel="${s.id}" ${selected.has(s.id)?'checked':''}></td>
      <td style="width:130px">${statusChip(s)}</td>
      <td><div class="cell-streamer">${avatarEl(s)}<div style="min-width:0">
        <div class="s-name"><span class="nm">${esc(s.user)}</span> <span class="flag">${s.flag}</span></div>
      </div></div></td>
      <td style="width:90px">${siteBadge(s.site)}</td>
      <td style="width:80px">${sparkline(s)}</td>
      <td class="num dim" style="width:60px">${fileCount(s)}${s.temps.length?` <span class="faint" title="${t('tempTitle')}: ${s.temps.length}">+${s.temps.length}</span>`:''}</td>
      <td class="num" style="width:86px">${fmtSize(sizeOf(s))}</td>
      <td class="num dim" style="width:96px">${fmtAgo(s.lastCheckSec)}</td>
      <td class="num faint" style="width:96px">${addedDate(s)}</td>
      <td style="width:116px"><div class="row-actions">
        <button class="icon-btn" data-toggle="${s.id}" title="${off?t('resume'):t('pause')}">${off?IC.playCirc:IC.pauseCirc}</button>
        <button class="icon-btn" data-ext="${s.id}" title="${t('openPage')}">${IC.ext}</button>
        <button class="icon-btn danger" data-purgevids="${s.id}" title="${fileCount(s)?t('purgeVideosTip'):t('purgeVideosNone')}"${fileCount(s)?'':' disabled'}>${IC.filmX}</button>
        <button class="icon-btn danger" data-remove="${s.id}" title="${t('removeStreamer')}">${IC.trash}</button>
      </div></td>
    </tr>`;
  }).join('');

  const cards = list.map(s=>{
    const off = s.status==='paused'||s.status==='stopped';
    const rec = s.status==='recording';
    const st = STATUS[s.status] || STATUS.offline;
    const dot = st.ring ? `background:transparent;border:1.5px solid ${st.ring}` : `background:${st.color}`;
    const corner = rec
      ? `<span class="card-live">${t('liveBadge')}</span>
        <span class="card-rec" title="${t('recInfoTitle')}">${fmtSize(s.recBytes||0)}${s.recSince?` · ${fmtDur(Math.floor((NOW-s.recSince)/1000))}`:''}${recMeta(s)}</span>`
      : `<span class="card-status"><span class="dot ${st.pulse?'pulse':''}" style="${dot}"></span>${st.label()}</span>`;
    return `<div class="card ${rec?'recording':''}${s.status==='stopped'?' stopped':''}${selected.has(s.id)?' selrow':''}" data-open="${s.id}">
      <div class="card-cover">
        ${cardCover(s)}
        ${corner}
        <span class="card-site">${siteBadge(s.site)}</span>
        <input type="checkbox" class="rowcheck card-check" data-sel="${s.id}" ${selected.has(s.id)?'checked':''}>
        <div class="card-hover"><div class="card-actions">
          <button class="icon-btn" data-toggle="${s.id}" title="${off?t('resume'):t('pause')}">${off?IC.playCirc:IC.pauseCirc}</button>
          <button class="icon-btn" data-ext="${s.id}" title="${t('openPage')}">${IC.ext}</button>
          <button class="icon-btn danger" data-purgevids="${s.id}" title="${fileCount(s)?t('purgeVideosTip'):t('purgeVideosNone')}"${fileCount(s)?'':' disabled'}>${IC.filmX}</button>
          <button class="icon-btn danger" data-remove="${s.id}" title="${t('removeStreamer')}">${IC.trash}</button>
        </div></div>
      </div>
      <div class="card-body">
        ${avatarEl(s)}
        <div class="grow">
          <div class="s-name"><span class="nm">${esc(s.user)}</span> <span class="flag">${s.flag}</span></div>
          <div class="card-stats">
            <span>${t('thFiles')} <b>${fileCount(s)}</b></span>
            <span>${t('thSize')} <b>${fmtSize(sizeOf(s))}</b></span>
            <span>${t('lastRec')} <b>${lastRecAgo(s)}</b></span>
          </div>
        </div>
        ${sparkline(s)}
      </div>
    </div>`;
  }).join('');

  const addBox = addMode==='url' ? `
    <div class="addbar">
      <div class="url-box">${IC.link}
        <input type="text" id="addUrl" placeholder="${t('addPh')}" value="${esc(addDraft.url)}" autocomplete="off" spellcheck="false">
        <span class="parsed" id="addParsed"></span>
      </div>
      <button class="btn primary" id="addSubmit">${IC.plus}${t('addBtn')}</button>
    </div>
    <div class="add-toggle"><button data-addmode="manual">${t('addManualToggle')}</button></div>`
  : `
    <div class="add-manual">
      <select id="addSite">${Object.entries(SITES).map(([k,s])=>`<option value="${k}">${s.name}</option>`).join('')}</select>
      <input type="text" id="addUser" placeholder="${t('manualUserPh')}" value="${esc(addDraft.user)}" autocomplete="off" spellcheck="false">
      <button class="btn primary" id="addSubmitManual">${IC.plus}${t('addBtn')}</button>
    </div>
    <div class="add-toggle"><button data-addmode="url">${t('addUrlToggle')}</button></div>`;

  return `
  <div class="view view-${viewMode}${selArr.length?' has-sel':''}${inSel?' selmode':''}">
    <div class="topbar">
      <h1>${t('navDashboard')}</h1>
      <div class="counts">
        <span class="c-rec">${t('recording')} <b>${nRec}</b></span>
        <span class="c-on">${t('online')} <b>${nOn}</b></span>
        <span>${t('total')} <b>${streamers.length}</b> ${t('streamers')}</span>
      </div>
      <div class="topbar-right">
        <div class="view-toggle" role="group" aria-label="${t('viewToggle')}">
          <button class="vt ${viewMode==='list'?'on':''}" data-view="list" title="${t('viewList')}">${IC.list}<span>${t('viewList')}</span></button>
          <button class="vt ${viewMode==='card'?'on':''}" data-view="card" title="${t('viewCard')}">${IC.grid}<span>${t('viewCard')}</span></button>
        </div>
        <button class="btn sel-toggle${inSel?' on':''}" id="selToggle" title="${inSel?t('selectDone'):t('selectMode')}">${IC.check}<span>${inSel?t('selectDone'):t('selectMode')}</span></button>
        <button class="btn" id="stopAll" title="${t('stopAll')}">${IC.pauseCirc}<span>${t('stopAll')}</span></button>
        <button class="btn" id="startAll" title="${t('startAll')}">${IC.playCirc}<span>${t('startAll')}</span></button>
      </div>
    </div>
    <div class="addbar-wrap">
      ${addBox}
      ${addMsg?`<div class="add-msg ${addMsg.type}">${addMsg.type==='err'?IC.alert:IC.check}${esc(addMsg.text)}</div>`:''}
    </div>
    <div class="filters site-filters">
      <div class="chip-group">
        <button class="chip ${filters.site==='all'?'on':''}" data-site="all">${t('allSites')} <span class="n">${streamers.length}</span></button>
        ${siteChips}
      </div>
    </div>
    <div class="filters status-filters">
      <div class="chip-group">
        <button class="chip ${filters.status==='all'?'on':''}" data-fs="all">${t('all')} <span class="n">${streamers.length}</span></button>
        ${chips}
      </div>
      <label class="selall-toggle"><input type="checkbox" id="selAll" ${allSel?'checked':''}>${t('selectAll')}</label>
      <div class="search-box">${IC.search}<input id="qFilter" placeholder="${t('searchPh')}" value="${esc(filters.q)}"></div>
    </div>
    <div class="list-scroll">
      ${list.length? `
      <table class="roster">
        <thead><tr>
          <th class="sel-cell"></th>
          ${th('status',t('thStatus'))}
          ${th('name',t('thStreamer'))}
          <th>${t('thSite')}</th>
          <th>${t('thPattern')}</th>
          ${th('files',t('thFiles'),'num')}
          ${th('size',t('thSize'),'num')}
          ${th('checked',t('thChecked'),'num')}
          ${th('added',t('thAdded'),'num')}
          <th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="cards">${cards}</div>`
      : `<div class="empty"><div class="big">${t('emptyFiltered')}</div><button class="btn" id="clearFilters">${t('clearFilters')}</button></div>`}
    </div>
    ${selArr.length? `
    <div class="batchbar">
      <span>${t('selectedBar')(selArr.length, selSize)}</span>
      <button class="btn" id="batchPause">${t('batchPause')}</button>
      <button class="btn" id="batchResume">${t('batchResume')}</button>
      <button class="btn" id="batchUnmon">${t('batchUnmon')}</button>
      <button class="btn danger-solid" id="batchPurge">${t('batchPurge')}</button>
      <button class="btn" id="batchClear">${t('clearSel')}</button>
    </div>`:''}
  </div>`;
}

/* ---------------- history view ---------------- */
/* V33C: in API mode show the purged model's real avatar if it's still cached on
 * disk (do_purge keeps .thumbs/avatars/); onerror falls back to the generated
 * placeholder. Mock mode uses the generated avatar directly (zero network). */
function histAvatarError(img){
  img.onerror = null;
  img.src = img.dataset.fallback;
}
function histAvatarImg(h){
  const dims = `width="30" height="30" style="width:100%;height:100%;object-fit:cover"`;
  if (typeof apiState!=='undefined' && apiState.mode==='api' && h._idx!=null){
    const src = apiUrl('/history/'+h._idx+'/avatar');
    return `<img src="${src}" data-fallback="${h.avatarDataUrl}" ${dims} alt="" onerror="histAvatarError(this)">`;
  }
  return `<img src="${h.avatarDataUrl}" ${dims} alt="">`;
}
function renderHistory(){
  const rows = history.map((h,i)=>`
    <div class="hist-row">
      <span class="avatar">${histAvatarImg(h)}</span>
      <div class="hist-main">
        <div class="s-name">${esc(h.user)}</div>
        <div class="hist-sub">${siteBadge(h.site)}<span class="hist-dates">${h.addedDate?`${t('histAdded')} ${t('dateFmt')(h.addedDate)} · `:''}${t('histRemoved')} ${t('dateFmt')(h.removedDate)}</span></div>
      </div>
      <div class="hist-acts">
        <button class="btn" data-readd="${i}">${IC.plus}<span>${t('histReadd')}</span></button>
        <button class="icon-btn" data-histext="${i}" title="${t('openPage')}">${IC.ext}</button>
        <button class="icon-btn danger" data-histdel="${i}" title="${t('histRemove')}">${IC.trash}</button>
      </div>
    </div>`).join('');
  return `
  <div class="view">
    <div class="topbar">
      <h1>${t('navHistory')}</h1>
      <div class="counts"><span>${t('total')} <b>${history.length}</b> ${t('histUnit')}</span></div>
    </div>
    <div class="list-scroll">
      ${history.length? `<div class="hist-list">${rows}</div>`
      : `<div class="empty"><div class="big">${t('histEmpty')}</div></div>`}
    </div>
  </div>`;
}

/* ---------------- dashboard selection / batch actions ---------------- */
function batchPause(){
  const n = selected.size;
  if (apiState.mode==='api'){ apiBatchTargets('pause', selectedTargets(), t('batchPauseToast')(n)); return; }
  selected.forEach(id=>{ const s=streamers.find(x=>x.id===id); if(s && s.status!=='paused' && s.status!=='stopped') s.status='paused'; });
  toast(t('batchPauseToast')(n));
  render();
}
function batchResume(){
  const n = selected.size;
  if (apiState.mode==='api'){ apiBatchTargets('resume', selectedTargets(), t('batchResumeToast')(n)); return; }
  selected.forEach(id=>{ const s=streamers.find(x=>x.id===id); if(s && (s.status==='paused'||s.status==='stopped')){ s.status='checking'; s.lastCheckSec=0;
    setTimeout(()=>{ if(streamers.includes(s) && s.status==='checking'){s.status='offline'; s.lastCheckSec=5; render();} }, 2000);
  }});
  toast(t('batchResumeToast')(n));
  render();
}
function batchUnmonitor(){
  const n = selected.size;
  if (apiState.mode==='api'){ apiBatchTargets('stopRecording', selectedTargets(), t('batchUnmonToast')(n)); return; }
  selected.forEach(id=>{ const s=streamers.find(x=>x.id===id); if(s){ s.status='stopped'; s.monitorState='unmonitored'; } });
  selected.clear();
  toast(t('batchUnmonToast')(n));
  render();
}
function batchPurge(){
  const ids = new Set(selected);
  if (apiState.mode==='api'){ apiBatchTargets('purge', selectedTargets(), t('batchPurgedToast')(ids.size), true); return; }
  streamers.forEach(s=>{ if(ids.has(s.id)) pushHistory(s); });
  streamers = streamers.filter(s=>!ids.has(s.id));
  selected.clear();
  toast(t('batchPurgedToast')(ids.size), 'warn');
  render();
}
function readdFromHistory(i){
  const h = history[i];
  if (!h) return;
  if (apiState.mode==='api'){ apiHistoryReadd(h._idx!=null?h._idx:i, h); return; }
  removeHistoryAt(i);
  const existing = streamers.find(s=>s.site===h.site && s.user.toLowerCase()===h.user.toLowerCase());
  if (existing){ nav({view:'detail', id:existing.id}); return; }
  doAdd(h.site, h.user);          // recreates the entry (checking → offline) and toasts
  nav({view:'dashboard'});        // leave the history view to reveal the new entry
  toast(t('histReaddToast')(h.user));
}
function bindHistory(){
  document.querySelectorAll('[data-readd]').forEach(b=>b.addEventListener('click', ()=>{
    readdFromHistory(+b.dataset.readd);
  }));
  document.querySelectorAll('[data-histext]').forEach(b=>b.addEventListener('click', ()=>{
    const h=history[+b.dataset.histext]; if(h) window.open(h.url, '_blank');
  }));
  document.querySelectorAll('[data-histdel]').forEach(b=>b.addEventListener('click', ()=>{
    modalState={type:'histDel', idx:+b.dataset.histdel}; renderModal();
  }));
}

