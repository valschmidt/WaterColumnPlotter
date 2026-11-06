'use strict';

// ---------------------------------------------------------------------------
// Color maps: 256-entry RGB lookup tables. LUT is the one in use.
// ---------------------------------------------------------------------------
function makeLUT(rgb) {
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const c = rgb(i / 255);
    for (let k = 0; k < 3; k++) lut[i * 3 + k] = Math.round(Math.max(0, Math.min(1, c[k])) * 255);
  }
  return lut;
}

const VIRIDIS = (() => {
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

const LUTS = {
  viridis: VIRIDIS,
  jet: makeLUT(t => [1.5 - Math.abs(4 * t - 3), 1.5 - Math.abs(4 * t - 2), 1.5 - Math.abs(4 * t - 1)]),
  grayscale: makeLUT(t => [t, t, t]),
};
let LUT = LUTS.viridis;

const SLICE_H_COLOR = 'rgba(255, 92, 220, 0.9)';   // horizontal slice band (depth)
const SLICE_V_COLOR = 'rgba(80, 220, 255, 0.9)';   // vertical slice band (across-track)

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------
const state = {
  settings: null,
  grid: null,          // slice bands as grid indices: {vertical: [c0, c1] columns, horizontal: [r0, r1] rows}
  pie: null,           // {data, rows, cols, row0, col0}
  columns: [],         // [{t, vOff, v, hOff, h, layers: [[id, top, bottom]]}], oldest first; offsets are grid indices
  paused: false,
  autoZoom: true,
  autoLevels: true,
  showGrid: true,
  tracking: 'thermocline',  // tracked features drawn on the vertical slice: 'off', 'layers' or 'thermocline'
  svp: null,           // {time, depth_m: [], speed_mps: []}, depths below the water surface
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

// Depth shown at the top of the pie and vertical slice: the water surface, or the heave allowance above the
// transducer's resting depth.
function topDepth() {
  const s = state.settings;
  if (!s) return 0;
  return s.referenceToSurface ? 0 : -s.heaveAllowance_m;
}

function depthLabel() {
  return state.settings && state.settings.referenceToSurface ? 'Depth below surface (m)' : 'Depth below transducer (m)';
}

function finiteSample(data, maxCount) {
  const step = Math.max(1, Math.floor(data.length / maxCount));
  const vals = [];
  for (let i = 0; i < data.length; i += step) if (Number.isFinite(data[i])) vals.push(data[i]);
  return vals;
}

// Value range for a histogram: the 0.2nd to 99.8th percentile of the data, padded by 5%.
function histogramRange(data) {
  const vals = finiteSample(data, 100000);
  if (vals.length < 2) return null;
  vals.sort((a, b) => a - b);
  const lo = vals[Math.floor(0.002 * (vals.length - 1))];
  const hi = vals[Math.ceil(0.998 * (vals.length - 1))];
  const pad = (hi - lo) * 0.05 || 1;
  return { lo: lo - pad, hi: hi + pad };
}

// Histogram of the finite values over a fixed range; values outside it count in the end bins.
function histogram(data, range, nbins = 100) {
  const counts = new Float32Array(nbins);
  const scale = nbins / (range.hi - range.lo);
  for (const v of finiteSample(data, 100000)) {
    counts[Math.max(0, Math.min(nbins - 1, Math.floor((v - range.lo) * scale)))]++;
  }
  return { lo: range.lo, hi: range.hi, counts, max: Math.max(1, ...counts) };
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

const HIST = { w: 50, gap: 4, barW: 12 };
const SIDE_W = 90;  // side strip width (px)  // histogram level editor: bars, gap, color bar (px)

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
    Object.assign(this, opts);  // xLabel, yLabel, pinTop, pinRight, equalAspect, describe, overlay
    this.sideW = this.side ? SIDE_W + 12 : 0;  // optional strip right of the plot (the SVP on the vertical slice)
    this.margin = { l: 58, r: 12 + this.sideW + HIST.w + HIST.gap + HIST.barW + 8, t: 8, b: 34 };
    this.hist = null;       // histogram of the image values, for the level editor
    this.histRangeFixed = null;  // value range of the histogram, fixed so it doesn't shift with each update
    this.histDrag = null;   // {which: 'lo' | 'hi' | 'both', range, start, levels} while dragging a level
    this.image = null;      // {w, h, data, x0, x1, y0, y1, extent: {x0, x1, y0, y1}}
    this.bitmap = document.createElement('canvas');
    this.levels = null;     // [lo, hi] dB
    this.view = null;       // {x0, x1, y0, y1} world coordinates shown

    new ResizeObserver(() => this.resize()).observe(this.canvas);
    this.loInput.addEventListener('change', () => this.setLevels([+this.loInput.value, this.levels[1]], true, true));
    this.hiInput.addEventListener('change', () => this.setLevels([this.levels[0], +this.hiInput.value], true, true));
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
    if (!image) { this.hist = null; this.draw(); return; }
    if (!this.histRangeFixed) this.histRangeFixed = histogramRange(image.data);
    this.hist = this.histRangeFixed && histogram(image.data, this.histRangeFixed);
    if (!this.levels) this.autoLevels(false);
    else if (state.autoLevels && !this.histDrag) this.autoLevels(false, true);
    this.colorize();
    if (state.autoZoom || !this.view) this.fit();
    this.draw();
  }

  // Set the color scale to the 10th to 90th percentile of the data. With smooth, move only part of the way there,
  // so continuous auto levels don't make the colors jump from one update to the next.
  autoLevels(redraw = true, smooth = false) {
    if (!this.image) return;
    const p = percentiles(this.image.data, 0.10, 0.90);
    if (!p) return;
    if (smooth && this.levels) {
      const k = 0.3;
      this.setLevels([this.levels[0] + k * (p[0] - this.levels[0]), this.levels[1] + k * (p[1] - this.levels[1])], redraw);
    } else {
      this.setLevels(p, redraw);
    }
  }

  // manual: set by the user (histogram or inputs), which turns off auto levels.
  setLevels(levels, redraw = true, manual = false) {
    if (!Number.isFinite(levels[0]) || !Number.isFinite(levels[1]) || levels[0] >= levels[1]) return;
    if (manual) setAutoLevels(false);
    this.levels = levels;
    // Keep the levels within the histogram's range, widening it if needed.
    const r = this.histRangeFixed;
    if (r && (levels[0] < r.lo || levels[1] > r.hi)) {
      const pad = (levels[1] - levels[0]) * 0.05;
      this.histRangeFixed = { lo: Math.min(r.lo, levels[0] - pad), hi: Math.max(r.hi, levels[1] + pad) };
      if (this.image) this.hist = histogram(this.image.data, this.histRangeFixed);
    }
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
    if (this.pinTop) v.y0 = topDepth();
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
    if (this.pinTop) { const dy = v.y1 - v.y0; v.y0 = topDepth(); v.y1 = v.y0 + dy; }
    if (this.pinRight && state.autoZoom) { const dx = v.x1 - v.x0; v.x1 = 0; v.x0 = -dx; }
    return v;
  }

  bindMouse() {
    const c = this.canvas;
    c.addEventListener('wheel', ev => {
      if (!this.view) return;
      ev.preventDefault();
      if (this.inHist(ev.offsetX, ev.offsetY)) return;
      setAutoZoom(false);
      const f = Math.exp(ev.deltaY * 0.0015);
      const [cx, cy0] = this.toWorld(ev.offsetX, ev.offsetY);
      const cy = this.pinTop ? topDepth() : cy0;
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
      c.setPointerCapture(ev.pointerId);
      if (this.inHist(ev.offsetX, ev.offsetY)) { this.startLevelDrag(ev.offsetY); return; }
      drag = { x: ev.offsetX, y: ev.offsetY, view: { ...this.view } };
    });
    c.addEventListener('pointermove', ev => {
      if (this.histDrag) { this.moveLevelDrag(ev.offsetY); return; }
      c.style.cursor = this.inHist(ev.offsetX, ev.offsetY) ? 'ns-resize' : 'crosshair';
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
    c.addEventListener('pointerup', () => {
      drag = null;
      if (this.histDrag) { this.histDrag = null; this.draw(); }
    });
    c.addEventListener('pointerleave', () => { this.readout.textContent = ''; });
    c.addEventListener('dblclick', ev => {
      if (this.inHist(ev.offsetX, ev.offsetY)) {
        // Reset this plot's histogram range and color scale to their defaults.
        if (this.image) this.histRangeFixed = histogramRange(this.image.data);
        if (this.histRangeFixed) this.hist = histogram(this.image.data, this.histRangeFixed);
        this.autoLevels();
      } else {
        setAutoZoom(true);
      }
    });
  }

  // ----- Histogram and color-level editor, to the right of the plot -----

  get histArea() {
    const p = this.plot;
    const x = p.x + p.w + 12 + this.sideW;
    return { x, y: p.y, w: HIST.w, h: p.h, barX: x + HIST.w + HIST.gap, barW: HIST.barW };
  }

  inHist(sx, sy) {
    const a = this.histArea;
    return this.hist && this.levels && sx >= a.x - 4 && sx <= a.barX + a.barW + 6 && sy >= a.y && sy <= a.y + a.h;
  }

  // Value range shown by the histogram.
  histRange() {
    return this.histDrag ? this.histDrag.range : this.histRangeFixed;
  }

  valueToY(v, range) {
    const a = this.histArea;
    return a.y + (range.hi - v) / (range.hi - range.lo) * a.h;
  }

  yToValue(y, range) {
    const a = this.histArea;
    return range.hi - (y - a.y) / a.h * (range.hi - range.lo);
  }

  startLevelDrag(sy) {
    const range = this.histRange();
    const [lo, hi] = this.levels;
    const ylo = this.valueToY(lo, range), yhi = this.valueToY(hi, range);
    let which;
    if (Math.abs(sy - yhi) <= 6) which = 'hi';
    else if (Math.abs(sy - ylo) <= 6) which = 'lo';
    else if (sy > yhi && sy < ylo) which = 'both';
    else which = Math.abs(sy - yhi) < Math.abs(sy - ylo) ? 'hi' : 'lo';
    this.histDrag = { which, range, start: this.yToValue(sy, range), levels: [lo, hi] };
    setAutoLevels(false);
    if (which !== 'both') this.moveLevelDrag(sy);
  }

  moveLevelDrag(sy) {
    const d = this.histDrag;
    const r = d.range;
    const v = Math.max(r.lo, Math.min(r.hi, this.yToValue(sy, r)));
    const minGap = (r.hi - r.lo) / 200;
    let [lo, hi] = d.levels;
    if (d.which === 'lo') lo = Math.min(v, hi - minGap);
    else if (d.which === 'hi') hi = Math.max(v, lo + minGap);
    else {
      // Move both, keeping the band inside the histogram's range.
      const shift = Math.max(r.lo - lo, Math.min(r.hi - hi, v - d.start));
      lo += shift;
      hi += shift;
    }
    this.pendingLevels = [lo, hi];
    if (!this.levelFrame) {
      this.levelFrame = requestAnimationFrame(() => {
        this.levelFrame = null;
        this.setLevels(this.pendingLevels, true, true);
      });
    }
  }

  drawHistogram() {
    if (!this.hist || !this.levels) return;
    const ctx = this.ctx, a = this.histArea, h = this.hist, range = this.histRange();
    const [lo, hi] = this.levels;

    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--plot-bg');
    ctx.fillRect(a.x, a.y, a.w, a.h);

    // Shade the selected range.
    const yhi = this.valueToY(hi, range), ylo = this.valueToY(lo, range);
    ctx.fillStyle = 'rgba(78, 161, 255, 0.15)';
    ctx.fillRect(a.x, yhi, a.w, ylo - yhi);

    // Bars grow leftward from the color bar; square-root scaling keeps small counts visible.
    const nb = h.counts.length, bw = (h.hi - h.lo) / nb;
    ctx.fillStyle = 'rgba(205, 210, 220, 0.6)';
    for (let i = 0; i < nb; i++) {
      if (!h.counts[i]) continue;
      const y0 = this.valueToY(h.lo + (i + 1) * bw, range), y1 = this.valueToY(h.lo + i * bw, range);
      const w = Math.sqrt(h.counts[i] / h.max) * a.w;
      ctx.fillRect(a.x + a.w - w, y0, w, Math.max(1, y1 - y0));
    }

    // Color bar: values below the minimum get the lowest color, above the maximum the highest.
    for (let y = 0; y < a.h; y += 2) {
      const t = (this.yToValue(a.y + y, range) - lo) / (hi - lo);
      const k = Math.max(0, Math.min(255, Math.round(t * 255))) * 3;
      ctx.fillStyle = `rgb(${LUT[k]},${LUT[k + 1]},${LUT[k + 2]})`;
      ctx.fillRect(a.barX, a.y + y, a.barW, 2);
    }

    // Level lines with their values.
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    for (const [v, y] of [[hi, yhi], [lo, ylo]]) {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(a.x, Math.round(y) - 1, a.barX + a.barW - a.x + 4, 2);
      ctx.beginPath();
      ctx.moveTo(a.barX + a.barW + 8, y - 4);
      ctx.lineTo(a.barX + a.barW + 3, y);
      ctx.lineTo(a.barX + a.barW + 8, y + 4);
      ctx.fill();
      ctx.fillStyle = '#e3e6eb';
      ctx.fillText(v.toFixed(0), a.x + 2, y - 2);
    }
    ctx.strokeStyle = '#4a505c';
    ctx.strokeRect(a.x + 0.5, a.y + 0.5, a.w - 1, a.h - 1);
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
    if (this.inHist(sx, sy)) {
      this.readout.textContent = 'Color scale: drag the lines or the band between them; double-click for auto';
      return;
    }
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
    if (state.showGrid) this.drawGrid();
    if (this.overlay) this.overlay(ctx, this);
    ctx.restore();
    this.drawAxes();
    if (this.side) this.side(ctx, this);
    this.drawHistogram();
  }

  ticks(axis) {
    const p = this.plot, v = this.view;
    const [a, b, px] = axis === 'x' ? [v.x0, v.x1, p.w / 80] : [v.y0, v.y1, p.h / 40];
    const step = niceStep(b - a, px), out = [];
    for (let t = Math.ceil(a / step) * step; t <= b; t += step) out.push(t);
    return { step, values: out };
  }

  drawGrid() {
    const ctx = this.ctx, p = this.plot;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const x of this.ticks('x').values) {
      const sx = Math.round(this.toScreen(x, 0)[0]) + 0.5;
      ctx.moveTo(sx, p.y); ctx.lineTo(sx, p.y + p.h);
    }
    for (const y of this.ticks('y').values) {
      const sy = Math.round(this.toScreen(0, y)[1]) + 0.5;
      ctx.moveTo(p.x, sy); ctx.lineTo(p.x + p.w, sy);
    }
    ctx.stroke();
  }

  drawAxes() {
    const ctx = this.ctx, p = this.plot, v = this.view;
    ctx.strokeStyle = '#4a505c';
    ctx.strokeRect(p.x + 0.5, p.y + 0.5, p.w - 1, p.h - 1);
    ctx.fillStyle = '#c9ced6';
    ctx.font = '11px system-ui, sans-serif';

    const { step: xs, values: xticks } = this.ticks('x');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const x of xticks) {
      const [sx] = this.toScreen(x, 0);
      ctx.fillRect(sx, p.y + p.h, 1, 4);
      ctx.fillText(fmt(x, xs), sx, p.y + p.h + 5);
    }
    ctx.fillText(this.xLabel, p.x + p.w / 2, p.y + p.h + 19);

    const { step: ys, values: yticks } = this.ticks('y');
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const y of yticks) {
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
// Grid row r covers depth [r, r + 1) * cell size; column c covers across-track [c, c + 1) * cell size.
function depthBand() {
  const b = state.settings.binSize_m, [r0, r1] = state.grid.horizontal;
  return [r0 * b, r1 * b];
}
function acrossBand() {
  const b = state.settings.binSize_m, [c0, c1] = state.grid.vertical;
  return [c0 * b, c1 * b];
}

function historyColumns() {
  const s = state.settings;
  return s ? Math.ceil(s.historyPings / s.alongTrackAvg_ping) : 1000;
}

function columnAt(x) {
  // Column under world x (pings, newest = 0) on the slice panels; each column spans alongTrackAvg_ping pings.
  const k = state.columns.length + Math.floor(x / state.settings.alongTrackAvg_ping);
  return state.columns[k];
}

const panels = {
  pie: new Panel('pie', {
    xLabel: 'Across-track (m)   port ← → starboard',
    yLabel: 'Depth below transducer (m)',
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
      return `${Math.round(-x)} pings ago${c ? ' (' + utc(c.t) + ' UTC)' : ''}, across ${y.toFixed(1)} m`;
    },
    overlay: (ctx, panel) => { if (state.grid) band(ctx, panel, 'y', ...acrossBand(), SLICE_V_COLOR); },
  }),
  vertical: new Panel('vertical', {
    xLabel: 'Pings (0 = newest)',
    yLabel: 'Depth below transducer (m)',
    pinTop: true,
    pinRight: true,
    describe: (x, y) => {
      const c = columnAt(x);
      return `${Math.round(-x)} pings ago${c ? ' (' + utc(c.t) + ' UTC)' : ''}, depth ${y.toFixed(1)} m`;
    },
    overlay: (ctx, panel) => {
      if (state.grid) band(ctx, panel, 'y', ...depthBand(), SLICE_H_COLOR);
      if (state.tracking === 'layers') drawLayers(ctx, panel);
      if (state.tracking === 'thermocline') drawThermocline(ctx, panel);
    },
    side: drawSVP,
  }),
};

// ---------------------------------------------------------------------------
// Experimental: tracked layers and the sound speed profile on the vertical slice
// ---------------------------------------------------------------------------
const LAYER_COLORS = ['#ff9f1c', '#2ec4b6', '#e71d36', '#ffffff', '#a463f2', '#9bf6ff'];
const LAYER_MIN_COLUMNS = 10;  // only draw layers seen in at least this many columns

// Outline each tracked layer: its top and bottom in every column it was found in.
function drawLayers(ctx, panel) {
  const cols = state.columns, N = cols.length, A = state.settings.alongTrackAvg_ping;
  const count = new Map();
  for (const c of cols) for (const [id] of c.layers) count.set(id, (count.get(id) || 0) + 1);
  ctx.lineWidth = 1.5;
  for (let k = 0; k < N; k++) {
    const [x0] = panel.toScreen((k - N) * A, 0), [x1] = panel.toScreen((k - N + 1) * A, 0);
    for (const [id, top, bottom] of cols[k].layers) {
      if (count.get(id) < LAYER_MIN_COLUMNS) continue;
      ctx.strokeStyle = LAYER_COLORS[id % LAYER_COLORS.length];
      const [, y0] = panel.toScreen(0, top), [, y1] = panel.toScreen(0, bottom);
      ctx.beginPath();
      ctx.moveTo(x0, y0); ctx.lineTo(x1, y0);
      ctx.moveTo(x0, y1); ctx.lineTo(x1, y1);
      ctx.stroke();
    }
  }
}

// SVP depths are below the surface; the shift to plot depths when depths are measured from the transducer.
function svpShift() {
  const s = state.settings;
  return s.referenceToSurface ? 0 : -(s.transducerDepth_m || 0);
}

const ALERT_OK = '#3ddc84', ALERT_BAD = '#ff4d4d', ALERT_NONE = '#c9ced6';
function alertColor(err) {
  if (err === null || err === undefined) return ALERT_NONE;
  return Math.abs(err) > state.settings.castAlertError_m ? ALERT_BAD : ALERT_OK;
}

// The latest column with a thermocline pick, or undefined.
function lastPick() {
  for (let k = state.columns.length - 1; k >= 0; k--) if (state.columns[k].thermo) return state.columns[k].thermo;
}

// The tracked thermocline bottom, one segment per column colored by the cast alert, and the depth the picks are
// compared with (dashed): the picks just after the cast, or the cast's thermocline bottom.
function drawThermocline(ctx, panel) {
  const cols = state.columns, N = cols.length, A = state.settings.alongTrackAvg_ping, p = panel.plot;
  const last = lastPick();
  const ref = last && last[1] !== null ? last[1]
    : state.svp && state.svp.thermocline_m ? state.svp.thermocline_m[1] + svpShift() : null;
  if (ref !== null) {
    const [, y] = panel.toScreen(0, ref);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(p.x, y); ctx.lineTo(p.x + p.w, y); ctx.stroke();
    ctx.setLineDash([]);
  }
  // A dark outline first, so the line stands out on any color map.
  for (const [width, color] of [[5, 'rgba(0, 0, 0, 0.8)'], [2.5, null]]) {
    ctx.lineWidth = width;
    for (let k = 0; k < N; k++) {
      const t = cols[k].thermo;
      if (!t) continue;
      const [x0, y] = panel.toScreen((k - N) * A, t[0]), [x1] = panel.toScreen((k - N + 1) * A, 0);
      ctx.strokeStyle = color || alertColor(t[2]);
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(Math.max(x1, x0 + 1), y); ctx.stroke();
    }
  }
}

// Sound speed profile in the strip right of the vertical slice, on the same depth scale.
function drawSVP(ctx, panel) {
  const p = panel.plot, x = p.x + p.w + 12, w = SIDE_W, v = panel.view;
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--plot-bg');
  ctx.fillRect(x, p.y, w, p.h);
  ctx.strokeStyle = '#4a505c';
  ctx.strokeRect(x + 0.5, p.y + 0.5, w - 1, p.h - 1);
  ctx.fillStyle = '#c9ced6';
  ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillText('SVP (m/s)', x + w / 2, p.y + p.h + 19);
  const svp = state.svp, s = state.settings;
  if (!svp || !s || !v) {
    ctx.fillStyle = '#9aa1ad';
    ctx.fillText('no SVP', x + w / 2, p.y + p.h / 2);
    return;
  }
  const shift = svpShift();
  const pts = svp.depth_m.map((d, i) => [d + shift, svp.speed_mps[i]]);
  const inView = pts.filter(([d]) => d >= v.y0 && d <= v.y1);
  if (!inView.length) return;
  let lo = Math.min(...inView.map(q => q[1])), hi = Math.max(...inView.map(q => q[1]));
  if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.1;
  lo -= pad; hi += pad;
  const sx = c => x + (c - lo) / (hi - lo) * w;
  const speedStep = niceStep(hi - lo, w / 30), speedTicks = [];
  for (let c = Math.ceil(lo / speedStep) * speedStep; c <= hi; c += speedStep) speedTicks.push(c);
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, p.y, w, p.h);
  ctx.clip();
  if (state.showGrid) {
    // Depth lines at the vertical slice's ticks, speed lines at round values.
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const d of panel.ticks('y').values) {
      const y = Math.round(panel.toScreen(0, d)[1]) + 0.5;
      ctx.moveTo(x, y); ctx.lineTo(x + w, y);
    }
    for (const c of speedTicks) {
      const gx = Math.round(sx(c)) + 0.5;
      ctx.moveTo(gx, p.y); ctx.lineTo(gx, p.y + p.h);
    }
    ctx.stroke();
  }
  // The cast's thermocline (gradient at least half its maximum), shaded, with its bottom marked.
  if (svp.thermocline_m) {
    const [, yt] = panel.toScreen(0, svp.thermocline_m[0] + shift), [, yb] = panel.toScreen(0, svp.thermocline_m[1] + shift);
    ctx.fillStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.fillRect(x, yt, w, yb - yt);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(x, yb); ctx.lineTo(x + w, yb); ctx.stroke();
    ctx.setLineDash([]);
  }
  // Velocline: the depth of the strongest sound speed gradient in view.
  let best = null;
  for (let i = 1; i < pts.length; i++) {
    const [d0, c0] = pts[i - 1], [d1, c1] = pts[i];
    if (d1 <= d0 || d0 < v.y0 || d1 > v.y1) continue;
    const g = Math.abs((c1 - c0) / (d1 - d0));
    if (!best || g > best.g) best = { g, d: (d0 + d1) / 2 };
  }
  if (best) {
    const [, y] = panel.toScreen(0, best.d);
    ctx.strokeStyle = 'rgba(255, 159, 28, 0.8)';
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + w, y); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = '#e3e6eb';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  pts.forEach(([d, c], i) => {
    const [, y] = panel.toScreen(0, d);
    if (i) ctx.lineTo(sx(c), y); else ctx.moveTo(sx(c), y);
  });
  ctx.stroke();
  ctx.restore();
  ctx.fillStyle = '#c9ced6';
  ctx.textBaseline = 'top';
  // Label the outermost speed lines.
  if (speedTicks.length) {
    const first = speedTicks[0], last = speedTicks[speedTicks.length - 1];
    ctx.textAlign = 'left';
    ctx.fillText(first.toFixed(0), Math.max(x, sx(first) - 12), p.y + p.h + 4);
    if (speedTicks.length > 1) {
      ctx.textAlign = 'right';
      ctx.fillText(last.toFixed(0), Math.min(x + w, sx(last) + 12), p.y + p.h + 4);
    }
  }
  // Latest thermocline pick: its shift from the cast and the resulting outer-beam depth error.
  const last = state.tracking === 'thermocline' && lastPick();
  if (last) {
    const [depth, ref, err] = last;
    ctx.textAlign = 'center';
    ctx.fillStyle = alertColor(err);
    if (ref === null) {
      ctx.fillText('calibrating', x + w / 2, p.y + 4);
    } else {
      const moved = depth - ref;
      ctx.fillText(`Δz ${moved >= 0 ? '+' : ''}${moved.toFixed(1)} m`, x + w / 2, p.y + 4);
      ctx.fillText(err === null ? 'err n/a' : `err ${err.toFixed(2)} m`, x + w / 2, p.y + 18);
      if (err !== null && Math.abs(err) > s.castAlertError_m) ctx.fillText('new cast?', x + w / 2, p.y + 32);
    }
  }
}

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
  const b = s.binSize_m;
  const x0 = p.col0 * b, y0 = p.row0 * b;
  const x1 = x0 + p.cols * b, y1 = y0 + p.rows * b;
  panels.pie.setImage({ w: p.cols, h: p.rows, data: p.data, x0, x1, y0, y1, extent: { x0, x1, y0, y1 } });
}

// Build a slice image: rows are grid indices covering every column's data, columns are pings (oldest at left).
// Returns null if no column has data.
function sliceImage(cols, offKey, valKey) {
  let lo = Infinity, hi = -Infinity;
  for (const c of cols) {
    if (!c[valKey].length) continue;
    lo = Math.min(lo, c[offKey]);
    hi = Math.max(hi, c[offKey] + c[valKey].length);
  }
  if (hi <= lo) return null;
  const N = cols.length, rows = hi - lo;
  const data = new Float32Array(rows * N).fill(NaN);
  for (let k = 0; k < N; k++) {
    const vals = cols[k][valKey], start = cols[k][offKey] - lo;
    for (let i = 0; i < vals.length; i++) data[(start + i) * N + k] = vals[i];
  }
  return { data, row0: lo, rows };
}

function updateSlices() {
  const s = state.settings, cols = state.columns, N = cols.length;
  if (!s || !N) { panels.vertical.setImage(null); panels.horizontal.setImage(null); return; }
  const b = s.binSize_m;
  const x0 = -N * s.alongTrackAvg_ping;  // x is in pings, newest = 0
  const span = -historyColumns() * s.alongTrackAvg_ping;  // auto-zoom shows the full history, filling from the right

  for (const [panel, offKey, valKey] of [[panels.vertical, 'vOff', 'v'], [panels.horizontal, 'hOff', 'h']]) {
    const im = sliceImage(cols, offKey, valKey);
    if (!im) { panel.setImage(null); continue; }
    const ext = dataExtentRows(im.data, N, im.rows) || [0, im.rows - 1];
    panel.setImage({
      w: N, h: im.rows, data: im.data, x0, x1: 0, y0: im.row0 * b, y1: (im.row0 + im.rows) * b,
      extent: { x0: span, x1: 0, y0: (im.row0 + ext[0]) * b, y1: (im.row0 + ext[1] + 1) * b },
    });
  }
}

function redrawAll() {
  updatePie();
  updateSlices();
}

function resetData() {
  state.pie = null;
  state.columns = [];
  for (const p of Object.values(panels)) {
    p.levels = null;
    p.histRangeFixed = null;
    p.view = null;
    p.image = null;
    p.draw();
  }
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

document.getElementById('grid').addEventListener('change', ev => {
  state.showGrid = ev.target.checked;
  for (const p of Object.values(panels)) p.draw();
});
document.getElementById('tracking').addEventListener('change', ev => {
  state.tracking = ev.target.value;
  panels.vertical.draw();
});
document.getElementById('colormap').addEventListener('change', ev => {
  LUT = LUTS[ev.target.value];
  for (const p of Object.values(panels)) if (p.image) { p.colorize(); p.draw(); }
});

const autoLevelsBox = document.getElementById('autolevels');
function setAutoLevels(on) {
  if (state.autoLevels === on) return;
  state.autoLevels = on;
  autoLevelsBox.checked = on;
  if (on) for (const p of Object.values(panels)) p.autoLevels();
}
autoLevelsBox.addEventListener('change', () => setAutoLevels(autoLevelsBox.checked));

const settingsForm = document.getElementById('settings');
const settingsToggle = document.getElementById('settings-toggle');
settingsToggle.addEventListener('click', () => {
  settingsForm.hidden = !settingsForm.hidden;
  settingsToggle.setAttribute('aria-expanded', String(!settingsForm.hidden));
});

function showSettings() {
  for (const input of settingsForm.querySelectorAll('input, select')) {
    if (document.activeElement === input) continue;
    if (input.type === 'checkbox') input.checked = state.settings[input.name];
    else input.value = state.settings[input.name];
  }
  const d = state.settings.transducerDepth_m;
  document.getElementById('transducer-depth').textContent = d === null
    ? 'Transducer depth not yet known (waiting for the installation datagram)'
    : `Transducer ${d.toFixed(2)} m below the surface`;
}


settingsForm.elements.sidelobeSuppression.addEventListener('change', ev => {
  send({ type: 'settings', settings: { sidelobeSuppression: ev.target.value } });
});

// The depth reference applies as soon as it is ticked, like the toolbar checkboxes.
settingsForm.elements.referenceToSurface.addEventListener('change', ev => {
  send({ type: 'settings', settings: { referenceToSurface: ev.target.checked } });
});

settingsForm.addEventListener('submit', ev => {
  ev.preventDefault();
  const requested = {};
  for (const input of settingsForm.querySelectorAll('input, select')) {
    if (input.type === 'checkbox') requested[input.name] = input.checked;
    else if (input.tagName === 'SELECT') requested[input.name] = input.value;
    else if (input.value !== '') requested[input.name] = Number(input.value);
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
  // Pings skipped since the last update because the cell size is too small for the swath.
  const tooLarge = lastStats ? (stats.grid_too_large ?? 0) - lastStats.tooLarge : 0;
  lastStats = { at: now, datagrams: stats.datagrams, tooLarge: stats.grid_too_large ?? 0 };
  statusEl.classList.toggle('error', tooLarge > 0);
  statusEl.textContent = (tooLarge > 0 ? 'Cell size too small for this swath: pings are being skipped · ' : '') +
    `${rate}received ${stats.datagrams ?? 0} · incomplete dropped ${stats.discarded ?? 0}` +
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
    // Payload: every column's vertical values, then every column's horizontal values.
    const { count, times, v, h } = header;
    const vals = [];
    let pos = 0;
    for (const [, len] of [...v, ...h]) { vals.push(payload.subarray(pos, pos + len)); pos += len; }
    for (let k = 0; k < count; k++) {
      state.columns.push({ t: times[k], vOff: v[k][0], v: vals[k], hOff: h[k][0], h: vals[count + k],
                           layers: header.layers[k], thermo: header.thermocline[k] });
    }
    const max = historyColumns();
    if (state.columns.length > max) state.columns.splice(0, state.columns.length - max);
    if (!state.paused) updateSlices();
  }
}

function handleText(msg) {
  if (msg.type === 'settings') {
    state.settings = msg.settings;
    state.grid = msg.grid;
    showSettings();
    panels.pie.yLabel = panels.vertical.yLabel = depthLabel();
    // The top depth may have changed (heave allowance, depth reference).
    for (const p of [panels.pie, panels.vertical]) {
      if (!p.view) continue;
      if (state.autoZoom) p.fit();
      else p.view = p.constrain({ ...p.view });
    }
    if (state.columns.length > historyColumns()) state.columns.splice(0, state.columns.length - historyColumns());
    for (const p of Object.values(panels)) p.draw();
  } else if (msg.type === 'reset') {
    resetData();
  } else if (msg.type === 'svp') {
    state.svp = msg;
    panels.vertical.draw();
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
