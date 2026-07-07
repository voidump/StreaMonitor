'use strict';
/* ---------------- detail ---------------- */
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
  const markedFiles = s.files.filter(f=>f.marked);
  const ff = detailState.fileFilter;
  const displayFiles = ff==='marked' ? markedFiles : s.files;
  const displaySize = displayFiles.reduce((a,f)=>a+f.size,0);

  const heatRows = s.schedule.map((row,d)=>`
    <div class="hm-lab">${wd[d]}</div>
    ${row.map((v,h)=>`<div class="hm-cell l${v}" title="${wd[d]} ${h}:00"></div>`).join('')}
  `).join('');

  const thumbs = displayFiles.map(f=>{
    const name = fileName(s,f);
    const isCur = cur && cur.id===f.id;
    const confirm = confirmDelId===f.id && !f._recording;
    const watchedLabel = f.watchPct>=100?t('watched'):f.watchPct>0?`${f.watchPct}%`:t('neverWatched');
    return `<div class="vthumb ${isCur?'current':''}${f.marked?' marked':''}${f._recording?' recording':''}" data-playfile="${f.id}">
      <div class="imgwrap">
        <img src="${genThumb(f.id)}" alt="" loading="lazy">
        ${f._recording?`<span class="rec-badge">${t('recTag')}</span>`:''}
        ${f.marked?`<span class="mark-badge">${IC.markFill}${t('markedTag')}</span>`:''}
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
            <span>${t('filesN')(s.files.length)} · ${fmtSize(totalSize)}</span>
          </div>
        </div>
      </div>
      ${liveWatchable(s)?`<button class="btn live-btn" data-watchlive="${s.id}"><span class="ldot"></span><span>${t('watchLive')}</span></button>`:''}
      <button class="btn" data-toggle="${s.id}">${off?IC.playCirc:IC.pauseCirc}<span>${off?t('resume'):t('pause')}</span></button>
      <button class="btn" data-ext="${s.id}">${IC.ext}<span>${t('openPage')}</span></button>
      <button class="btn danger" data-remove="${s.id}">${IC.userMinus}<span>${t('removeStreamer')}</span></button>
    </div>
    <div class="detail-scroll">
      <div class="detail-grid">
        <div class="panel">${renderPlayer(s)}</div>
        <div style="display:flex;flex-direction:column;gap:16px">
          <div class="panel">
            <div class="panel-h">${t('schedTitle')}<span class="n">${t('schedHeat')}</span></div>
            <div class="sched">
              <div class="heatmap">${heatRows}</div>
              <div class="hm-hours"><span></span>${Array.from({length:24},(_,h)=>`<span>${h%6===0?h:''}</span>`).join('')}</div>
            </div>
            <div class="stat-rows">
              <div class="stat-row"><span class="k">${t('schedCommon')}</span><span class="v accent">${t('hourRange')(stats.commonStart, stats.commonEnd)}</span></div>
              <div class="stat-row"><span class="k">${t('schedAvg')}</span><span class="v">${avgMin} ${t('minsUnit')}</span></div>
              <div class="stat-row"><span class="k">${t('schedDays7')}</span><span class="v">${week7h/10} ${t('hoursUnit')}</span></div>
              <div class="stat-row"><span class="k">${t('schedActive')}</span><span class="v">${act}</span></div>
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
        ${s.files.length
          ? (displayFiles.length
              ? `<div class="thumb-grid">${thumbs}</div>`
              : `<div class="empty" style="padding:30px">${t('noMarked')}</div>`)
          : `<div class="empty" style="padding:30px">${t('noRecordings')}</div>`}
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

