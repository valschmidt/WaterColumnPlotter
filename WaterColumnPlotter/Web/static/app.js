'use strict';

// ---------------------------------------------------------------------------
// Colour map: viridis (polynomial fit), 256-entry lookup table.
// ---------------------------------------------------------------------------
const LUT = (() => {
  const c = [
    [0.2777273272234177, 0.005407344544966578, 0.3340998053353061],
    [0.1050930431085774, 1.404613529898575, 1.384590162594685],
    [-0.3308618287255563, 0.214847559468213, 0.09509516302823659],
    [-4.634230498983486, -5.799100973351585, -19.33244095627987],
    [6.228269936347081, 14.17993336680509, 56.69055260068105],
    [4.776384997670288, -13.74514537774601, -65.35303263337234],
    [-5.435455855934631, 4.645852612178535, 26.3124352495832],
  ];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    for (let k = 0; k < 3; k++) {
      let v = c[6][k];
      for (let j = 5; j >= 0; j--) v = c[j][k] + t * v;
      lut[i * 3 + k] = Math.round(v * 255);
    }
  }
  return lut;
})();

const COLORBAR_CSS = 'linear-gradient(to right, ' +
  [0, 64, 128, 192, 255].map(i => `rgb(${LUT[i * 3]},${LUT[i * 3 + 1]},${LUT[i * 3 + 2]})`).join(', ') + ')';

const SLICE_H_COLOR = 'rgba(255, 92, 220, 0.9)';   // horizontal slice band (depth)
const SLICE_V_COLOR = 'rgba(80, 220, 255, 0.9)';   // vertical slice band (across-track)

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------
const state = {
  settings: null,
  grid: null,          // {vertical: [c0, c1], horizontal: [r0, r1], heaveRows}
  pie: null,           // {data, rows, cols, row0, col0}
  columns: [],         // [{t, v: Float32Array(n), h: Float32Array(n)}], oldest first
  paused: false,
  autoZoom: true,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function percentiles(data, lo, hi) {
  // Percentiles of the finite values, from a sample of at most ~50k values.
  const step = Math.max(1, Math.floor(data.length / 50000));
  const vals = [];
  for (let i = 0; i < data.length; i += step) if (Number.isFinite(data[i])) vals.push(data[i]);
  if (!vals.length) return null;
  vals.sort((a, b) => a - b);
  const at = p => vals[Math.min(vals.length - 1, Math.floor(p * (vals.length - 1)))];
  return [at(lo), at(hi)];
}

function niceStep(range, target) {
  const raw = range / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
}

function fmt(v, step) {
  const decimals = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  return v.toFixed(Math.min(decimals, 3));
}

function utc(t) {
  return new Date(t * 1000).toISOString().slice(11, 19);
}

// ---------------------------------------------------------------------------
// Panel: one plot. World coordinates have y increasing downward on screen.
// ---------------------------------------------------------------------------
class Panel {
  constructor(id, opts) {
    this.el = document.getElementById(id);
    this.canvas = this.el.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.readout = this.el.querySelector('.readout');
    this.loInput = this.el.querySelector('.lo');
    this.hiInput = this.el.querySelector('.hi');
    this.el.querySelector('.colorbar').style.background = COLORBAR_CSS;
    Object.assign(this, opts);  // xLabel, yLabel, pinTop, pinRight, equalAspect, describe, overlay
    this.margin = { l: 58, r: 12, t: 8, b: 34 };
    this.image = null;      // {w, h, data, x0, x1, y0, y1, extent: {x0, x1, y0, y1}}
    this.bitmap = document.createElement('canvas');
    this.levels = null;     // [lo, hi] dB
    this.view = null;       // {x0, x1, y0, y1} world coordinates shown

    new ResizeObserver(() => this.resize()).observe(this.canvas);
    this.loInput.addEventListener('change', () => this.setLevels([+this.loInput.value, this.levels[1]]));
    this.hiInput.addEventListener('change', () => this.setLevels([this.levels[0], +this.hiInput.value]));
    this.bindMouse();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    this.width = this.canvas.clientWidth;
    this.height = this.canvas.clientHeight;
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (state.autoZoom) this.fit();
    this.draw();
  }

  get plot() {
    const m = this.margin;
    return { x: m.l, y: m.t, w: Math.max(1, this.width - m.l - m.r), h: Math.max(1, this.height - m.t - m.b) };
  }

  toScreen(x, y) {
    const p = this.plot, v = this.view;
    return [p.x + (x - v.x0) / (v.x1 - v.x0) * p.w, p.y + (y - v.y0) / (v.y1 - v.y0) * p.h];
  }

  toWorld(sx, sy) {
    const p = this.plot, v = this.view;
    return [v.x0 + (sx - p.x) / p.w * (v.x1 - v.x0), v.y0 + (sy - p.y) / p.h * (v.y1 - v.y0)];
  }

  setImage(image) {
    this.image = image;
    if (!image) { this.draw(); return; }
    if (!this.levels) this.autoLevels(false);
    this.colorize();
    if (state.autoZoom || !this.view) this.fit();
    this.draw();
  }

  autoLevels(redraw = true) {
    if (!this.image) return;
    const p = percentiles(this.image.data, 0.10, 0.90);
    if (p) this.setLevels(p, redraw);
  }

  setLevels(levels, redraw = true) {
    if (!Number.isFinite(levels[0]) || !Number.isFinite(levels[1]) || levels[0] === levels[1]) return;
    this.levels = levels;
    this.loInput.value = levels[0].toFixed(1);
    this.hiInput.value = levels[1].toFixed(1);
    if (redraw && this.image) { this.colorize(); this.draw(); }
  }

  colorize() {
    const { w, h, data } = this.image;
    const [lo, hi] = this.levels;
    const scale = 255 / (hi - lo);
    this.bitmap.width = w;
    this.bitmap.height = h;
    const bctx = this.bitmap.getContext('2d');
    const img = bctx.createImageData(w, h);
    const px = img.data;
    for (let i = 0; i < data.length; i++) {
      const v = data[i];
      if (Number.isNaN(v)) continue;  // transparent: no data
      const k = Math.max(0, Math.min(255, Math.round((v - lo) * scale))) * 3;
      const j = i * 4;
      px[j] = LUT[k]; px[j + 1] = LUT[k + 1]; px[j + 2] = LUT[k + 2]; px[j + 3] = 255;
    }
    bctx.putImageData(img, 0, 0);
  }

  // Fit the view to the data, honouring the panel's constraints.
  fit() {
    if (!this.image || !this.width) return;
    const e = this.image.extent;
    let v = { x0: e.x0, x1: e.x1, y0: e.y0, y1: e.y1 };
    if (this.pinTop) v.y0 = 0;
    if (this.pinRight) v.x1 = 0;
    if (v.x1 <= v.x0) v.x1 = v.x0 + 1;
    if (v.y1 <= v.y0) v.y1 = v.y0 + 1;
    if (this.equalAspect) {
      const p = this.plot;
      const scale = Math.min(p.w / (v.x1 - v.x0), p.h / (v.y1 - v.y0));  // pixels per metre
      const cx = (v.x0 + v.x1) / 2;
      v.x0 = cx - p.w / scale / 2;
      v.x1 = cx + p.w / scale / 2;
      v.y1 = v.y0 + p.h / scale;
    }
    this.view = v;
  }

  constrain(v) {
    if (this.pinTop) { const dy = v.y1 - v.y0; v.y0 = 0; v.y1 = dy; }
    if (this.pinRight && state.autoZoom) { const dx = v.x1 - v.x0; v.x1 = 0; v.x0 = -dx; }
    return v;
  }

  bindMouse() {
    const c = this.canvas;
    c.addEventListener('wheel', ev => {
      if (!this.view) return;
      ev.preventDefault();
      setAutoZoom(false);
      const f = Math.exp(ev.deltaY * 0.0015);
      const [cx, cy0] = this.toWorld(ev.offsetX, ev.offsetY);
      const cy = this.pinTop ? 0 : cy0;
      const v = this.view;
      this.view = this.constrain({
        x0: cx + (v.x0 - cx) * f, x1: cx + (v.x1 - cx) * f,
        y0: cy + (v.y0 - cy) * f, y1: cy + (v.y1 - cy) * f,
      });
      this.draw();
    }, { passive: false });

    let drag = null;
    c.addEventListener('pointerdown', ev => {
      if (!this.view) return;
      drag = { x: ev.offsetX, y: ev.offsetY, view: { ...this.view } };
      c.setPointerCapture(ev.pointerId);
    });
    c.addEventListener('pointermove', ev => {
      if (drag) {
        const p = this.plot, v = drag.view;
        const dx = (ev.offsetX - drag.x) / p.w * (v.x1 - v.x0);
        const dy = this.pinTop ? 0 : (ev.offsetY - drag.y) / p.h * (v.y1 - v.y0);
        if (dx || dy) setAutoZoom(false);
        this.view = this.constrain({ x0: v.x0 - dx, x1: v.x1 - dx, y0: v.y0 - dy, y1: v.y1 - dy });
        this.draw();
      }
      this.updateReadout(ev.offsetX, ev.offsetY);
    });
    c.addEventListener('pointerup', () => { drag = null; });
    c.addEventListener('pointerleave', () => { this.readout.textContent = ''; });
    c.addEventListener('dblclick', () => setAutoZoom(true));
  }

  valueAt(x, y) {
    const im = this.image;
    if (!im) return NaN;
    const col = Math.floor((x - im.x0) / (im.x1 - im.x0) * im.w);
    const row = Math.floor((y - im.y0) / (im.y1 - im.y0) * im.h);
    if (col < 0 || col >= im.w || row < 0 || row >= im.h) return NaN;
    return im.data[row * im.w + col];
  }

  updateReadout(sx, sy) {
    if (!this.view) return;
    const p = this.plot;
    if (sx < p.x || sx > p.x + p.w || sy < p.y || sy > p.y + p.h) { this.readout.textContent = ''; return; }
    const [x, y] = this.toWorld(sx, sy);
    const v = this.valueAt(x, y);
    this.readout.textContent = this.describe(x, y) + (Number.isFinite(v) ? `   ${v.toFixed(1)} dB` : '');
  }

  draw() {
    const ctx = this.ctx, p = this.plot;
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--plot-bg');
    ctx.fillRect(p.x, p.y, p.w, p.h);
    if (!this.view) {
      ctx.fillStyle = '#9aa1ad';
      ctx.font = '13px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Waiting for data…', p.x + p.w / 2, p.y + p.h / 2);
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.rect(p.x, p.y, p.w, p.h);
    ctx.clip();
    if (this.image) {
      const im = this.image;
      const [sx0, sy0] = this.toScreen(im.x0, im.y0);
      const [sx1, sy1] = this.toScreen(im.x1, im.y1);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(this.bitmap, sx0, sy0, sx1 - sx0, sy1 - sy0);
    }
    if (this.overlay) this.overlay(ctx, this);
    ctx.restore();
    this.drawAxes();
  }

  drawAxes() {
    const ctx = this.ctx, p = this.plot, v = this.view;
    ctx.strokeStyle = '#4a505c';
    ctx.strokeRect(p.x + 0.5, p.y + 0.5, p.w - 1, p.h - 1);
    ctx.fillStyle = '#c9ced6';
    ctx.font = '11px system-ui, sans-serif';

    const xs = niceStep(v.x1 - v.x0, p.w / 80);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let x = Math.ceil(v.x0 / xs) * xs; x <= v.x1; x += xs) {
      const [sx] = this.toScreen(x, 0);
      ctx.fillRect(sx, p.y + p.h, 1, 4);
      ctx.fillText(fmt(x, xs), sx, p.y + p.h + 5);
    }
    ctx.fillText(this.xLabel, p.x + p.w / 2, p.y + p.h + 19);

    const ys = niceStep(v.y1 - v.y0, p.h / 40);
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let y = Math.ceil(v.y0 / ys) * ys; y <= v.y1; y += ys) {
      const [, sy] = this.toScreen(0, y);
      ctx.fillRect(p.x - 4, sy, 4, 1);
      ctx.fillText(fmt(y, ys), p.x - 6, sy);
    }
    ctx.save();
    ctx.translate(12, p.y + p.h / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText(this.yLabel, 0, 0);
    ctx.restore();
  }
}

// Draw a band between two world coordinates along one axis.
function band(ctx, panel, axis, a, b, color) {
  const v = panel.view;
  const [x0, y0] = panel.toScreen(axis === 'x' ? a : v.x0, axis === 'y' ? a : v.y0);
  const [x1, y1] = panel.toScreen(axis === 'x' ? b : v.x1, axis === 'y' ? b : v.y1);
  ctx.fillStyle = color.replace('0.9)', '0.12)');
  ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
  ctx.strokeStyle = color;
  ctx.setLineDash([4, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  if (axis === 'y') { ctx.moveTo(x0, y0); ctx.lineTo(x1, y0); ctx.moveTo(x0, y1); ctx.lineTo(x1, y1); }
  else { ctx.moveTo(x0, y0); ctx.lineTo(x0, y1); ctx.moveTo(x1, y0); ctx.lineTo(x1, y1); }
  ctx.stroke();
  ctx.setLineDash([]);
}

// World extents of the slice bands, from the grid indices the worker uses.
function depthBand() {
  const b = state.settings.binSize_m, h = state.grid.heaveRows, [r0, r1] = state.grid.horizontal;
  return [(r0 - h) * b, (r1 - h) * b];
}
function acrossBand() {
  const b = state.settings.binSize_m, n = state.settings.maxGridCells, [c0, c1] = state.grid.vertical;
  return [(c0 - Math.floor(n / 2)) * b, (c1 - Math.floor(n / 2)) * b];
}

function columnAt(x) {
  // Column under world x on the slice panels (newest column spans [-1, 0)).
  const k = state.columns.length + Math.floor(x);
  return state.columns[k];
}

const panels = {
  pie: new Panel('pie', {
    xLabel: 'Across-track (m)   port ← → starboard',
    yLabel: 'Depth (m)',
    pinTop: true,
    equalAspect: true,
    describe: (x, y) => `across ${x.toFixed(1)} m, depth ${y.toFixed(1)} m`,
    overlay: (ctx, panel) => {
      if (!state.grid) return;
      band(ctx, panel, 'y', ...depthBand(), SLICE_H_COLOR);
      band(ctx, panel, 'x', ...acrossBand(), SLICE_V_COLOR);
    },
  }),
  horizontal: new Panel('horizontal', {
    xLabel: 'Pings (0 = newest)',
    yLabel: 'Across-track (m)   port ↑',
    pinRight: true,
    describe: (x, y) => {
      const c = columnAt(x);
      return `${c ? utc(c.t) + ' UTC, ' : ''}across ${y.toFixed(1)} m`;
    },
    overlay: (ctx, panel) => { if (state.grid) band(ctx, panel, 'y', ...acrossBand(), SLICE_V_COLOR); },
  }),
  vertical: new Panel('vertical', {
    xLabel: 'Pings (0 = newest)',
    yLabel: 'Depth (m)',
    pinTop: true,
    pinRight: true,
    describe: (x, y) => {
      const c = columnAt(x);
      return `${c ? utc(c.t) + ' UTC, ' : ''}depth ${y.toFixed(1)} m`;
    },
    overlay: (ctx, panel) => { if (state.grid) band(ctx, panel, 'y', ...depthBand(), SLICE_H_COLOR); },
  }),
};

// ---------------------------------------------------------------------------
// Building panel images from received data
// ---------------------------------------------------------------------------
function dataExtentRows(data, w, h) {
  // First and last rows containing any finite value, or null.
  let first = -1, last = -1;
  for (let r = 0; r < h; r++) {
    const off = r * w;
    for (let k = 0; k < w; k++) {
      if (!Number.isNaN(data[off + k])) { if (first < 0) first = r; last = r; break; }
    }
  }
  return first < 0 ? null : [first, last];
}

function updatePie() {
  const p = state.pie, s = state.settings;
  if (!p || !s) { panels.pie.setImage(null); return; }
  const b = s.binSize_m, n = s.maxGridCells, h = state.grid.heaveRows;
  const x0 = (p.col0 - Math.floor(n / 2)) * b, y0 = (p.row0 - h) * b;
  const x1 = x0 + p.cols * b, y1 = y0 + p.rows * b;
  panels.pie.setImage({ w: p.cols, h: p.rows, data: p.data, x0, x1, y0, y1, extent: { x0, x1, y0, y1 } });
}

function updateSlices() {
  const s = state.settings, cols = state.columns, N = cols.length;
  if (!s || !N) { panels.vertical.setImage(null); panels.horizontal.setImage(null); return; }
  const n = s.maxGridCells, b = s.binSize_m, h = state.grid.heaveRows;

  // Images are rows (grid index) by columns (ping, oldest at left).
  const vdata = new Float32Array(n * N), hdata = new Float32Array(n * N);
  for (let k = 0; k < N; k++) {
    const v = cols[k].v, hz = cols[k].h;
    for (let r = 0; r < n; r++) { vdata[r * N + k] = v[r]; hdata[r * N + k] = hz[r]; }
  }

  const vRows = dataExtentRows(vdata, N, n) || [h, h];
  const vy0 = -h * b, vy1 = (n - h) * b;
  panels.vertical.setImage({
    w: N, h: n, data: vdata, x0: -N, x1: 0, y0: vy0, y1: vy1,
    extent: { x0: -N, x1: 0, y0: (vRows[0] - h) * b, y1: (vRows[1] + 1 - h) * b },
  });

  const hRows = dataExtentRows(hdata, N, n) || [n / 2, n / 2];
  const half = Math.floor(n / 2);
  panels.horizontal.setImage({
    w: N, h: n, data: hdata, x0: -N, x1: 0, y0: -half * b, y1: (n - half) * b,
    extent: { x0: -N, x1: 0, y0: (hRows[0] - half) * b, y1: (hRows[1] + 1 - half) * b },
  });
}

function redrawAll() {
  updatePie();
  updateSlices();
}

function resetData() {
  state.pie = null;
  state.columns = [];
  for (const p of Object.values(panels)) { p.levels = null; p.view = null; p.image = null; p.draw(); }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
const autoZoomBox = document.getElementById('autozoom');
function setAutoZoom(on) {
  if (state.autoZoom === on) return;
  state.autoZoom = on;
  autoZoomBox.checked = on;
  if (on) for (const p of Object.values(panels)) { p.fit(); p.draw(); }
}
autoZoomBox.addEventListener('change', () => setAutoZoom(autoZoomBox.checked));

const pauseButton = document.getElementById('pause');
pauseButton.addEventListener('click', () => {
  state.paused = !state.paused;
  pauseButton.textContent = state.paused ? 'Resume' : 'Pause';
  pauseButton.setAttribute('aria-pressed', String(state.paused));
  if (!state.paused) redrawAll();
});

document.getElementById('autolevels').addEventListener('click', () => {
  for (const p of Object.values(panels)) p.autoLevels();
});

const settingsForm = document.getElementById('settings');
const settingsToggle = document.getElementById('settings-toggle');
settingsToggle.addEventListener('click', () => {
  settingsForm.hidden = !settingsForm.hidden;
  settingsToggle.setAttribute('aria-expanded', String(!settingsForm.hidden));
});

function showSettings() {
  for (const input of settingsForm.querySelectorAll('input')) {
    if (document.activeElement !== input) input.value = state.settings[input.name];
  }
}

settingsForm.addEventListener('submit', ev => {
  ev.preventDefault();
  const requested = {};
  for (const input of settingsForm.querySelectorAll('input')) {
    if (input.value !== '') requested[input.name] = Number(input.value);
  }
  send({ type: 'settings', settings: requested });
  document.activeElement.blur();
});

// ---------------------------------------------------------------------------
// Status line
// ---------------------------------------------------------------------------
const statusEl = document.getElementById('status');
let lastStats = null;
function showStats(stats) {
  const now = performance.now();
  let rate = '';
  if (lastStats && stats.datagrams !== undefined) {
    const dt = (now - lastStats.at) / 1000;
    rate = `${((stats.datagrams - lastStats.datagrams) / dt).toFixed(1)} datagrams/s · `;
  }
  lastStats = { at: now, datagrams: stats.datagrams };
  statusEl.classList.remove('error');
  statusEl.textContent = `${rate}received ${stats.datagrams ?? 0} · incomplete dropped ${stats.discarded ?? 0}` +
    ` · processing ${(stats.process_ms ?? 0).toFixed(0)} ms/datagram` + (state.paused ? ' · paused' : '');
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
let ws = null;
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function handleBinary(buf) {
  const headerLen = new DataView(buf).getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headerLen)));
  const payload = new Float32Array(buf, 4 + headerLen);
  if (header.type === 'pie') {
    state.pie = { ...header, data: payload };
    if (!state.paused) updatePie();
  } else if (header.type === 'columns') {
    const { count, n, times } = header;
    for (let k = 0; k < count; k++) {
      state.columns.push({
        t: times[k],
        v: payload.subarray(k * n, (k + 1) * n),
        h: payload.subarray((count + k) * n, (count + k + 1) * n),
      });
    }
    const max = state.settings ? state.settings.historyColumns : 1000;
    if (state.columns.length > max) state.columns.splice(0, state.columns.length - max);
    if (!state.paused) updateSlices();
  }
}

function handleText(msg) {
  if (msg.type === 'settings') {
    state.settings = msg.settings;
    state.grid = msg.grid;
    showSettings();
    if (state.columns.length > state.settings.historyColumns) {
      state.columns.splice(0, state.columns.length - state.settings.historyColumns);
    }
    for (const p of Object.values(panels)) p.draw();
  } else if (msg.type === 'reset') {
    resetData();
  } else if (msg.type === 'stats') {
    showStats(msg.stats);
  }
}

function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { statusEl.classList.remove('error'); statusEl.textContent = 'Connected, waiting for data…'; };
  ws.onmessage = ev => {
    if (typeof ev.data === 'string') handleText(JSON.parse(ev.data));
    else handleBinary(ev.data);
  };
  ws.onclose = () => {
    statusEl.classList.add('error');
    statusEl.textContent = 'Disconnected from plotter, retrying…';
    resetData();
    setTimeout(connect, 2000);
  };
}

connect();
