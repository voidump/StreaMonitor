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
      ${renderDownloadDirPanel()}
      ${renderHotkeyPanel()}
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
});
document.addEventListener('change', e=>{
  if (route.view!=='settings') return;
  const sel = e.target.closest('[data-lang-select]');
  if (sel){ lang=sel.value; localStorage.setItem('sm_lang',lang); render(); }
});
document.addEventListener('keydown', e=>{
  if (route.view!=='settings') return;
  if (e.target && e.target.id==='dlDirInput' && e.key==='Enter'){
    e.preventDefault();
    downloadDir.draft = e.target.value;
    saveDownloadDir();
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

