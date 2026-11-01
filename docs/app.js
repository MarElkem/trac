/* Match Sync Viewer — static, no build step.
 *
 * Clock model: the master clock T is "seconds since kickoff of the current half", i.e. tracking time.
 *   video.currentTime = T + videoOffset[half]
 *   event time        = event.t/1000 + eventOffset[half]
 * When a video is playable it drives T; otherwise a performance.now() clock does, so the tactical
 * view and events keep working without video.
 *
 * URL params: ?data=<base url of data folder>  ?match=<id>  ?v1=<url> ?v2=<url> (override videos)
 *             ?h=<half> ?t=<seconds>
 */
(() => {
  'use strict';

  const LABEL_ORDER = ['Pass', 'Drive', 'Header', 'High Pass', 'Out', 'Cross', 'Throw In', 'Shot',
    'Ball Player Block', 'Player Successful Tackle', 'Free Kick', 'Goal'];
  const LABEL_COLOR = {
    'Pass': '#4e9af1', 'Drive': '#f2a541', 'Header': '#b58df0', 'High Pass': '#3fc1c9', 'Out': '#8b93a0',
    'Cross': '#e5655c', 'Throw In': '#9ccc5a', 'Shot': '#ff7eb3', 'Ball Player Block': '#a98467',
    'Player Successful Tackle': '#2fbf71', 'Free Kick': '#e08f62', 'Goal': '#ffd84a',
  };
  const FRAME = 0.04;            // one source frame (25 fps)
  const TRAIL_S = 2;             // trail length in seconds
  const HOLD_FRAMES = 5;         // keep a track on screen this many tracking frames after it vanishes

  const $ = (id) => document.getElementById(id);
  const el = {
    stage: $('stage'), video: $('video'), vpane: $('vpane'), vmsg: $('vmsg'), pitch: $('pitch'),
    bannerV: $('bannerV'), bannerT: $('bannerT'), trkInfo: $('trkInfo'), matchSel: $('matchSel'),
    badge: $('badge'), playBtn: $('playBtn'), prevEv: $('prevEv'), nextEv: $('nextEv'),
    timeLbl: $('timeLbl'), frameLbl: $('frameLbl'), speedSel: $('speedSel'), trailChk: $('trailChk'),
    timeline: $('timeline'), chips: $('chips'), evList: $('evList'), evCount: $('evCount'),
    teamSeg: $('teamSeg'), visChk: $('visChk'), clipChk: $('clipChk'), preIn: $('preIn'), postIn: $('postIn'),
    vidOff: $('vidOff'), vidOffOut: $('vidOffOut'), evOff: $('evOff'), evOffOut: $('evOffOut'),
    calMsg: $('calMsg'), attrib: $('attrib'), viewSeg: $('viewSeg'), halfSeg: $('halfSeg'),
  };

  const S = {
    base: 'data', index: null, match: null, half: 1, view: 'split', playing: false, T: 0, speed: 1,
    trk: {}, vidOff: { 1: 0, 2: 0 }, evOff: { 1: 0, 2: 0 }, vidOverride: {},
    usingVideo: false, videoReady: false,
    labels: new Set(), team: 'both', visOnly: false, clip: false, pre: 3, post: 3, trails: false,
    events: [], fe: [], curEv: -1, loadToken: 0, lastTick: 0,
  };

  // ---------------------------------------------------------------- helpers
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const colorOf = (label) => LABEL_COLOR[label] || '#9aa5a0';
  const resolveUrl = (u) => new URL(u, document.baseURI).href;
  const fmt = (t) => {
    const sign = t < 0 ? '-' : '';
    t = Math.abs(t);
    const m = Math.floor(t / 60), s = t - m * 60;
    return `${sign}${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
  };
  const fmtShort = (t) => `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  const lowerBound = (arr, x, key) => { // first index with key(arr[i]) >= x
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (key(arr[mid]) < x) lo = mid + 1; else hi = mid; }
    return lo;
  };
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };
  const calKey = () => `sts:cal:${S.match && S.match.id}`;
  const halfInfo = (h = S.half) => S.match.halves.find((x) => x.half === h);
  const dur = () => {
    const hi = halfInfo();
    let d = hi ? hi.durationS : 0;
    if (S.usingVideo && isFinite(el.video.duration)) d = Math.min(d || Infinity, el.video.duration - S.vidOff[S.half]);
    return d || 0;
  };
  const evTime = (e) => e.t / 1000 + S.evOff[e.half];

  // ---------------------------------------------------------------- loading
  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  async function init() {
    const q = new URLSearchParams(location.search);
    S.base = (q.get('data') || 'data').replace(/\/$/, '');
    if (q.get('v1')) S.vidOverride[1] = [q.get('v1')];
    if (q.get('v2')) S.vidOverride[2] = [q.get('v2')];
    bindUI();
    try {
      S.index = await getJSON(`${S.base}/index.json`);
    } catch (err) {
      showFatal(`Could not load ${S.base}/index.json (${err.message}). Serve this folder over http(s); opening index.html as a file will not work.`);
      return;
    }
    el.matchSel.innerHTML = '';
    for (const m of S.index.matches) {
      const o = document.createElement('option');
      o.value = m.id; o.textContent = m.title || m.id;
      el.matchSel.appendChild(o);
    }
    const want = q.get('match') || S.index.matches[0].id;
    el.matchSel.value = S.index.matches.some((m) => m.id === want) ? want : S.index.matches[0].id;
    await loadMatch(el.matchSel.value, { half: Number(q.get('h')) || 1, t: Number(q.get('t')) || 0 });
    requestAnimationFrame(loop);
  }

  function showFatal(msg) {
    el.vmsg.hidden = false; el.vmsg.textContent = msg;
  }

  async function loadMatch(id, start = { half: 1, t: 0 }) {
    const entry = S.index.matches.find((m) => m.id === id);
    const url = `${S.base}/${entry.path}`;
    S.match = await getJSON(url);
    S.trk = {};
    const saved = store.get(calKey()) || {};
    for (const h of S.match.halves) {
      S.vidOff[h.half] = saved.vid && saved.vid[h.half] != null ? saved.vid[h.half] : (h.videoOffset || 0);
      S.evOff[h.half] = saved.ev && saved.ev[h.half] != null ? saved.ev[h.half] : (h.eventOffsetMs || 0) / 1000;
    }
    S.events = S.match.events.slice();
    el.badge.hidden = !S.match.synthetic;
    el.attrib.textContent = S.match.attribution || '';
    const present = new Set(S.events.map((e) => e.label));
    const labels = [...LABEL_ORDER.filter((l) => present.has(l)), ...[...present].filter((l) => !LABEL_ORDER.includes(l))];
    S.labels = new Set(labels);
    S.allLabels = labels;
    buildChips();
    buildHalfButtons();
    await setHalf(start.half, start.t, false);
    document.title = `${S.match.title} · Match Sync Viewer`;
  }

  function buildHalfButtons() {
    const avail = new Set(S.match.halves.map((h) => h.half));
    for (const b of el.halfSeg.querySelectorAll('button')) b.hidden = !avail.has(Number(b.dataset.half));
  }

  async function loadTracking(h) {
    if (S.trk[h]) return S.trk[h];
    const hi = halfInfo(h);
    const t = await getJSON(`${S.base}/${S.match.id}/${hi.tracking}`);
    t.cache = new Map();
    S.trk[h] = t;
    return t;
  }

  async function setHalf(h, t = 0, autoplay = S.playing) {
    if (!halfInfo(h)) return;
    const token = ++S.loadToken;
    const wasPlaying = autoplay;
    pause();
    S.usingVideo = false;   // the old half's video must not drive the clock while the new half loads
    S.T = 0;
    S.half = h;
    for (const b of el.halfSeg.querySelectorAll('button')) b.classList.toggle('on', Number(b.dataset.half) === h);
    syncCalUI();
    try { await loadTracking(h); } catch (err) { el.trkInfo.textContent = 'tracking failed to load'; console.error(err); }
    if (token !== S.loadToken) return;
    const trk = S.trk[h];
    if (trk) el.trkInfo.textContent = `${trk.fps} fps · ${Object.keys(trk.tracks).length} tracks`;
    await loadVideo(h, token);
    if (token !== S.loadToken) return;
    refilter();
    seek(t);
    if (wasPlaying) play();
  }

  function pickSource(list) {
    const probe = document.createElement('video');
    for (const u of list) {
      const ext = u.split('?')[0].split('.').pop().toLowerCase();
      const mime = { mp4: 'video/mp4', m4v: 'video/mp4', webm: 'video/webm', ogv: 'video/ogg', mov: 'video/mp4' }[ext];
      if (!mime || probe.canPlayType(mime)) return u;
    }
    return null;
  }

  function loadVideo(h, token) {
    return new Promise((resolve) => {
      const v = el.video;
      v.pause();
      S.usingVideo = false; S.videoReady = false;
      el.vmsg.hidden = true;
      const list = (S.vidOverride[h] || halfInfo(h).video || []).map(resolveUrl);
      const src = pickSource(list);
      if (!src) {
        v.removeAttribute('src'); v.load();
        el.vmsg.hidden = false;
        el.vmsg.textContent = list.length
          ? 'This browser cannot play the available video formats. Showing tracking only.'
          : 'No video configured for this half. Showing tracking only.';
        return resolve();
      }
      const done = () => { cleanup(); resolve(); };
      const onMeta = () => {
        if (token !== S.loadToken) return done();
        S.usingVideo = true; S.videoReady = true;
        if (v.videoWidth) el.vpane.style.setProperty('--vr', String(v.videoWidth / v.videoHeight));
        done();
      };
      const onErr = () => {
        S.usingVideo = false;
        el.vmsg.hidden = false;
        el.vmsg.textContent = `Video failed to load (${src}). Check the URL, CORS and range-request support. Showing tracking only.`;
        done();
      };
      const cleanup = () => { v.removeEventListener('loadedmetadata', onMeta); v.removeEventListener('error', onErr); clearTimeout(timer); };
      const timer = setTimeout(onErr, 20000);
      v.addEventListener('loadedmetadata', onMeta);
      v.addEventListener('error', onErr);
      v.src = src;
      v.load();
    });
  }

  // ---------------------------------------------------------------- transport
  function play() {
    if (!S.match) return;
    if (dur() && S.T >= dur() - 0.05) seek(0);
    S.playing = true; S.lastTick = performance.now();
    if (S.usingVideo) { el.video.playbackRate = S.speed; el.video.play().catch(() => { /* needs a user gesture */ }); }
    el.playBtn.textContent = '❚❚ Pause';
  }
  function pause() {
    S.playing = false;
    if (S.usingVideo) el.video.pause();
    el.playBtn.textContent = '▶ Play';
  }
  function seek(t) {
    const d = dur();
    S.T = clamp(t, 0, d || t);
    if (S.usingVideo) el.video.currentTime = Math.max(0, S.T + S.vidOff[S.half]);
    S.curEv = -1;
  }
  function nextEvent(dir) {
    const fe = S.fe; if (!fe.length) return;
    const eps = 0.15;
    if (dir > 0) {
      const i = lowerBound(fe, S.T + eps, evTime);
      if (i < fe.length) seek(evTime(fe[i]) - S.pre);
    } else {
      // previous: the last event strictly before the one whose pre-roll we are inside
      const i = lowerBound(fe, S.T + S.pre - eps, evTime) - 1;
      if (i >= 0) seek(evTime(fe[i]) - S.pre);
    }
  }
  function seekToEvent(e) { seek(evTime(e) - S.pre); play(); }

  // ---------------------------------------------------------------- filters
  function refilter() {
    S.fe = S.events.filter((e) => e.half === S.half && S.labels.has(e.label)
      && (S.team === 'both' || e.team === S.team) && (!S.visOnly || e.vis === 'visible'))
      .sort((a, b) => evTime(a) - evTime(b));
    renderList(); updateChipCounts(); drawTimeline();
  }

  function buildChips() {
    el.chips.innerHTML = '';
    for (const l of S.allLabels) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'chip on'; b.dataset.label = l; b.style.setProperty('--c', colorOf(l));
      b.innerHTML = `<span class="dot"></span><span class="nm"></span><small></small>`;
      b.querySelector('.nm').textContent = l;
      b.title = 'Click to toggle · Alt/Option-click to show only this';
      b.addEventListener('click', (ev) => {
        if (ev.altKey) { S.labels = new Set([l]); } else if (S.labels.has(l)) S.labels.delete(l); else S.labels.add(l);
        syncChips(); refilter();
      });
      el.chips.appendChild(b);
    }
  }
  function syncChips() {
    for (const b of el.chips.children) b.classList.toggle('on', S.labels.has(b.dataset.label));
  }
  function updateChipCounts() {
    const counts = {};
    for (const e of S.events) {
      if (e.half !== S.half) continue;
      if (S.team !== 'both' && e.team !== S.team) continue;
      if (S.visOnly && e.vis !== 'visible') continue;
      counts[e.label] = (counts[e.label] || 0) + 1;
    }
    for (const b of el.chips.children) b.querySelector('small').textContent = counts[b.dataset.label] || 0;
  }

  function renderList() {
    el.evList.innerHTML = '';
    el.evCount.textContent = `${S.fe.length} in this half`;
    if (!S.fe.length) {
      el.evList.innerHTML = '<div class="empty">No events match the current filters.</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    S.fe.forEach((e, i) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ev'; b.dataset.i = i; b.setAttribute('role', 'listitem');
      b.style.setProperty('--c', colorOf(e.label));
      const side = e.team ? `<span class="side-chip ${e.team}">${e.team === 'left' ? 'L' : 'R'}</span>` : '<span></span>';
      b.innerHTML = `<span class="dot"></span><span class="tm">${fmtShort(Math.max(0, evTime(e)))}</span><span class="lb"></span>${side}`;
      b.querySelector('.lb').textContent = e.label;
      frag.appendChild(b);
    });
    el.evList.appendChild(frag);
  }

  // ---------------------------------------------------------------- tracking sampling
  function posAt(trk, i) { // Map(track_id -> [x_m, y_m]) at tracking frame i with short hold for dropouts
    const hit = trk.cache.get(i);
    if (hit) return hit;
    const m = new Map();
    for (let back = 0; back <= HOLD_FRAMES && i - back >= 0; back++) {
      const row = trk.frames[i - back];
      if (!row) continue;
      for (let k = 0; k < row.length; k += 3) {
        const id = row[k];
        if (!m.has(id)) m.set(id, [row[k + 1] / 10, row[k + 2] / 10]);
      }
    }
    if (trk.cache.size > 400) trk.cache.clear();
    trk.cache.set(i, m);
    return m;
  }
  function sample(trk, T) {
    const f = clamp(T * trk.fps, 0, trk.n - 1);
    const i = Math.floor(f), a = f - i;
    const A = posAt(trk, i), B = i + 1 < trk.n ? posAt(trk, i + 1) : A;
    const out = [];
    for (const [id, p] of A) {
      const q = B.get(id);
      out.push([id, q ? p[0] + (q[0] - p[0]) * a : p[0], q ? p[1] + (q[1] - p[1]) * a : p[1]]);
    }
    return out;
  }

  // ---------------------------------------------------------------- drawing
  function fitCanvas(c) {
    const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    return { w, h, dpr };
  }

  function drawPitch() {
    if (el.stage.dataset.view === 'video') return;
    const c = el.pitch, ctx = c.getContext('2d');
    const { w, h, dpr } = fitCanvas(c);
    const [PL, PW] = S.match.pitch;
    const pad = 14 * dpr;
    const sc = Math.min((w - 2 * pad) / PL, (h - 2 * pad) / PW);
    const ox = w / 2, oy = h / 2;
    const X = (x) => ox + x * sc, Y = (y) => oy - y * sc;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#1f5a3a'; ctx.fillRect(0, 0, w, h);
    const stripes = 12, sw = (PL * sc) / stripes;
    for (let i = 0; i < stripes; i += 2) { ctx.fillStyle = 'rgba(255,255,255,.035)'; ctx.fillRect(X(-PL / 2) + i * sw, Y(PW / 2), sw, PW * sc); }
    ctx.strokeStyle = 'rgba(255,255,255,.8)'; ctx.lineWidth = Math.max(1, 1.5 * dpr);
    const rect = (x0, y0, x1, y1) => ctx.strokeRect(X(x0), Y(y1), (x1 - x0) * sc, (y1 - y0) * sc);
    rect(-PL / 2, -PW / 2, PL / 2, PW / 2);
    ctx.beginPath(); ctx.moveTo(X(0), Y(-PW / 2)); ctx.lineTo(X(0), Y(PW / 2)); ctx.stroke();
    ctx.beginPath(); ctx.arc(X(0), Y(0), 9.15 * sc, 0, Math.PI * 2); ctx.stroke();
    for (const s of [-1, 1]) {
      rect(s > 0 ? PL / 2 - 16.5 : -PL / 2, -20.16, s > 0 ? PL / 2 : -PL / 2 + 16.5, 20.16);
      rect(s > 0 ? PL / 2 - 5.5 : -PL / 2, -9.16, s > 0 ? PL / 2 : -PL / 2 + 5.5, 9.16);
      rect(s > 0 ? PL / 2 : -PL / 2 - 2, -3.66, s > 0 ? PL / 2 + 2 : -PL / 2, 3.66);
      const sx = s * (PL / 2 - 11);
      ctx.beginPath(); ctx.arc(X(sx), Y(0), 0.35 * sc, 0, Math.PI * 2); ctx.fillStyle = 'rgba(255,255,255,.8)'; ctx.fill();
      const a = Math.acos(5.5 / 9.15);
      ctx.beginPath();
      if (s > 0) ctx.arc(X(sx), Y(0), 9.15 * sc, Math.PI - a, Math.PI + a); else ctx.arc(X(sx), Y(0), 9.15 * sc, -a, a);
      ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(X(0), Y(0), 0.35 * sc, 0, Math.PI * 2); ctx.fillStyle = 'rgba(255,255,255,.8)'; ctx.fill();

    const trk = S.trk[S.half];
    if (!trk) return;
    const T = S.T;
    if (T < 0 || T > trk.n / trk.fps + 1) { // outside tracking coverage
      ctx.fillStyle = 'rgba(0,0,0,.45)'; ctx.fillRect(0, 0, w, h);
      return;
    }
    const pts = sample(trk, T);
    if (S.trails) {
      ctx.lineWidth = Math.max(1, 1.5 * dpr); ctx.lineCap = 'round';
      const steps = Math.round(TRAIL_S * trk.fps);
      const f0 = clamp(T * trk.fps, 0, trk.n - 1);
      const paths = new Map();
      for (let k = steps; k >= 0; k--) {
        const f = f0 - k; if (f < 0) continue;
        const i = Math.floor(f), a = f - i;
        const A = posAt(trk, i), B = i + 1 < trk.n ? posAt(trk, i + 1) : A;
        for (const [id, p] of A) {
          const q = B.get(id) || p;
          if (!paths.has(id)) paths.set(id, []);
          paths.get(id).push([p[0] + (q[0] - p[0]) * a, p[1] + (q[1] - p[1]) * a]);
        }
      }
      for (const [id, path] of paths) {
        const meta = trk.tracks[id]; if (!meta || meta[1] === 'referee') continue;
        ctx.strokeStyle = meta[0] === 'left' ? 'rgba(79,147,255,.45)' : 'rgba(255,106,92,.45)';
        ctx.beginPath(); path.forEach(([x, y], n) => (n ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)))); ctx.stroke();
      }
    }
    const rpx = clamp(0.9 * sc, 5 * dpr, 13 * dpr);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.font = `700 ${Math.round(rpx * 1.05)}px system-ui, sans-serif`;
    for (const [id, x, y] of pts) {
      const meta = trk.tracks[id] || [null, 'other', null];
      const isRef = meta[1] === 'referee' || meta[1] === 'other';
      ctx.beginPath(); ctx.arc(X(x), Y(y), isRef ? rpx * 0.75 : rpx, 0, Math.PI * 2);
      ctx.fillStyle = isRef ? '#ffd84a' : meta[0] === 'left' ? '#4f93ff' : meta[0] === 'right' ? '#ff6a5c' : '#bbb';
      ctx.fill();
      ctx.lineWidth = meta[1] === 'goalkeeper' ? 2.4 * dpr : 1.2 * dpr;
      ctx.strokeStyle = meta[1] === 'goalkeeper' ? '#fff' : 'rgba(0,0,0,.55)'; ctx.stroke();
      if (!isRef && meta[2] != null && rpx >= 7 * dpr) { ctx.fillStyle = '#04101f'; ctx.fillText(String(meta[2]), X(x), Y(y) + 0.5 * dpr); }
    }
  }

  function drawTimeline() {
    const c = el.timeline, ctx = c.getContext('2d');
    const { w, h, dpr } = fitCanvas(c);
    ctx.clearRect(0, 0, w, h);
    const d = dur() || (halfInfo() ? halfInfo().durationS : 1) || 1;
    const pad = 12 * dpr, tx = (t) => pad + clamp(t / d, 0, 1) * (w - 2 * pad);
    ctx.fillStyle = '#26332d'; ctx.fillRect(pad, h - 18 * dpr, w - 2 * pad, 4 * dpr);
    // minute grid
    ctx.fillStyle = '#93a39a'; ctx.font = `${10 * dpr}px system-ui, sans-serif`; ctx.textAlign = 'center';
    const stepMin = d > 1800 ? 5 : d > 600 ? 2 : d > 120 ? 1 : 0.25;
    for (let m = 0; m * 60 <= d; m += stepMin) {
      const x = tx(m * 60); ctx.fillRect(x, h - 14 * dpr, 1, 5 * dpr);
      if (stepMin >= 1 || m % 0.5 === 0) ctx.fillText(stepMin < 1 ? `${m * 60}s` : `${m}'`, x, h - 2 * dpr);
    }
    // events
    ctx.lineWidth = Math.max(2, 2 * dpr);
    for (const e of S.fe) {
      const x = tx(evTime(e));
      ctx.strokeStyle = colorOf(e.label);
      ctx.beginPath(); ctx.moveTo(x, 6 * dpr); ctx.lineTo(x, h - 22 * dpr); ctx.stroke();
    }
    // playhead
    const px = tx(S.T);
    ctx.fillStyle = '#fff'; ctx.fillRect(px - 1 * dpr, 2 * dpr, 2 * dpr, h - 12 * dpr);
    ctx.beginPath(); ctx.arc(px, h - 16 * dpr, 5 * dpr, 0, Math.PI * 2); ctx.fillStyle = '#5fd08b'; ctx.fill();
    ctx.lineWidth = 2 * dpr; ctx.strokeStyle = '#0e1411'; ctx.stroke();
  }

  function updateBanner() {
    // latest filtered event whose time is within [t, t+2.5s) of the playhead
    const fe = S.fe;
    let i = lowerBound(fe, S.T - 2.5, evTime); // first event with time >= T-2.5
    let pick = -1;
    for (; i < fe.length && evTime(fe[i]) <= S.T + 0.02; i++) pick = i;
    if (pick >= 0 && S.T - evTime(fe[pick]) >= 2.5) pick = -1;
    if (pick !== S.curEv) {
      S.curEv = pick;
      for (const b of el.evList.querySelectorAll('.ev.cur')) b.classList.remove('cur');
      if (pick >= 0) {
        const row = el.evList.querySelector(`.ev[data-i="${pick}"]`);
        if (row) { row.classList.add('cur'); if (S.playing) row.scrollIntoView({ block: 'nearest' }); }
      }
    }
    for (const b of [el.bannerV, el.bannerT]) {
      if (pick < 0) { b.hidden = true; continue; }
      const e = fe[pick];
      b.hidden = false;
      b.style.borderLeftColor = colorOf(e.label);
      b.textContent = `${e.label}${e.team ? ' · ' + e.team : ''}${e.player ? ' · ' + e.player : ''}`;
    }
  }

  // ---------------------------------------------------------------- main loop
  function loop(now) {
    const dt = Math.min(0.25, (now - S.lastTick) / 1000); S.lastTick = now;
    if (S.match) {
      if (S.usingVideo) {
        S.T = el.video.currentTime - S.vidOff[S.half];
        if (S.playing && el.video.ended) pause();
      } else if (S.playing) {
        S.T += dt * S.speed;
      }
      if (S.playing) {
        const d = dur();
        if (d && S.T >= d) { S.T = d; pause(); }
        if (S.clip) clipStep();
      }
      el.timeLbl.textContent = `${S.half === 1 ? '1H' : '2H'} ${fmt(S.T)}`;
      el.frameLbl.textContent = `frame ${Math.max(0, Math.round(S.T * 25))}`;
      updateBanner();
      drawPitch();
      drawTimeline();
    }
    requestAnimationFrame(loop);
  }

  // Clip mode: stateless rule — find the first event whose window has not ended; if we are before its window, jump to it.
  function clipStep() {
    const fe = S.fe;
    if (!fe.length) { pause(); return; }
    const i = lowerBound(fe, S.T - S.post, evTime); // first event with t >= T - post (window still open)
    if (i >= fe.length) { pause(); return; }
    const start = evTime(fe[i]) - S.pre;
    if (S.T < start - 0.05) seek(start);
  }

  // ---------------------------------------------------------------- calibration UI
  function syncCalUI() {
    el.vidOff.value = S.vidOff[S.half]; el.evOff.value = S.evOff[S.half];
    el.vidOffOut.textContent = `${S.vidOff[S.half].toFixed(2)} s`;
    el.evOffOut.textContent = `${S.evOff[S.half].toFixed(2)} s`;
  }
  function saveCal() { store.set(calKey(), { vid: S.vidOff, ev: S.evOff }); }
  function setVidOff(v) {
    v = Math.round(v * 100) / 100;
    const T = S.T; S.vidOff[S.half] = v; syncCalUI(); saveCal();
    if (S.usingVideo) el.video.currentTime = Math.max(0, T + v);   // keep tracking time fixed, move the video
  }
  function setEvOff(v) {
    S.evOff[S.half] = Math.round(v * 100) / 100; syncCalUI(); saveCal(); refilter();
  }

  // ---------------------------------------------------------------- UI wiring
  function setView(v) {
    S.view = v; el.stage.dataset.view = v;
    for (const b of el.viewSeg.querySelectorAll('button')) b.classList.toggle('on', b.dataset.view === v);
  }

  function bindUI() {
    el.viewSeg.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setView(b.dataset.view); });
    el.halfSeg.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setHalf(Number(b.dataset.half), 0, false); });
    el.teamSeg.addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      S.team = b.dataset.team;
      for (const x of el.teamSeg.querySelectorAll('button')) x.classList.toggle('on', x === b);
      refilter();
    });
    el.matchSel.addEventListener('change', () => { pause(); loadMatch(el.matchSel.value); });
    el.playBtn.addEventListener('click', () => (S.playing ? pause() : play()));
    el.prevEv.addEventListener('click', () => nextEvent(-1));
    el.nextEv.addEventListener('click', () => nextEvent(1));
    el.speedSel.addEventListener('change', () => { S.speed = Number(el.speedSel.value); el.video.playbackRate = S.speed; });
    el.trailChk.addEventListener('change', () => { S.trails = el.trailChk.checked; });
    el.visChk.addEventListener('change', () => { S.visOnly = el.visChk.checked; refilter(); });
    el.clipChk.addEventListener('change', () => { S.clip = el.clipChk.checked; if (S.clip && S.playing) clipStep(); });
    el.preIn.addEventListener('change', () => { S.pre = clamp(Number(el.preIn.value) || 0, 0, 30); });
    el.postIn.addEventListener('change', () => { S.post = clamp(Number(el.postIn.value) || 0, 0, 30); });
    $('fAll').addEventListener('click', () => { S.labels = new Set(S.allLabels); syncChips(); refilter(); });
    $('fNone').addEventListener('click', () => { S.labels = new Set(); syncChips(); refilter(); });
    el.evList.addEventListener('click', (e) => {
      const b = e.target.closest('.ev'); if (!b) return;
      seekToEvent(S.fe[Number(b.dataset.i)]);
    });
    el.vidOff.addEventListener('input', () => setVidOff(Number(el.vidOff.value)));
    el.evOff.addEventListener('input', () => setEvOff(Number(el.evOff.value)));
    document.querySelectorAll('[data-nudge]').forEach((b) => b.addEventListener('click', () => {
      const d = Number(b.dataset.d);
      if (b.dataset.nudge === 'vid') setVidOff(S.vidOff[S.half] + d); else setEvOff(S.evOff[S.half] + d);
    }));
    $('calReset').addEventListener('click', () => {
      const hi = halfInfo();
      setVidOff(hi.videoOffset || 0); setEvOff((hi.eventOffsetMs || 0) / 1000);
    });
    $('calCopy').addEventListener('click', async () => {
      const out = {
        videoOffset: Object.fromEntries(S.match.halves.map((h) => [h.half, S.vidOff[h.half]])),
        eventOffsetMs: Object.fromEntries(S.match.halves.map((h) => [h.half, Math.round(S.evOff[h.half] * 1000)])),
      };
      const text = JSON.stringify(out);
      try { await navigator.clipboard.writeText(text); el.calMsg.textContent = 'Copied. Bake these into match.json.'; }
      catch { el.calMsg.textContent = text; }
    });

    // timeline scrub (pointer capture so dragging outside still works)
    const scrub = (ev) => {
      const r = el.timeline.getBoundingClientRect(), pad = 12;
      const f = clamp((ev.clientX - r.left - pad) / (r.width - 2 * pad), 0, 1);
      seek(f * (dur() || 0));
    };
    let dragging = false;
    el.timeline.addEventListener('pointerdown', (ev) => { dragging = true; el.timeline.setPointerCapture(ev.pointerId); scrub(ev); });
    el.timeline.addEventListener('pointermove', (ev) => { if (dragging) scrub(ev); });
    el.timeline.addEventListener('pointerup', () => { dragging = false; });
    el.timeline.addEventListener('pointercancel', () => { dragging = false; });

    window.addEventListener('keydown', (e) => {
      if (e.target.matches('input[type=number], select, textarea')) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key;
      if (k === ' ' && !e.target.matches('button, summary, input')) { e.preventDefault(); S.playing ? pause() : play(); }
      else if (k === 'ArrowRight' || k === 'ArrowLeft') {
        if (e.target.matches('input[type=range]')) return;
        e.preventDefault(); seek(S.T + (k === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? FRAME : 5));
      } else if (k === 'j' || k === 'J') nextEvent(1);
      else if (k === 'k' || k === 'K') nextEvent(-1);
      else if (k === 'v' || k === 'V') setView({ split: 'video', video: 'tactical', tactical: 'split' }[S.view]);
    });
    window.addEventListener('resize', () => { drawPitch(); drawTimeline(); });
  }

  // Test / debugging hook (also handy from the console).
  window.__sts = {
    S, seek, play, pause, setView, setHalf, setVidOff, setEvOff, refilter, nextEvent,
    get T() { return S.T; },
    get filtered() { return S.fe; },
  };

  init().catch((err) => { console.error(err); showFatal(String(err.message || err)); });
})();
