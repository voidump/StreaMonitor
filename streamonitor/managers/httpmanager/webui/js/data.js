'use strict';
const NOW = new Date('2026-07-05T21:30:00');

/* ---------------- sites ---------------- */
const SITES = {
  chaturbate:{name:'Chaturbate', abbr:'CB', color:'#f5a623', hosts:['chaturbate.com'], url:u=>`https://chaturbate.com/${u}/`},
  stripchat:{name:'StripChat', abbr:'SC', color:'#e0455a', hosts:['stripchat.com'], url:u=>`https://stripchat.com/${u}`},
  bongacams:{name:'BongaCams', abbr:'BC', color:'#ff7a45', hosts:['bongacams.com'], url:u=>`https://bongacams.com/${u}`},
  camsoda:{name:'CamSoda', abbr:'CS', color:'#2dd4bf', hosts:['camsoda.com'], url:u=>`https://camsoda.com/${u}`},
  myfreecams:{name:'MyFreeCams', abbr:'MFC', color:'#6ee76e', hosts:['myfreecams.com'], url:u=>`https://myfreecams.com/#${u}`},
  cam4:{name:'Cam4', abbr:'C4', color:'#fb923c', hosts:['cam4.com'], url:u=>`https://cam4.com/${u}`},
  flirt4free:{name:'Flirt4Free', abbr:'F4F', color:'#ec6ba8', hosts:['flirt4free.com'], url:u=>`https://flirt4free.com/?model=${u}`},
  amateurtv:{name:'Amateur.tv', abbr:'ATV', color:'#60a5fa', hosts:['amateur.tv'], url:u=>`https://amateur.tv/${u}`},
  streamate:{name:'Streamate', abbr:'SM', color:'#a78bfa', hosts:['streamate.com'], url:u=>`https://streamate.com/cam/${u}`},
  cherrytv:{name:'Cherry.tv', abbr:'CHT', color:'#f43f5e', hosts:['cherry.tv'], url:u=>`https://cherry.tv/${u}`},
  camscom:{name:'Cams.com', abbr:'CAM', color:'#38bdf8', hosts:['cams.com'], url:u=>`https://cams.com/${u}`},
  manyvids:{name:'ManyVids Live', abbr:'MV', color:'#e879f9', hosts:['manyvids.com','live.manyvids.com'], url:u=>`https://live.manyvids.com/stream/${u}`},
  xlovecam:{name:'XLoveCam', abbr:'XLC', color:'#f87171', hosts:['xlovecam.com'], url:u=>`https://xlovecam.com/${u}`},
  dreamcam:{name:'Dreamcam', abbr:'DC', color:'#818cf8', hosts:['dreamcam.com'], url:u=>`https://dreamcam.com/models/${u}`},
};

/* ---------------- status meta ---------------- */
/* keys mirror streamonitor Status enum semantics */
const STATUS = {
  recording:{color:'var(--rec)', label:()=>t('recording'), pulse:true},
  online:{color:'var(--online)', label:()=>t('online')},
  private:{color:'var(--private)', label:()=>t('private')},
  offline:{color:'var(--offline)', label:()=>t('offline')},
  long_offline:{color:'var(--long-offline)', label:()=>t('longOffline')},
  paused:{color:'transparent', label:()=>t('paused'), ring:'var(--paused)'},
  stopped:{color:'transparent', label:()=>t('stopped'), ring:'var(--stopped)'},
  ratelimit:{color:'var(--ratelimit)', label:()=>t('ratelimit')},
  error:{color:'var(--error)', label:()=>t('error')},
  restricted:{color:'var(--restricted)', label:()=>t('restricted')},
  checking:{color:'var(--accent)', label:()=>t('checking')},
};
const STATUS_ORDER = ['recording','online','private','checking','ratelimit','error','restricted','offline','long_offline','paused','stopped'];
const FILTER_CHIPS = ['recording','online','paused','stopped','offline','error']; // condensed filter set; error covers error/ratelimit/restricted
const filterMatch = (chip, st) => {
  if (chip==='all') return true;
  if (chip==='offline') return st==='offline'||st==='long_offline';
  if (chip==='error') return st==='error'||st==='ratelimit'||st==='restricted';
  return st===chip;
};

/* ---------------- seeded rng ---------------- */
function rng(seed){
  let s = 0;
  for (const c of String(seed)) s = (s*31 + c.charCodeAt(0)) >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/* ---------------- fake data ---------------- */
function mkFiles(seed, count, baseHour, tempSpec){
  const r = rng(seed+':files');
  const files = [];
  let day = 0;
  for (let i=0;i<count;i++){
    day += 1 + Math.floor(r()*3);
    const d = new Date(NOW); d.setDate(d.getDate()-day);
    d.setHours(baseHour + Math.floor(r()*4) - 1, Math.floor(r()*60), Math.floor(r()*60));
    const dur = 900 + Math.floor(r()*9000);            // 15min – 2.75h
    const size = Math.floor(dur * (350+r()*450) * 1024); // ~ bitrate
    const watched = r();
    const wp = watched<0.35?0:(watched>0.8?100:Math.floor(r()*90)+5);
    const lw = wp>0 ? new Date(d.getTime() + (1+r()*20)*86400000) : null;
    files.push({id:seed+'-f'+i, dt:d, dur, size, watchPct:wp, marked:false,
      lastWatched: lw && lw<NOW ? lw : (wp>0?new Date(NOW.getTime()-r()*5*86400000):null)});
  }
  files.sort((a,b)=>b.dt-a.dt);
  const temps = (tempSpec||[]).map((st,i)=>{
    const d = new Date(NOW); d.setDate(d.getDate()-Math.floor(r()*6)); d.setHours(baseHour, Math.floor(r()*60));
    return {id:seed+'-t'+i, dt:d, size:Math.floor((20+r()*700)*1024*1024), state:st};
  });
  return {files, temps};
}
function mkSchedule(seed, evening, weekend, activity){
  // heat[7][24] 0..3 — built from a "usual hours" window + noise
  const r = rng(seed+':sched');
  const heat = [];
  for (let d=0; d<7; d++){
    const row = [];
    const dayBoost = weekend ? (d>=5?1.5:0.8) : (d>=5?0.7:1.1);
    for (let h=0; h<24; h++){
      const inWin = h>=evening[0] && h<evening[1];
      let v = inWin ? activity*dayBoost*(0.65+r()*0.6) : r()*0.35*activity;
      row.push(v>2.2?3 : v>1.4?2 : v>0.7?1 : 0);
    }
    heat.push(row);
  }
  return heat;
}
function schedStats(heat){
  const hourTotals = Array(24).fill(0);
  heat.forEach(row=>row.forEach((v,h)=>hourTotals[h]+=v));
  let best=0, bestH=20;
  for(let h=0;h<24;h++){
    const w = hourTotals[h]+ (hourTotals[(h+1)%24]||0) + (hourTotals[(h+2)%24]||0);
    if (w>best){best=w;bestH=h;}
  }
  const total = hourTotals.reduce((a,b)=>a+b,0);
  const dayTotals = heat.map(r=>r.reduce((a,b)=>a+b,0));
  return {commonStart:bestH, commonEnd:(bestH+3)%24, total, dayTotals};
}

function mkStreamer(o){
  const seed = o.site+'/'+o.user;
  const {files,temps} = mkFiles(seed, o.nfiles, o.evening[0]+1, o.temps);
  const s = Object.assign({
    id:seed, seed,
    flag:'', addedDaysAgo:60, lastCheckSec:45,
    recBytes:0, recSince:null,
    monitorState:'active',   // 'active'|'unmonitored' — v2 scaffolding, maps 'stopped' status
    schedule: mkSchedule(seed, o.evening, o.weekend||false, o.activity||1.4),
  }, o);
  s.files = files; s.temps = temps;   // must win over the raw spec in o
  if (s.status==='stopped') s.monitorState='unmonitored';
  return s;
}

let streamers = [
  mkStreamer({site:'chaturbate', user:'velvet_ada', status:'recording', flag:'🇨🇴', evening:[20,26>24?24:26], nfiles:14, temps:['fixable'], addedDaysAgo:212, lastCheckSec:8, activity:1.9, recBytes: 683*1024*1024, recSince:new Date(NOW.getTime()-52*60000)}),
  mkStreamer({site:'stripchat', user:'mira_wave', status:'recording', flag:'🇺🇦', evening:[18,23], nfiles:22, temps:[], addedDaysAgo:388, lastCheckSec:12, activity:2.0, recBytes: 1.42*1024*1024*1024, recSince:new Date(NOW.getTime()-124*60000)}),
  mkStreamer({site:'chaturbate', user:'noraskye', status:'online', flag:'🇺🇸', evening:[21,24], nfiles:9, temps:['fixable','broken'], addedDaysAgo:96, lastCheckSec:31, activity:1.5}),
  mkStreamer({site:'bongacams', user:'ksenia_lov', status:'online', flag:'🇷🇺', evening:[17,22], nfiles:31, temps:[], addedDaysAgo:451, lastCheckSec:55, activity:1.7, weekend:true}),
  mkStreamer({site:'camsoda', user:'lil_June', status:'private', flag:'🇧🇷', evening:[22,24], nfiles:6, temps:['broken'], addedDaysAgo:44, lastCheckSec:18, activity:1.2}),
  mkStreamer({site:'stripchat', user:'aki_tan', status:'offline', flag:'🇯🇵', evening:[13,17], nfiles:17, temps:[], addedDaysAgo:170, lastCheckSec:140, activity:1.4}),
  mkStreamer({site:'chaturbate', user:'gingerlily', status:'offline', flag:'🇬🇧', evening:[19,23], nfiles:4, temps:[], addedDaysAgo:23, lastCheckSec:95, activity:0.9}),
  mkStreamer({site:'myfreecams', user:'DaisyPetal', status:'offline', flag:'🇨🇦', evening:[20,24], nfiles:11, temps:['fixable'], addedDaysAgo:301, lastCheckSec:200, activity:1.1, weekend:true}),
  mkStreamer({site:'cam4', user:'tomas_leo', status:'long_offline', flag:'🇪🇸', evening:[21,24], nfiles:2, temps:[], addedDaysAgo:130, lastCheckSec:3600*9, activity:0.4}),
  mkStreamer({site:'bongacams', user:'anna_belle99', status:'paused', flag:'🇩🇪', evening:[18,22], nfiles:27, temps:['broken','broken','fixable'], addedDaysAgo:512, lastCheckSec:86400*3, activity:1.3}),
  mkStreamer({site:'flirt4free', user:'SelenaRay', status:'ratelimit', flag:'🇺🇸', evening:[22,24], nfiles:7, temps:[], addedDaysAgo:66, lastCheckSec:240, activity:1.2}),
  mkStreamer({site:'stripchat', user:'nova_kit', status:'error', flag:'🇵🇱', evening:[16,21], nfiles:13, temps:['fixable'], addedDaysAgo:154, lastCheckSec:520, activity:1.5}),
  mkStreamer({site:'amateurtv', user:'carla_mx', status:'restricted', flag:'🇲🇽', evening:[19,23], nfiles:5, temps:[], addedDaysAgo:88, lastCheckSec:400, activity:1.0}),
  mkStreamer({site:'camsoda', user:'peachtea_', status:'offline', flag:'🇦🇷', evening:[23,24], nfiles:19, temps:[], addedDaysAgo:270, lastCheckSec:75, activity:1.6, weekend:true}),
  mkStreamer({site:'streamate', user:'holly_dee', status:'stopped', flag:'🇺🇸', evening:[19,23], nfiles:12, temps:[], addedDaysAgo:330, lastCheckSec:86400*21, activity:1.3, weekend:true}),
];

/* orphan local videos: exist on disk though streamer not yet monitored */
const ORPHANS = {
  'chaturbate/cutelily': 3,
};

const DISK = {total: 2*1024*1024*1024*1024, used: 1.37*1024*1024*1024*1024};
const MOCK_DOWNLOAD_DIR = '/srv/streamonitor/downloads';   // shown by the settings panel in mock mode (no backend)

/* ---------------- formatting ---------------- */
function fmtSize(b){
  if (b>=1024**4) return (b/1024**4).toFixed(2)+' TB';
  if (b>=1024**3) return (b/1024**3).toFixed(2)+' GB';
  if (b>=1024**2) return (b/1024**2).toFixed(0)+' MB';
  return (b/1024).toFixed(0)+' KB';
}
function fmtSpeed(bps){
  if (bps==null || !(bps>0)) return '';   // unknown / first poll — degrade to nothing
  if (bps>=1024**2) return (bps/1024**2).toFixed(1)+' MB/s';
  if (bps>=1024) return (bps/1024).toFixed(0)+' KB/s';
  return Math.round(bps)+' B/s';
}
function fmtDur(s){
  if (s==null || !isFinite(s)) return '--:--';   // unknown duration (backend still backfilling) — placeholder, not 0:00
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60), sec=Math.floor(s%60);
  return h>0 ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
}
function fmtAgo(sec){
  if (sec<60) return t('justNow');
  if (sec<3600) return t('minAgo')(Math.floor(sec/60));
  if (sec<86400) return t('hourAgo')(Math.floor(sec/3600));
  return t('dayAgo')(Math.floor(sec/86400));
}
function dateWd(d){ return `${t('dateFmt')(d)} ${t('wdFull')[d.getDay()]}`; }
function timeShort(d){ return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; }
function addedDate(s){
  if (s.addedDaysAgo==null) return '—';   // API contract carries no added-date
  const d = new Date(NOW); d.setDate(d.getDate()-s.addedDaysAgo);
  return t('dateFmt')(d);
}
function esc(x){return String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fileName(s, f){
  if (f && f._filename) return f._filename;   // real filename from API when present
  const d=f.dt;
  const p=n=>String(n).padStart(2,'0');
  return `${s.site}-${s.user}-${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.mp4`;
}
function tempName(s, tp){
  const d=tp.dt; const p=n=>String(n).padStart(2,'0');
  return `${s.site}-${s.user}-${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}00.mp4.part`;
}
function filesSize(s){return s.files.reduce((a,f)=>a+f.size,0);}
/* list-view aggregates: prefer API-provided totals (files aren't loaded until detail) */
function fileCount(s){return s._videoCount!=null ? s._videoCount : s.files.length;}
function sizeOf(s){return s._totalSize!=null ? s._totalSize : filesSize(s);}

/* ---------------- generated imagery ---------------- */
const thumbCache = {};
function genThumb(seed, w=352, h=198){
  if (thumbCache[seed]) return thumbCache[seed];
  const r = rng('thumb:'+seed);
  const c = document.createElement('canvas'); c.width=w; c.height=h;
  const g = c.getContext('2d');
  const hue = Math.floor(r()*360);
  const base = g.createLinearGradient(0,0,w,h);
  base.addColorStop(0, `hsl(${hue},22%,${8+r()*6}%)`);
  base.addColorStop(1, `hsl(${(hue+40)%360},26%,${13+r()*8}%)`);
  g.fillStyle=base; g.fillRect(0,0,w,h);
  // soft ambient blobs (dim room light feel)
  for (let i=0;i<4;i++){
    const x=r()*w, y=r()*h, rad=(0.25+r()*0.5)*w;
    const bl = g.createRadialGradient(x,y,0,x,y,rad);
    const bh = (hue + 300 + r()*120)%360;
    bl.addColorStop(0, `hsla(${bh},45%,${30+r()*25}%,${0.14+r()*0.2})`);
    bl.addColorStop(1, 'transparent');
    g.fillStyle=bl; g.beginPath(); g.arc(x,y,rad,0,7); g.fill();
  }
  // subject silhouette hint
  const sx = w*(0.3+r()*0.4), sy=h*(0.52+r()*0.2);
  const sil = g.createRadialGradient(sx,sy,0,sx,sy,h*0.55);
  sil.addColorStop(0,`hsla(${(hue+20)%360},30%,45%,0.28)`);
  sil.addColorStop(1,'transparent');
  g.fillStyle=sil; g.beginPath(); g.ellipse(sx,sy,w*0.2,h*0.42,0,0,7); g.fill();
  // scanline-ish noise
  g.globalAlpha=0.05;
  for(let y=0;y<h;y+=2){ g.fillStyle = r()>0.5?'#000':'#fff'; g.fillRect(0,y,w,1); }
  g.globalAlpha=1;
  // vignette
  const v = g.createRadialGradient(w/2,h/2,h*0.3,w/2,h/2,w*0.75);
  v.addColorStop(0,'transparent'); v.addColorStop(1,'rgba(0,0,0,0.5)');
  g.fillStyle=v; g.fillRect(0,0,w,h);
  const url = c.toDataURL('image/jpeg',0.75);
  thumbCache[seed]=url;
  return url;
}
const avatarCache = {};
function genAvatar(seed, name){
  const key = seed;
  if (avatarCache[key]) return avatarCache[key];
  const r = rng('av:'+seed);
  const c=document.createElement('canvas'); c.width=c.height=64;
  const g=c.getContext('2d');
  const hue=Math.floor(r()*360);
  const grad=g.createLinearGradient(0,0,64,64);
  grad.addColorStop(0,`hsl(${hue},38%,30%)`);
  grad.addColorStop(1,`hsl(${(hue+55)%360},42%,20%)`);
  g.fillStyle=grad; g.fillRect(0,0,64,64);
  const bl=g.createRadialGradient(20+r()*24,16+r()*20,2,32,32,44);
  bl.addColorStop(0,`hsla(${(hue+180)%360},50%,55%,.35)`); bl.addColorStop(1,'transparent');
  g.fillStyle=bl; g.fillRect(0,0,64,64);
  g.fillStyle='rgba(255,255,255,.92)';
  g.font='600 30px "IBM Plex Sans",sans-serif';
  g.textAlign='center'; g.textBaseline='middle';
  g.fillText(name[0].toUpperCase(),32,35);
  const url=c.toDataURL();
  avatarCache[key]=url;
  return url;
}

/* ---------------- svg icons ---------------- */
const IC = {
  grid:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  list:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
  gear:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  link:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  plus:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  search:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  play:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>',
  playLg:'<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>',
  pause:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  pauseCirc:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/></svg>',
  playCirc:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m10 8.5 5 3.5-5 3.5z"/></svg>',
  ext:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg>',
  trash:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  down:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>',
  back:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>',
  wrench:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
  alert:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  check:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  user:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/></svg>',
  volume:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
  full:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>',
  userMinus:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="8" r="4"/><path d="M1 21c0-4 3.6-6 8-6 1.5 0 2.9.23 4.1.66"/><path d="M17 11h6"/></svg>',
  mark:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 3h12v18l-6-4.2L6 21z"/></svg>',
  markFill:'<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M6 3h12v18l-6-4.2L6 21z"/></svg>',
  filmX:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="4" width="12" height="14" rx="2"/><path d="M2.5 8.5h3M2.5 13.5h3M11.5 8.5h3M11.5 13.5h3M8.5 4v14"/><path d="m15.5 15.5 5 5M20.5 15.5l-5 5"/></svg>',
};
