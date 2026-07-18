'use strict';
/* ---------------- modal ---------------- */
function renderModal(){
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
    const files = arr.reduce((a,x)=>a+x.files.length+x.temps.length, 0);
    const sz = fmtSize(arr.reduce((a,x)=>a+filesSize(x)+x.temps.reduce((b,tp)=>b+tp.size,0), 0));
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
        <div class="modal-h">${t('histDelTitle')(h.user)}</div>
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
  if (modalState.type==='purgeVideos'){
    const n = fileCount(s);
    const sz = fmtSize(sizeOf(s));
    root.innerHTML = `
    <div class="modal-backdrop" id="modalBk">
      <div class="modal">
        <div class="modal-h">${t('purgeVideosTitle')(s.user)}</div>
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
  const n = s.files.length + s.temps.length;
  const sz = fmtSize(filesSize(s)+s.temps.reduce((a,x)=>a+x.size,0));
  root.innerHTML = `
  <div class="modal-backdrop" id="modalBk">
    <div class="modal">
      <div class="modal-h">${t('removeTitle')(s.user)}</div>
      <div class="modal-b">
        ${alreadyStopped?'':`
        <label class="opt ${!delFiles?'sel':''}">
          <input type="radio" name="rmopt" ${!delFiles?'checked':''} data-rmopt="keep">
          <span><span class="t">${t('unmonT')}</span><br><span class="d">${t('unmonD')}</span></span>
        </label>`}
        <label class="opt ${delFiles?'sel danger':''}">
          <input type="radio" name="rmopt" ${delFiles?'checked':''} data-rmopt="del">
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
