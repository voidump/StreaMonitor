'use strict';
/* ---------------- modal ---------------- */
/* The entrance animation (backdrop fadein + modal pop) should play only when a
 * modal first opens. Re-renders — switching the remove-dialog radio option, or
 * the full render()→renderView()→renderModal() rebuild fired by list polling —
 * recreate the backdrop DOM and would otherwise replay the animation, making the
 * whole screen appear to flash. Track the shown modal's signature; on a re-render
 * of the same modal, tag the backdrop with `no-anim` to skip the animation. */
let _modalSig = null;
function modalSig(){
  if (!modalState) return null;
  const key = modalState.id!=null ? modalState.id : (modalState.idx!=null ? modalState.idx : '');
  return modalState.type + ':' + key;
}
function renderModal(){
  const sig = modalSig();
  const animate = sig!==null && sig!==_modalSig;
  renderModalInner();
  _modalSig = modalSig();   // recompute: inner may clear modalState (missing streamer/history)
  if (!animate){
    const bk = document.getElementById('modalBk');
    if (bk) bk.classList.add('no-anim');
  }
}
/* Switching the remove-dialog radio must not rebuild the backdrop: even with
 * no-anim, destroying and recreating the blurred backdrop recomposites the
 * whole viewport and reads as a full-page flash. Update only the affected
 * nodes (option selection + confirm button) in place. */
function updateRemoveModal(){
  if (!modalState || modalState.type!=='remove') return;
  const bk = document.getElementById('modalBk');
  if (!bk){ renderModal(); return; }
  const delFiles = !!modalState.delFiles;
  bk.querySelectorAll('.opt[data-rmopt]').forEach(opt=>{
    const on = (opt.dataset.rmopt==='del') === delFiles;
    opt.classList.toggle('sel', on);
    opt.classList.toggle('danger', on && delFiles);
    const r = opt.querySelector('input[type="radio"]');
    if (r) r.checked = on;
  });
  const btn = document.getElementById('modalConfirm');
  if (btn){
    btn.classList.toggle('danger-solid', delFiles);
    btn.classList.toggle('primary', !delFiles);
    btn.textContent = delFiles ? t('purgeBtn') : t('unmonBtn');
  }
}
function renderModalInner(){
  const root = document.getElementById('modalRoot');
  if (!modalState){ root.innerHTML=''; return; }
  if (modalState.type==='batchUnmon'){
    const arr = streamers.filter(x=>selected.has(x.id));
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('batchUnmonTitle')(arr.length)}</div>
        <div class="modal-b" style="padding-bottom:14px">${t('batchUnmonDesc')}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn primary" id="modalConfirm">${t('batchUnmonBtn')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  if (modalState.type==='batchPurge'){
    const arr = streamers.filter(x=>selected.has(x.id));
    // fileCount/sizeOf: in API mode s.files is only populated after the detail
    // view loads, so counting s.files here showed "0 files (0 KB)" on an
    // irreversible delete. The list payload's aggregates are always present.
    const files = arr.reduce((a,x)=>a+fileCount(x)+x.temps.length, 0);
    const sz = fmtSize(arr.reduce((a,x)=>a+sizeOf(x)+x.temps.reduce((b,tp)=>b+tp.size,0), 0));
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('batchPurgeTitle')(arr.length)}</div>
        <div class="modal-b" style="padding-bottom:14px">${t('batchPurgeDesc')(arr.length, files, sz)}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn danger-solid" id="modalConfirm">${t('batchPurgeBtn')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  if (modalState.type==='histDel'){
    const h = history[modalState.idx];
    if (!h){ modalState=null; root.innerHTML=''; return; }
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('histDelTitle')(esc(h.user))}</div>
        <div class="modal-b" style="padding-bottom:14px">${t('histDelDesc')}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn danger-solid" id="modalConfirm">${t('histDelBtn')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  const s = streamers.find(x=>x.id===modalState.id);
  if (!s){ modalState=null; root.innerHTML=''; return; }
  // manual non-adult override under a recycle/delete policy: the file is not
  // touched now, but the next scheduled scan will act on it — say so explicitly
  if (modalState.type==='clsManual'){
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('clsManualTitle')}</div>
        <div class="modal-b" style="padding-bottom:14px">${nsfw.action==='delete'?t('clsManualDescDelete'):t('clsManualDescRecycle')}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn ${nsfw.action==='delete'?'danger-solid':'primary'}" id="modalConfirm">${t('clsManualBtn')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  if (modalState.type==='purgeVideos'){
    const n = fileCount(s);
    const sz = fmtSize(sizeOf(s));
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('purgeVideosTitle')(esc(s.user))}</div>
        <div class="modal-b" style="padding-bottom:14px">${t('purgeVideosDesc')(n, sz)}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn danger-solid" id="modalConfirm">${t('purgeVideosBtn')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  if (modalState.type==='batch'){
    const marked = s.files.filter(f=>f.marked);
    const msz = fmtSize(marked.reduce((a,f)=>a+f.size,0));
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('batchConfirmTitle')(marked.length)}</div>
        <div class="modal-b" style="padding-bottom:14px">${t('batchConfirmDesc')(marked.length, msz)}</div>
        <div class="modal-f">
          <button class="btn" id="modalCancel">${t('cancel')}</button>
          <button class="btn danger-solid" id="modalConfirm">${t('batchDelete')}</button>
        </div>
      </div>
    </div>`;
    return;
  }
  const alreadyStopped = s.status==='stopped';   // keep-files option is the current state — offer delete only
  const delFiles = alreadyStopped ? true : modalState.delFiles;
  // Same as batchPurge above: use the list aggregates, s.files may be empty.
  const n = fileCount(s) + s.temps.length;
  const sz = fmtSize(sizeOf(s)+s.temps.reduce((a,x)=>a+x.size,0));
  root.innerHTML = `
  <div class="modal-backdrop" id="modalBk">
    <div class="modal">
      <div class="modal-h">${t('removeTitle')(esc(s.user))}</div>
      <div class="modal-b">
        ${alreadyStopped?'':`
        <label class="opt ${!delFiles?'sel':''}" data-rmopt="keep">
          <input type="radio" name="rmopt" ${!delFiles?'checked':''}>
          <span><span class="t">${t('unmonT')}</span><br><span class="d">${t('unmonD')}</span></span>
        </label>`}
        <label class="opt ${delFiles?'sel danger':''}" data-rmopt="del">
          <input type="radio" name="rmopt" ${delFiles?'checked':''}>
          <span><span class="t">${t('purgeT')}</span><br><span class="d">${t('purgeD')(n, sz)}</span></span>
        </label>
      </div>
      <div class="modal-f">
        <button class="btn" id="modalCancel">${t('cancel')}</button>
        <button class="btn ${delFiles?'danger-solid':'primary'}" id="modalConfirm">${delFiles?t('purgeBtn'):t('unmonBtn')}</button>
      </div>
    </div>
  </div>`;
}
