'use strict';
/* ---------------- settings ---------------- */
let hotkeyCapture = null;   // action id currently listening for a new key, or null

/* pretty-print a stored hotkey token (e.g. 'Shift+ArrowLeft' → '⇧ ←') */
function formatHotkey(token){
  if (!token) return '—';
  const SYM = {ArrowLeft:'←', ArrowRight:'→', ArrowUp:'↑', ArrowDown:'↓', PageUp:'PgUp', PageDown:'PgDn', Escape:'Esc'};
  return token.split('+').map(p=>{
    if (p==='Shift') return '⇧';
    if (SYM[p]) return SYM[p];
    return p.length===1 ? p.toUpperCase() : p;
  }).join(' ');
}

function renderHotkeyPanel(){
  return `
      <div class="panel settings-panel hk-panel">
        <div class="panel-h">${t('hkTitle')}<span class="right"><button class="btn" data-hkreset>${t('hkReset')}</button></span></div>
        <div class="hk-body">
          <div class="hk-desc">${t('hkDesc')}</div>
          ${HOTKEY_ACTIONS.map(a=>`
            <div class="hk-row">
              <span class="hk-label">${t('hk_'+a)}</span>
              <button class="hk-badge ${hotkeyCapture===a?'listening':''}" data-hkbind="${a}">${hotkeyCapture===a ? t('hkListening') : formatHotkey(settings.hotkeys[a])}</button>
            </div>`).join('')}
        </div>
      </div>`;
}

function renderDownloadDirPanel(){
  const d = downloadDir;
  return `
      <div class="panel settings-panel dldir-panel">
        <div class="set-row dldir-row">
          <div class="grow">
            <div class="k">${t('dlDir')}</div>
            <div class="desc">${t('dlDirDesc')}</div>
            <div class="dldir-edit">
              <input type="text" id="dlDirInput" class="dldir-input" value="${esc(d.draft)}" placeholder="${t('dlDirPh')}" autocomplete="off" spellcheck="false" ${d.loading?'disabled':''}>
              <button class="btn primary" data-dldir-save ${(d.saving||d.loading)?'disabled':''}>${d.saving?t('dlDirSaving'):t('dlDirSave')}</button>
            </div>
            ${d.error?`<div class="dldir-err">${d.error}</div>`:''}
          </div>
        </div>
      </div>`;
}

function renderMinVideoSizePanel(){
  const m = minVid;
  return `
      <div class="panel settings-panel dldir-panel">
        <div class="set-row dldir-row">
          <div class="grow">
            <div class="k">${t('minVid')}</div>
            <div class="desc">${t('minVidDesc')}</div>
            <div class="dldir-edit">
              <input type="number" id="minVidInput" class="dldir-input" value="${esc(m.draft)}" placeholder="${t('minVidPh')}" min="0" max="10240" step="any" ${m.loading?'disabled':''}>
              <button class="btn primary" data-minvid-save ${(m.saving||m.loading)?'disabled':''}>${m.saving?t('dlDirSaving'):t('dlDirSave')}</button>
            </div>
            ${m.error?`<div class="dldir-err">${m.error}</div>`:''}
          </div>
        </div>
      </div>`;
}

function renderRecResolutionPanel(){
  const r = recRes;
  const busy = r.loading || r.saving;
  const isCustom = r.sel==='custom';
  const isMax = r.sel==='max';
  const opt=(v,label)=>`<option value="${v}" ${r.sel===v?'selected':''}>${label}</option>`;
  const prefOpt=(v,label)=>`<option value="${v}" ${r.pref===v?'selected':''}>${label}</option>`;
  return `
      <div class="panel settings-panel recres-panel">
        <div class="set-row dldir-row">
          <div class="grow">
            <div class="k">${t('recRes')}</div>
            <div class="desc">${t('recResDesc')}</div>
            <div class="dldir-edit">
              <select class="site-select on" data-recres-select ${busy?'disabled':''}>
                ${opt('max',t('recResMax'))}
                ${opt('1080','1080p')}
                ${opt('720','720p')}
                ${opt('480','480p')}
                ${opt('360','360p')}
                ${opt('240','240p')}
                ${opt('custom',t('recResCustom'))}
              </select>
              ${isCustom?`<input type="number" id="recResCustom" class="dldir-input" value="${esc(r.custom)}" placeholder="${t('recResCustomPh')}" min="144" max="9999" step="1" ${busy?'disabled':''}>`:''}
            </div>
            <div class="desc" style="margin-top:12px">${t('recResPref')}</div>
            <div class="dldir-edit" style="margin-top:6px">
              <select class="site-select on" data-recres-pref ${(isMax||busy)?'disabled':''}>
                ${prefOpt('closest',t('recResPrefClosest'))}
                ${prefOpt('exact_or_least_higher',t('recResPrefLeastHigher'))}
                ${prefOpt('exact_or_highest_lower',t('recResPrefHighestLower'))}
                ${prefOpt('exact',t('recResPrefExact'))}
              </select>
            </div>
            ${isMax?`<div class="desc" style="margin-top:8px">${t('recResPrefMaxNote')}</div>`:''}
            ${r.error?`<div class="dldir-err">${t(r.error)}</div>`:''}
          </div>
        </div>
      </div>`;
}

function renderNsfwPanel(){
  const n = nsfw;
  const busy = n.loading || n.saving;
  const notReady = n.loaded && (!n.modelPresent || !n.runtimeAvailable);
  const actionOpt=(v,tl,dl)=>`
            <label class="nsfw-opt">
              <input type="radio" name="nsfwAction" value="${v}" ${n.action===v?'checked':''} ${busy?'disabled':''} data-nsfw-action>
              <span><span class="t">${t(tl)}</span><div class="d">${t(dl)}</div></span>
            </label>`;
  return `
      <div class="panel settings-panel nsfw-panel">
        <div class="set-row">
          <div class="grow">
            <div class="k">${t('nsfw')}</div>
            <div class="desc">${t('nsfwDesc')}</div>
            ${notReady?`<div class="desc" style="margin-top:8px">${!n.runtimeAvailable?t('nsfwNoRuntime'):t('nsfwNoModel')}</div>`:''}
            ${n.error?`<div class="dldir-err">${t(n.error)}</div>`:''}
          </div>
          <button class="switch" role="switch" aria-checked="${n.enabled?'true':'false'}" aria-label="${t('nsfw')}" data-nsfw-toggle ${busy?'disabled':''}></button>
        </div>
        ${n.enabled?`
        <div class="nsfw-sub">
          <div>
            <div class="k" style="font-size:12.5px">${t('nsfwAction')}</div>
            <div class="desc">${t('nsfwActionDesc')}</div>
          </div>
          ${actionOpt('mark_only','nsfwActionMark','nsfwActionMarkDesc')}
          ${actionOpt('recycle','nsfwActionRecycle','nsfwActionRecycleDesc')}
          ${actionOpt('delete','nsfwActionDelete','nsfwActionDeleteDesc')}
        </div>
        ${n.action==='recycle'?`
        <div class="nsfw-sub">
          <div>
            <div class="k" style="font-size:12.5px">${t('nsfwRetention')}</div>
            <div class="desc">${t('nsfwRetentionDesc')}</div>
          </div>
          <div class="nsfw-num">
            <input type="number" id="nsfwRetentionInput" class="dldir-input" value="${esc(n.retentionDraft)}" min="1" max="90" step="1" ${busy?'disabled':''}>
            <button class="btn primary" data-nsfw-retention-save ${busy?'disabled':''}>${n.saving?t('dlDirSaving'):t('dlDirSave')}</button>
          </div>
        </div>`:''}
        <div class="nsfw-sub">
          <div>
            <div class="k" style="font-size:12.5px">${t('nsfwScan')}</div>
            <div class="desc">${t('nsfwScanDesc')}</div>
          </div>
          <div class="nsfw-num">
            <input type="number" id="nsfwScanInput" class="dldir-input" value="${esc(n.scanDraft)}" min="0" max="168" step="1" ${busy?'disabled':''}>
            <button class="btn primary" data-nsfw-scan-save ${busy?'disabled':''}>${n.saving?t('dlDirSaving'):t('dlDirSave')}</button>
          </div>
        </div>
        ${renderNsfwBackfillBlock(notReady)}`:''}
      </div>`;
}

/* Historic-file analysis (backfill): idle shows the pending count + start button,
 * running shows a progress bar, x/y counts, the current file and a cancel button.
 * The worker lives server-side; this block only reflects the polled progress. */
function renderNsfwBackfillBlock(notReady){
  const b = nsfwBackfill;
  const pct = b.total>0 ? Math.min(100, Math.round(b.done/b.total*100)) : 0;
  const body = b.running ? `
          <div class="nsfw-bf-bar"><i style="width:${pct}%"></i></div>
          <div class="nsfw-bf-status">
            <span>${t('nsfwBackfillProgress')(b.done, b.total)}${b.failed?` · ${t('nsfwBackfillFailed')(b.failed)}`:''}</span>
            ${b.currentFile?`<span class="nsfw-bf-file" title="${esc(b.currentFile)}">${t('nsfwBackfillCurrent')}${esc(b.currentFile)}</span>`:''}
          </div>
          <div class="nsfw-num">
            <button class="btn" data-nsfw-backfill-cancel ${b.cancelling?'disabled':''}>${b.cancelling?t('nsfwBackfillCancelling'):t('nsfwBackfillCancel')}</button>
          </div>` : `
          <div class="nsfw-bf-status"><span>${b.pending==null?t('nsfwBackfillPendingUnknown'):t('nsfwBackfillPending')(b.pending)}</span></div>
          <div class="nsfw-num">
            <button class="btn primary" data-nsfw-backfill-start ${(notReady||b.starting||b.pending===0)?'disabled':''}>${b.starting?t('nsfwBackfillStarting'):t('nsfwBackfillStart')}</button>
          </div>`;
  return `
        <div class="nsfw-sub">
          <div>
            <div class="k" style="font-size:12.5px">${t('nsfwBackfill')}</div>
            <div class="desc">${t('nsfwBackfillDesc')}</div>
          </div>
          ${notReady?`<div class="desc">${t('nsfwBackfillNotReady')}</div>`:''}
          ${body}
          ${b.error?`<div class="dldir-err">${t(b.error)}</div>`:''}
        </div>`;
}

/* validate + save the two NSFW numeric drafts (change-to-save on button/Enter) */
function saveNsfwRetention(){
  const raw = (nsfw.retentionDraft||'').trim();
  const v = Number(raw);
  if (raw==='' || !Number.isInteger(v) || v<1 || v>90){ nsfw.error = 'nsfwErrInvalid'; render(); return; }
  saveNsfw({recycleRetentionDays: v});
}
function saveNsfwScan(){
  const raw = (nsfw.scanDraft||'').trim();
  const v = Number(raw);
  if (raw==='' || !Number.isInteger(v) || v<0 || v>168){ nsfw.error = 'nsfwErrInvalid'; render(); return; }
  saveNsfw({scanIntervalHours: v});
}

/* Deleted-streamers entry: history is no longer a top-level nav item, so it is
 * reached from here. data-nav="history" reuses the global nav click handler. */
function renderDeletedStreamersPanel(){
  return `
      <div class="panel settings-panel">
        <div class="set-row">
          <div class="grow"><div class="k">${t('setHistory')}</div><div class="desc">${t('setHistoryDesc')}</div></div>
          <button class="btn" data-nav="history">${t('setHistoryOpen')}${history.length?` · ${history.length}`:''}</button>
        </div>
      </div>`;
}

/* Session log-out entry — only rendered when the backend reports a password is
 * configured (auth.enabled from /basesettings); otherwise there is no session
 * to end and the row would be meaningless. */
function renderLogoutPanel(){
  if (!auth.enabled) return '';
  return `
      <div class="panel settings-panel">
        <div class="set-row">
          <div class="grow"><div class="k">${t('logout')}</div><div class="desc">${t('logoutDesc')}</div></div>
          <button class="btn" data-logout ${auth.loggingOut?'disabled':''}>${auth.loggingOut?t('logoutBusy'):t('logout')}</button>
        </div>
      </div>`;
}

function renderSettings(){
  return `
  <div class="view">
    <div class="topbar"><h1>${t('settingsTitle')}</h1></div>
    <div class="settings-wrap">
      <div class="panel settings-panel">
        <div class="set-row">
          <div class="grow"><div class="k">${t('language')}</div><div class="desc">${t('languageDesc')}</div></div>
          <select class="site-select on" data-lang-select>
            <option value="zh" ${lang==='zh'?'selected':''}>${t('langZh')}</option>
            <option value="en" ${lang==='en'?'selected':''}>${t('langEn')}</option>
          </select>
        </div>
        <div class="set-row" style="opacity:.55">
          <div class="grow"><div class="k">${t('setAutoClean')}</div><div class="desc">${t('setAutoCleanDesc')}</div></div>
          <span class="soon">${t('soon')}</span>
        </div>
        <div class="set-row" style="opacity:.55">
          <div class="grow"><div class="k">${t('setFreq')}</div><div class="desc">${t('setFreqDesc')}</div></div>
          <span class="soon">${t('soon')}</span>
        </div>
      </div>
      ${renderDeletedStreamersPanel()}
      ${renderDownloadDirPanel()}
      ${renderMinVideoSizePanel()}
      ${renderNsfwPanel()}
      ${renderRecResolutionPanel()}
      ${renderHotkeyPanel()}
      ${renderLogoutPanel()}
    </div>
  </div>`;
}

/* ---- hotkey editor interactions (settings.js owns its own delegated listeners) ---- */
document.addEventListener('click', e=>{
  if (route.view!=='settings') return;
  const dlSave = e.target.closest('[data-dldir-save]');
  if (dlSave){
    e.stopPropagation();
    const inp = document.getElementById('dlDirInput');
    if (inp) downloadDir.draft = inp.value;
    saveDownloadDir();
    return;
  }
  const mvSave = e.target.closest('[data-minvid-save]');
  if (mvSave){
    e.stopPropagation();
    const inp = document.getElementById('minVidInput');
    if (inp) minVid.draft = inp.value;
    saveMinVideoSize();
    return;
  }
  const nToggle = e.target.closest('[data-nsfw-toggle]');
  if (nToggle){
    e.stopPropagation();
    saveNsfw({enabled: !nsfw.enabled});
    return;
  }
  const nRet = e.target.closest('[data-nsfw-retention-save]');
  if (nRet){
    e.stopPropagation();
    const inp = document.getElementById('nsfwRetentionInput');
    if (inp) nsfw.retentionDraft = inp.value;
    saveNsfwRetention();
    return;
  }
  const nScan = e.target.closest('[data-nsfw-scan-save]');
  if (nScan){
    e.stopPropagation();
    const inp = document.getElementById('nsfwScanInput');
    if (inp) nsfw.scanDraft = inp.value;
    saveNsfwScan();
    return;
  }
  if (e.target.closest('[data-nsfw-backfill-start]')){
    e.stopPropagation();
    startNsfwBackfill();
    return;
  }
  if (e.target.closest('[data-nsfw-backfill-cancel]')){
    e.stopPropagation();
    cancelNsfwBackfill();
    return;
  }
  if (e.target.closest('[data-logout]')){
    e.stopPropagation();
    doLogout();
    return;
  }
  const bind = e.target.closest('[data-hkbind]');
  if (bind){
    e.stopPropagation();
    hotkeyCapture = hotkeyCapture===bind.dataset.hkbind ? null : bind.dataset.hkbind;
    bind.blur();   // keep Space/Enter from re-triggering the button while capturing
    render();
    return;
  }
  if (e.target.closest('[data-hkreset]')){
    e.stopPropagation();
    hotkeyCapture = null;
    resetHotkeys();
    toast(t('hkResetToast'));
    render();
    return;
  }
  if (hotkeyCapture){ hotkeyCapture = null; render(); }   // any other click cancels capture
});

/* download-dir input: keep the draft in sync (survives poll re-renders) and
 * save on Enter. Runs in bubble phase, after the capture-phase hotkey handler. */
document.addEventListener('input', e=>{
  if (route.view!=='settings') return;
  if (e.target && e.target.id==='dlDirInput'){
    downloadDir.draft = e.target.value;
    if (downloadDir.error){ downloadDir.error = null; render(); }
  }
  if (e.target && e.target.id==='minVidInput'){   // keep the threshold draft in sync across poll re-renders
    minVid.draft = e.target.value;
    if (minVid.error){ minVid.error = null; render(); }
  }
  if (e.target && e.target.id==='recResCustom'){   // keep the custom-height draft in sync across poll re-renders
    recRes.custom = e.target.value;
    if (recRes.error){ recRes.error = null; render(); }
  }
  if (e.target && e.target.id==='nsfwRetentionInput'){   // keep the NSFW drafts in sync across poll re-renders
    nsfw.retentionDraft = e.target.value;
    if (nsfw.error){ nsfw.error = null; render(); }
  }
  if (e.target && e.target.id==='nsfwScanInput'){
    nsfw.scanDraft = e.target.value;
    if (nsfw.error){ nsfw.error = null; render(); }
  }
});
document.addEventListener('change', e=>{
  if (route.view!=='settings') return;
  const sel = e.target.closest('[data-lang-select]');
  if (sel){ lang=sel.value; localStorage.setItem('sm_lang',lang); render(); return; }
  const rsel = e.target.closest('[data-recres-select]');
  if (rsel){
    recRes.sel = rsel.value; recRes.error = null;
    if (recRes.sel==='custom'){ render(); const inp=document.getElementById('recResCustom'); if(inp) inp.focus(); return; }   // wait for a valid number before saving
    saveRecResolution();
    return;
  }
  const rpref = e.target.closest('[data-recres-pref]');
  if (rpref){ recRes.pref = rpref.value; saveRecResolution(); return; }
  const nact = e.target.closest('[data-nsfw-action]');
  if (nact){ saveNsfw({action: nact.value}); return; }
  const rcustom = e.target.closest('#recResCustom');
  if (rcustom){ recRes.custom = rcustom.value; saveRecResolution(); return; }
});
document.addEventListener('keydown', e=>{
  if (route.view!=='settings') return;
  if (e.target && e.target.id==='dlDirInput' && e.key==='Enter'){
    e.preventDefault();
    downloadDir.draft = e.target.value;
    saveDownloadDir();
  }
  if (e.target && e.target.id==='minVidInput' && e.key==='Enter'){
    e.preventDefault();
    minVid.draft = e.target.value;
    saveMinVideoSize();
  }
  if (e.target && e.target.id==='recResCustom' && e.key==='Enter'){
    e.preventDefault();
    recRes.custom = e.target.value;
    saveRecResolution();
  }
  if (e.target && e.target.id==='nsfwRetentionInput' && e.key==='Enter'){
    e.preventDefault();
    nsfw.retentionDraft = e.target.value;
    saveNsfwRetention();
  }
  if (e.target && e.target.id==='nsfwScanInput' && e.key==='Enter'){
    e.preventDefault();
    nsfw.scanDraft = e.target.value;
    saveNsfwScan();
  }
});

/* capture-phase so it resolves the key before the player's global keydown handler */
document.addEventListener('keydown', e=>{
  if (!hotkeyCapture || route.view!=='settings') return;
  e.preventDefault(); e.stopPropagation();
  if (e.key==='Escape'){ hotkeyCapture = null; render(); return; }
  if (e.key==='Shift'||e.key==='Control'||e.key==='Alt'||e.key==='Meta') return;   // wait for a real key
  const action = hotkeyCapture;
  const res = rebindHotkey(action, hotkeyToken(e));
  hotkeyCapture = null;
  if (!res.ok) toast(t('hkConflict')(t('hk_'+res.conflict)), 'warn');
  render();
}, true);

