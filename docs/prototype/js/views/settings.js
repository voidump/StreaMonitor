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

function renderSettings(){
  return `
  <div class="view">
    <div class="topbar"><h1>${t('settingsTitle')}</h1></div>
    <div class="settings-wrap">
      <div class="panel settings-panel">
        <div class="set-row">
          <div class="grow"><div class="k">${t('language')}</div><div class="desc">${t('languageDesc')}</div></div>
          <div class="seg">
            <button class="${lang==='zh'?'on':''}" data-lang="zh">中文</button>
            <button class="${lang==='en'?'on':''}" data-lang="en">English</button>
          </div>
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
      ${renderHotkeyPanel()}
    </div>
  </div>`;
}

/* ---- hotkey editor interactions (settings.js owns its own delegated listeners) ---- */
document.addEventListener('click', e=>{
  if (route.view!=='settings') return;
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

