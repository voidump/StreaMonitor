'use strict';
/* ---------------- detail ---------------- */
/* V37C: skeleton grid shown while the detail payload is in flight. Uses the list-provided
 * videoCount to render up to 12 placeholder cards (same .vthumb geometry, so the real grid
 * drops in with no layout jump). When the count is unknown/zero, degrade to a centered hint. */
function skeletonGrid(s){
  const n = s._videoCount!=null && s._videoCount>0 ? Math.min(s._videoCount, 12) : 0;
  if (!n) return `<div class="empty" style="padding:30px"><span class="spin"></span> ${t('apiLoading')}</div>`;
  const card = `<div class="vthumb skeleton" aria-hidden="true">
      <div class="imgwrap"></div>
      <div class="meta">
        <div class="l1"><span class="sk-line"></span></div>
        <div class="l2"><span class="sk-line short"></span></div>
      </div>
    </div>`;
  return `<div class="thumb-grid">${card.repeat(n)}</div>`;
}
function renderDetail(s){
  const cur = s.files.find(f=>f.id===player.fileId) || null;
  const stats = schedStats(s.schedule);
  const totalSize = filesSize(s);
  const tempSize = s.temps.reduce((a,x)=>a+x.size,0);
  const avgMin = Math.round(s.files.reduce((a,f)=>a+f.dur,0)/Math.max(s.files.length,1)/60);
  const week7h = Math.round(stats.dayTotals.reduce((a,b)=>a+b,0)*0.9);
  const act = stats.total>90?t('activeHigh'):stats.total>45?t('activeMid'):t('activeLow');
  const off = s.status==='paused'||s.status==='stopped';
  const wd = t('weekdays');
  // API mode: render the honest /schedule payload (real heat + metrics, "—" when
  // a metric is null, "gathering data" until at least a week is observed). Mock
  // mode keeps the generated prototype figures untouched.
  const apiMode = apiState.mode==='api';
  const sched = s._sched;
  const accumulating = apiMode && s._schedLoaded && sched && sched.sampleDays < 7;
  const commonStr = apiMode
    ? (sched && sched.commonHours ? t('hourRange')(sched.commonHours[0], sched.commonHours[1]) : '—')
    : t('hourRange')(stats.commonStart, stats.commonEnd);
  const avgStr = apiMode
    ? (sched && sched.avgSessionMin!=null ? `${sched.avgSessionMin} ${t('minsUnit')}` : '—')
    : `${avgMin} ${t('minsUnit')}`;
  const days7Str = apiMode
    ? (sched && sched.days7OnlineHours!=null ? `${sched.days7OnlineHours} ${t('hoursUnit')}` : '—')
    : `${week7h/10} ${t('hoursUnit')}`;
  const markedFiles = s.files.filter(f=>f.marked);
  const ff = detailState.fileFilter;
  const cf = detailState.clsFilter;
  const clsBucket = f=>f._inRecycle ? 'recycle' : (f._cls==='adult'||f._cls==='non-adult'||f._cls==='unknown' ? f._cls : 'unanalyzed');
  const clsPool = ff==='marked' ? markedFiles : s.files;   // classification filter narrows whatever the marked filter left
  const displayFiles = cf==='all' ? clsPool : clsPool.filter(f=>clsBucket(f)===cf);
  const displaySize = displayFiles.reduce((a,f)=>a+f.size,0);
  // per-bucket counts + sizes over the current source (marked filter applied), so the
  // stats row and the filter counters always describe the same data set
  const CLS_BUCKETS = ['non-adult','adult','unknown','unanalyzed','recycle'];
  const clsStats = {};
  CLS_BUCKETS.forEach(k=>{ clsStats[k] = {n:0, size:0}; });
  clsPool.forEach(f=>{ const b = clsStats[clsBucket(f)]; b.n++; b.size += f.size; });
  const clsLabel = k=>k==='non-adult'?t('clsNonAdult'):k==='adult'?t('clsAdult'):k==='unknown'?t('clsUnknown'):k==='unanalyzed'?t('clsUnanalyzed'):t('clsFilterRecycle');
  const clsFilterBar = clsPool.length ? `<div class="seg clsfilter">
    <button class="${cf==='all'?'on':''}" data-clsfilter="all">${t('filterAll')}<span class="cnt">${clsPool.length}</span></button>
    ${CLS_BUCKETS.map(k=>`<button class="${cf===k?'on':''}" data-clsfilter="${k}">${clsLabel(k)}<span class="cnt">${clsStats[k].n}</span></button>`).join('')}
  </div>` : '';
  const clsStatsBar = clsPool.length ? `<div class="cls-stats">
    <span class="k">${t('clsStatsTitle')}</span>
    ${CLS_BUCKETS.map(k=>`<span class="cs-item${clsStats[k].n?'':' zero'}"><i class="dot ${k}"></i>${clsLabel(k)}<b>${clsStats[k].n}</b><em>${fmtSize(clsStats[k].size)}</em></span>`).join('')}
  </div>` : '';

  const heatRows = s.schedule.map((row,d)=>`
    <div class="hm-lab">${wd[d]}</div>
    ${row.map((v,h)=>`<div class="hm-cell l${v}" title="${wd[d]} ${h}:00"></div>`).join('')}
  `).join('');

  const thumbs = displayFiles.map(f=>{
    const name = fileName(s,f);
    const isCur = cur && cur.id===f.id;
    const confirm = confirmDelId===f.id && !f._recording;
    const watchedLabel = f.watchPct>=100?t('watched'):f.watchPct>0?`${f.watchPct}%`:t('neverWatched');
    const useApiThumb = apiState.mode==='api' && f._filename;   // real ffmpeg frame when backed by the API; else the generated placeholder
    const thumbVer = Math.floor((f.dt&&f.dt.getTime?f.dt.getTime():0)/1000);   // file mtime (s): busts the immutable thumb cache when the file is re-recorded
    const thumbSrc = useApiThumb
      ? apiUrl('/streamers/'+encodeURIComponent(s.site)+'/'+encodeURIComponent(s.user)+'/videos/'+encodeURIComponent(f._filename)+'/thumb')+'?v='+thumbVer
      : genThumb(f.id);
    const liveThumb = f._recording && liveWatchable(s);   // recording tile enters live view instead of playing the growing tmp file
    // classification badge (from the NSFW analyzer via the media index); no
    // badge at all when the file has not been analyzed (_cls null)
    const clsBadge = f._cls==='adult' ? `<span class="cls-badge adult" title="${t('clsAdultTip')}">${t('clsAdult')}</span>`
      : f._cls==='non-adult' ? `<span class="cls-badge nonadult" title="${t('clsNonAdultTip')}">${t('clsNonAdult')}</span>`
      : f._cls==='unknown' ? `<span class="cls-badge unknown" title="${t('clsUnknownTip')}">${t('clsUnknown')}</span>` : '';
    const recycleBadge = f._inRecycle ? `<span class="recycle-badge" title="${t('inRecycleTip')}">${IC.trash||''}${t('inRecycleTag')}</span>` : '';
    const trBadges = (clsBadge||recycleBadge) ? `<span class="tr-badges">${clsBadge}${recycleBadge}</span>` : '';
    // manual override menu (改判). Never offered for the file the recorder is
    // still writing — it has no media-index row yet, so the backend would 404.
    const clsMenu = clsMenuFileId===f.id && !f._recording
      ? `<span class="cls-menu">
           <span class="cm-h">${t('clsChangeTo')}</span>
           ${['adult','non-adult','unknown'].map(l=>`<button class="${f._cls===l?'on':''}" data-clsset="${l}" data-clsfile="${esc(f.id)}">${l==='adult'?t('clsAdult'):l==='non-adult'?t('clsNonAdult'):t('clsUnknown')}</button>`).join('')}
         </span>`
      : '';
    return `<div class="vthumb ${isCur?'current':''}${f.marked?' marked':''}${f._recording?' recording':''}${f._inRecycle?' inrecycle':''}" ${liveThumb?`data-watchlive="${s.id}"`:`data-playfile="${f.id}"`}>
      <div class="imgwrap">
        <img src="${thumbSrc}" alt="" loading="lazy"${useApiThumb?` data-thumb-fallback="${esc(f.id)}"`:''}>
        ${f._recording?`<span class="rec-badge">${t('recTag')}</span>`:''}
        ${f.marked?`<span class="mark-badge">${IC.markFill}${t('markedTag')}</span>`:''}
        ${trBadges}
        ${clsMenu}
        <span class="dur">${fmtDur(f.dur)}</span>
        ${f.watchPct>0?`<span class="wprog" style="width:${f.watchPct}%"></span>`:''}
        <span class="hover-play">${IC.playLg}</span>
      </div>
      <div class="meta">
        <div class="l1"><span class="fdate">${dateWd(f.dt)}</span><span class="ftime">${timeShort(f.dt)}</span></div>
        <div class="l2">
          ${confirm
            ? `<span class="confirm-del">${t('confirmDel')}
                 <button class="yes" data-delyes="${f.id}">${t('yes')}</button>
                 <button class="no" data-delno="1">${t('no')}</button></span>`
            : `<span title="${f.lastWatched?t('lastWatched')+' '+t('dtFmt')(f.lastWatched):''}">${fmtSize(f.size)} · ${watchedLabel}</span>
               <span class="acts">
                 <button class="icon-btn" data-play="${f.id}" title="${t('play')}">${IC.play}</button>
                 <button class="icon-btn ${f.marked?'markon':''}" data-mark="${f.id}" title="${f.marked?t('unmark'):t('mark')}">${f.marked?IC.markFill:IC.mark}</button>
                 <button class="icon-btn" data-dl="${f.id}" title="${t('download')}">${IC.down}</button>
                 ${f._recording ? '' : `<button class="icon-btn ${clsMenuFileId===f.id?'markon':''}" data-clsmenu="${esc(f.id)}" title="${t('clsChange')}">${IC.tag}</button>`}
                 ${f._recording
                   ? `<button class="icon-btn danger" disabled title="${t('recDelBlocked')}">${IC.trash}</button>`
                   : `<button class="icon-btn danger" data-del="${f.id}" title="${t('del')}">${IC.trash}</button>`}
               </span>`}
        </div>
      </div>
    </div>`;
  }).join('');

  const tempRows = s.temps.map(tp=>{
    const name = tempName(s,tp);
    return `<div class="temp-row">
      <span class="tag ${tp.state}">${tp.state==='fixable'?t('fixable'):tp.state==='fixing'?t('fixing'):t('broken')}</span>
      <span class="tname" title="${name}">${name}</span>
      <span class="tsize">${fmtSize(tp.size)}</span>
      ${tp.state==='fixable'?`<button class="btn" data-fix="${tp.id}">${IC.wrench}${t('fix')}</button>`:''}
      ${tp.state==='fixing'?`<span class="spin"></span>`:''}
      ${tp.state!=='fixing'?`<button class="icon-btn danger" data-tdel="${tp.id}" title="${t('del')}">${IC.trash}</button>`:''}
    </div>`;
  }).join('');

  return `
  <div class="view${markedFiles.length?' has-sel':''}">
    <div class="detail-head">
      <button class="icon-btn" data-nav="dashboard" title="${t('back')}">${IC.back}</button>
      <div class="grow">
        ${avatarEl(s,true)}
        <div class="detail-title">
          <div class="row1">
            <h2>${esc(s.user)}</h2><span class="flag">${s.flag}</span>
            ${siteBadge(s.site)}
            ${statusChip(s)}
          </div>
          <div class="row2">
            <span>${t('addedOn')} ${addedDate(s)}</span>
            <span>${t('lastCheck')} ${fmtAgo(s.lastCheckSec)}</span>
            <span>${t('filesN')(fileCount(s))} · ${fmtSize(sizeOf(s))}</span>
          </div>
        </div>
      </div>
      <button class="btn" data-toggle="${s.id}">${off?IC.playCirc:IC.pauseCirc}<span>${off?t('resume'):t('pause')}</span></button>
      <button class="btn" data-ext="${s.id}">${IC.ext}<span>${t('openPage')}</span></button>
      <button class="btn danger" data-purgevids="${s.id}"${s.files.length?'':' disabled'} title="${s.files.length?t('purgeVideosTip'):t('purgeVideosNone')}">${IC.filmX}<span>${t('purgeVideos')}</span></button>
      <button class="btn danger" data-remove="${s.id}">${IC.userMinus}<span>${t('removeStreamer')}</span></button>
    </div>
    <div class="detail-scroll">
      <div class="detail-grid">
        <div class="panel">${renderPlayer(s)}</div>
        <div style="display:flex;flex-direction:column;gap:16px">
          <div class="panel">
            <div class="panel-h">${t('schedTitle')}<span class="n">${t('schedHeat')}</span></div>
            ${accumulating
              ? `<div class="hm-empty">${t('schedAccum')(sched.sampleDays)}</div>`
              : `<div class="sched">
              <div class="heatmap">${heatRows}</div>
              <div class="hm-hours"><span></span>${Array.from({length:24},(_,h)=>`<span>${h%6===0?h:''}</span>`).join('')}</div>
            </div>`}
            <div class="stat-rows">
              <div class="stat-row"><span class="k">${t('schedCommon')}</span><span class="v accent">${commonStr}</span></div>
              <div class="stat-row"><span class="k">${t('schedAvg')}</span><span class="v">${avgStr}</span></div>
              <div class="stat-row"><span class="k">${t('schedDays7')}</span><span class="v">${days7Str}</span></div>
              ${!apiMode ? `<div class="stat-row"><span class="k">${t('schedActive')}</span><span class="v">${act}</span></div>` : ''}
            </div>
          </div>
          <div class="panel">
            <div class="panel-h">${t('storTitle')}</div>
            <div class="stat-rows" style="border-top:none">
              <div class="stat-row"><span class="k">${t('storCount')}</span><span class="v">${s.files.length}</span></div>
              <div class="stat-row"><span class="k">${t('storTemp')}</span><span class="v">${s.temps.length}${s.temps.length?` (${fmtSize(tempSize)})`:''}</span></div>
              <div class="stat-row"><span class="k">${t('storTotal')}</span><span class="v">${fmtSize(totalSize+tempSize)}</span></div>
            </div>
          </div>
        </div>
      </div>
      <div class="recordings">
        <div class="rec-section-h">
          <h3>${t('recordingsTitle')}</h3><span class="n">${displayFiles.length} · ${fmtSize(displaySize)}</span>
          ${s.files.length?`<div class="right"><div class="seg filefilter">
            <button class="${ff==='all'?'on':''}" data-filefilter="all">${t('filterAll')}<span class="cnt">${s.files.length}</span></button>
            <button class="${ff==='marked'?'on':''}" data-filefilter="marked">${t('filterMarked')}<span class="cnt">${markedFiles.length}</span></button>
          </div></div>`:''}
        </div>
        ${clsStatsBar}
        ${clsFilterBar?`<div class="cls-filter-row">${clsFilterBar}</div>`:''}
        ${s.files.length
          ? (displayFiles.length
              ? `<div class="thumb-grid">${thumbs}</div>`
              : `<div class="empty" style="padding:30px">${clsPool.length?t('noClsMatch'):t('noMarked')}</div>`)
          : (apiState.mode==='api' && s._detailError
              ? `<div class="empty" style="padding:30px">${IC.alert} ${t('detailLoadError')}
                  <button class="btn" style="margin-left:10px" onclick="retryDetail()">${IC.wrench}${t('apiRetry')}</button></div>`
              : apiState.mode==='api' && !s._detailLoaded
              ? skeletonGrid(s)
              : liveWatchable(s)
              ? `<div class="thumb-grid"><div class="vthumb recording" data-watchlive="${s.id}">
                  <div class="imgwrap">
                    <img src="${genThumb(s.seed||s.id)}" alt="" loading="lazy">
                    <span class="rec-badge">${t('recTag')}</span>
                    <span class="hover-play">${IC.playLg}</span>
                  </div>
                  <div class="meta">
                    <div class="l1"><span class="fdate">${t('recInProgress')}</span></div>
                    <div class="l2"><span>${t('liveChip')}</span></div>
                  </div>
                </div></div>`
              : `<div class="empty" style="padding:30px">${t('noRecordings')}</div>`)}
        ${s.temps.length?`
        <div class="rec-section-h" style="margin-top:22px">
          <h3>${t('tempTitle')}</h3><span class="n">${s.temps.length} · ${fmtSize(tempSize)}</span>
          <div class="right">
            ${s.temps.some(x=>x.state==='fixable')?`<button class="btn" id="fixAll">${IC.wrench}${t('fixAll')}</button>`:''}
            ${s.temps.some(x=>x.state==='broken')?`<button class="btn danger" id="cleanBroken">${IC.trash}${t('cleanBroken')}</button>`:''}
          </div>
        </div>
        <div style="font-size:11.5px;color:var(--text-faint);margin:-6px 0 10px">${t('tempHint')}</div>
        <div class="panel temp-list">${tempRows}</div>`:''}
      </div>
    </div>
    ${markedFiles.length?`<div class="batchbar">
      <span>${t('markedBar')(markedFiles.length, fmtSize(markedFiles.reduce((a,f)=>a+f.size,0)))}</span>
      <button class="btn danger" id="batchDel">${IC.trash}${t('batchDelete')}</button>
      <button class="btn" id="clearMarks">${t('clearMarks')}</button>
    </div>`:''}
  </div>`;
}

