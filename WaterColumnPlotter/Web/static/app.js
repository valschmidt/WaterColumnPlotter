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
  hMode: 'amplitude',  // horizontal slice: 'amplitude', or the amplitude-weighted 'depth' in its band ('shaded': hillshaded)
  // Where the slices are, this viewer's own (dragged on the plots): the horizontal slice's depth band [top, bottom]
  // and the vertical slice's across-track band [port, starboard], in metres (starboard positive).
  slices: null,        // set below, from storage
  pieSeq: null,        // number of the newest swath the pie shows; null: live, the newest received
  combine: 'mean',     // how data columns or rows sharing a screen pixel are combined: 'mean', 'max' or 'decimate'
  tracks: [],          // tracking results [{seq0, seq1, t, layers: [[id, top, bottom]], thermo}], oldest first
  autoLevels: true,
  showGrid: true,
  tracking: 'thermocline',  // tracked features drawn on the vertical slice: 'off', 'layers' or 'thermocline'
  svp: null,           // {time, depth_m: [], speed_mps: []}, depths below the water surface
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const SLICES_KEY = 'wcp.slices';
const DEFAULT_SLICES = { depth: [9, 11], across: [-5, 5] };
function loadSlices() {
  try {
    const s = JSON.parse(localStorage.getItem(SLICES_KEY));
    if (s && s.depth?.length === 2 && s.across?.length === 2 && [...s.depth, ...s.across].every(Number.isFinite)) return s;
  } catch (e) { /* no storage: defaults */ }
  return structuredClone(DEFAULT_SLICES);
}
function saveSlices() {
  try { localStorage.setItem(SLICES_KEY, JSON.stringify(state.slices)); } catch (e) { /* not kept */ }
}
state.slices = loadSlices();

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

// Combine an image's columns kx at a time and rows ky at a time: by the mean or max of the values present (NaN where
// none), or by decimation, keeping only the value nearest the middle of each group (as the browser does when it draws
// an image smaller than it is; at the image's edges a group may lack its middle). Groups are counted from the image's grid indices (ci0, ri0: of its first column and row), so they stay put
// as the image grows or scrolls. Returns an image with the same fields; the image itself if nothing is combined.
function combineImage(im, kx, ky, how) {
  if (kx === 1 && ky === 1) return im;
  const mod = (a, n) => ((a % n) + n) % n;
  const offx = mod(im.ci0 ?? 0, kx), offy = mod(im.ri0 ?? 0, ky);
  const W = Math.ceil((im.w + offx) / kx), H = Math.ceil((im.h + offy) / ky);
  const cw = (im.x1 - im.x0) / im.w, ch = (im.y1 - im.y0) / im.h;
  const col = Int32Array.from({ length: im.w }, (_, c) => Math.floor((c + offx) / kx));
  const acc = new Float32Array(W * H).fill(how === 'max' ? -Infinity : 0), n = new Uint32Array(W * H);
  const shade = im.shade ? new Float32Array(W * H) : null;
  const decimate = how === 'decimate', midx = (kx - 1) / 2, midy = (ky - 1) / 2;
  const best = decimate ? new Float32Array(W * H).fill(Infinity) : null;  // distance from the middle of the value kept
  for (let r = 0; r < im.h; r++) {
    const row = Math.floor((r + offy) / ky) * W, dy = Math.abs((r + offy) % ky - midy);
    for (let c = 0, i = r * im.w; c < im.w; c++, i++) {
      const v = im.data[i];
      if (Number.isNaN(v)) continue;
      const k = row + col[c];
      if (decimate) {
        const d = dy + Math.abs((c + offx) % kx - midx);
        if (d < best[k]) { best[k] = d; acc[k] = v; if (shade) shade[k] = im.shade[i]; }
        n[k] = 1;
        continue;
      }
      if (how === 'max') { if (v > acc[k]) acc[k] = v; } else acc[k] += v;
      n[k]++;
      if (shade) shade[k] += im.shade[i];
    }
  }
  const data = new Float32Array(W * H);
  for (let k = 0; k < data.length; k++) {
    data[k] = n[k] ? (how === 'mean' ? acc[k] / n[k] : acc[k]) : NaN;
    if (shade && n[k] && !decimate) shade[k] /= n[k];
  }
  const x0 = im.x0 - offx * cw, y0 = im.y0 - offy * ch;
  return { w: W, h: H, data, shade, x0, x1: x0 + W * kx * cw, y0, y1: y0 + H * ky * ch };
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
    // xLabel, yLabel, pinTop (used by fit), equalAspect, describe, overlay, and handles: a function returning the
    // indicators that can be dragged on the plot (see hitHandle).
    Object.assign(this, opts);
    this.sideW = this.side ? SIDE_W + 12 : 0;  // optional strip right of the plot (the SVP on the vertical slice)
    this.margin = { l: 58, r: 12 + this.sideW + HIST.w + HIST.gap + HIST.barW + 8, t: 8, b: 34 };
    this.hist = null;       // histogram of the image values, for the level editor
    this.histRangeFixed = null;  // value range of the histogram, fixed so it doesn't shift with each update
    this.histDrag = null;   // {which: 'lo' | 'hi' | 'both', range, start, levels} while dragging a level
    this.image = null;      // {w, h, data, x0, x1, y0, y1, extent: {x0, x1, y0, y1}}
    this.bitmap = document.createElement('canvas');
    // The image as colored and drawn: combined to fit the screen (combineImage), {kx, ky, how, x0, x1, y0, y1}.
    this.shown = null;
    this.decimationEl = Object.assign(document.createElement('span'), { className: 'decimation', hidden: true });
    this.el.append(this.decimationEl);
    this.levels = null;     // [lo, hi] in the plot's unit
    this.unitLabel = this.el.querySelector('.unit-label');
    this.setUnit('dB');
    this.view = null;       // {x0, x1, y0, y1} world coordinates shown
    this.autoZoom = true;   // keep the view fitted to the data as it arrives
    this.autoZoomButton = this.el.querySelector('.autozoom');
    this.autoZoomButton.addEventListener('click', () => this.setAutoZoom(!this.autoZoom));

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
    if (this.autoZoom) this.fit();
    this.draw();
  }

  // The unit of the plotted values (dB, or m for depths), for the color scale and readout.
  setUnit(unit) {
    this.unit = unit;
    this.reversed = unit === 'm';  // depths: deeper (larger) values get the low end of the color map
    this.unitLabel.textContent = unit;
    this.loInput.setAttribute('aria-label', `Color scale minimum (${unit})`);
    this.hiInput.setAttribute('aria-label', `Color scale maximum (${unit})`);
  }

  // Forget the color scale and histogram range, for values on a different scale.
  resetLevels() {
    this.levels = null;
    this.histRangeFixed = null;
    this.hist = null;
  }

  setAutoZoom(on) {
    if (this.autoZoom === on) return;
    this.autoZoom = on;
    this.autoZoomButton.setAttribute('aria-pressed', String(on));
    if (on) { this.fit(); this.draw(); }
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
    if (!this.histRangeFixed) this.histRangeFixed = this.defaultRange(image.data);
    this.hist = this.histRangeFixed && histogram(image.data, this.histRangeFixed);
    if (!this.levels) this.autoLevels(false);
    else if (state.autoLevels && !this.histDrag) this.autoLevels(false, true);
    // Too few values for percentiles (an image that is still almost empty): span the histogram's range.
    if (!this.levels && this.histRangeFixed) this.setLevels([this.histRangeFixed.lo, this.histRangeFixed.hi], false);
    if (this.levels) this.colorize();
    if (this.autoZoom || !this.view) this.fit();
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

  // The histogram's value range: the only values possible, if the panel knows them (valueRange), else the data's.
  defaultRange(data) {
    return (this.valueRange && this.valueRange()) || histogramRange(data);
  }

  // manual: set by the user (histogram or inputs), which turns off auto levels.
  setLevels(levels, redraw = true, manual = false) {
    const fixed = this.valueRange && this.valueRange();
    if (fixed) {
      // Nothing lies outside it; if the data does (from before the range changed), span the whole range.
      levels = [Math.max(levels[0], fixed.lo), Math.min(levels[1], fixed.hi)];
      if (!(levels[0] < levels[1])) levels = [fixed.lo, fixed.hi];
    }
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
    const decimals = this.unit === 'm' ? 2 : 1;
    this.loInput.value = levels[0].toFixed(decimals);
    this.hiInput.value = levels[1].toFixed(decimals);
    if (redraw && this.image) { this.colorize(); this.draw(); }
  }

  // Image columns and rows per screen (device) pixel at the current zoom, rounded: how many to combine (1: none).
  combineFactors() {
    const im = this.image, v = this.view, p = this.plot, dpr = window.devicePixelRatio || 1;
    if (!im || !v) return [1, 1];
    const cx = im.w / (im.x1 - im.x0) * (v.x1 - v.x0) / (p.w * dpr);
    const cy = im.h / (im.y1 - im.y0) * (v.y1 - v.y0) / (p.h * dpr);
    return [Math.max(1, Math.round(cx)), Math.max(1, Math.round(cy))];
  }

  // Color the image into the bitmap drawn, combined to fit the screen at the current zoom.
  colorize() {
    const [kx, ky] = this.combineFactors();
    // Depths: the maximum isn't meaningful, so they are averaged instead.
    const how = this.unit !== 'dB' && state.combine === 'max' ? 'mean' : state.combine;
    const im = combineImage(this.image, kx, ky, how);
    this.shown = { kx, ky, how, x0: im.x0, x1: im.x1, y0: im.y0, y1: im.y1 };
    const { w, h, data, shade } = im;
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
      const t = Math.max(0, Math.min(255, Math.round((v - lo) * scale)));
      const k = (this.reversed ? 255 - t : t) * 3;
      const j = i * 4;
      const f = shade ? shade[i] : 1;  // hillshading: 1 lit, smaller in shadow
      px[j] = LUT[k] * f; px[j + 1] = LUT[k + 1] * f; px[j + 2] = LUT[k + 2] * f; px[j + 3] = 255;
    }
    bctx.putImageData(img, 0, 0);
  }

  // Fit the view to the data: from the top depth down on the pie and vertical slice, newest ping at the right edge.
  fit() {
    if (!this.image || !this.width) return;
    const e = this.image.extent;
    let v = { x0: e.x0, x1: e.x1, y0: e.y0, y1: e.y1 };
    if (this.pinTop) v.y0 = topDepth();
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

  bindMouse() {
    const c = this.canvas;
    c.addEventListener('wheel', ev => {
      if (!this.view) return;
      ev.preventDefault();
      if (this.inHist(ev.offsetX, ev.offsetY)) return;
      this.setAutoZoom(false);
      const f = Math.exp(ev.deltaY * 0.0015);
      const [cx, cy] = this.toWorld(ev.offsetX, ev.offsetY);
      const v = this.view;
      this.view = {
        x0: cx + (v.x0 - cx) * f, x1: cx + (v.x1 - cx) * f,
        y0: cy + (v.y0 - cy) * f, y1: cy + (v.y1 - cy) * f,
      };
      this.draw();
    }, { passive: false });

    let drag = null, handleDrag = null;
    c.addEventListener('pointerdown', ev => {
      if (!this.view) return;
      c.setPointerCapture(ev.pointerId);
      if (this.inHist(ev.offsetX, ev.offsetY)) { this.startLevelDrag(ev.offsetY); return; }
      const hit = this.hitHandle(ev.offsetX, ev.offsetY);
      if (hit) {
        const [wx, wy] = this.toWorld(ev.offsetX, ev.offsetY), h = hit.handle;
        handleDrag = { ...hit, start: h.axis === 'x' ? wx : wy, lo: h.lo, hi: h.hi, at: h.at };
        return;
      }
      drag = { x: ev.offsetX, y: ev.offsetY, view: { ...this.view } };
    });
    c.addEventListener('pointermove', ev => {
      if (this.histDrag) { this.moveLevelDrag(ev.offsetY); return; }
      if (handleDrag) {
        const [wx, wy] = this.toWorld(ev.offsetX, ev.offsetY), d = handleDrag, h = d.handle;
        const delta = (h.axis === 'x' ? wx : wy) - d.start;
        if (h.kind === 'line') h.set(d.at + delta);
        else if (d.part === 'move') h.set(d.lo + delta, d.hi + delta);
        else {
          // An edge changes the width about the band's centre, which stays put.
          const centre = (d.lo + d.hi) / 2;
          const half = Math.max(0, d.part === 'lo' ? centre - (d.lo + delta) : d.hi + delta - centre);
          h.set(centre - half, centre + half);
        }
        this.updateReadout(ev.offsetX, ev.offsetY);
        return;
      }
      const hover = !drag && this.hitHandle(ev.offsetX, ev.offsetY);
      c.style.cursor = this.inHist(ev.offsetX, ev.offsetY) ? 'ns-resize'
        : hover ? (hover.handle.axis === 'x' ? 'ew-resize' : 'ns-resize') : 'crosshair';
      if (drag) {
        const p = this.plot, v = drag.view;
        const dx = (ev.offsetX - drag.x) / p.w * (v.x1 - v.x0);
        const dy = (ev.offsetY - drag.y) / p.h * (v.y1 - v.y0);
        if (dx || dy) this.setAutoZoom(false);
        this.view = { x0: v.x0 - dx, x1: v.x1 - dx, y0: v.y0 - dy, y1: v.y1 - dy };
        this.draw();
      }
      this.updateReadout(ev.offsetX, ev.offsetY);
    });
    c.addEventListener('pointerup', () => {
      if (handleDrag && handleDrag.handle.done) handleDrag.handle.done();
      drag = null;
      handleDrag = null;
      if (this.histDrag) { this.histDrag = null; this.draw(); }
    });
    c.addEventListener('pointerleave', () => { this.readout.textContent = ''; });
    c.addEventListener('dblclick', ev => {
      if (this.inHist(ev.offsetX, ev.offsetY)) {
        // Reset this plot's histogram range and color scale to their defaults.
        if (this.image) this.histRangeFixed = this.defaultRange(this.image.data);
        if (this.histRangeFixed) this.hist = histogram(this.image.data, this.histRangeFixed);
        this.autoLevels();
      } else {
        this.setAutoZoom(true);
      }
    });
  }

  // ----- Draggable indicators -----

  // The indicator under a screen point, if any: {handle, part: 'lo' | 'hi' | 'move'}. Handles are
  // {axis: 'x' | 'y', kind: 'band', lo, hi, set(lo, hi)} (a band between two world coordinates, dragged by either
  // edge to widen or narrow it about its centre, or by its centre line to move it) or {axis, kind: 'line', at, set(at)}; done() is called after a drag.
  hitHandle(sx, sy) {
    if (!this.handles || !this.view) return null;
    const p = this.plot;
    if (sx < p.x || sx > p.x + p.w || sy < p.y || sy > p.y + p.h) return null;
    for (const h of this.handles()) {
      const pos = v => (h.axis === 'x' ? this.toScreen(v, 0)[0] : this.toScreen(0, v)[1]);
      const s = h.axis === 'x' ? sx : sy;
      if (h.kind === 'line') {
        if (Math.abs(s - pos(h.at)) <= 6) return { handle: h, part: 'move' };
        continue;
      }
      const a = pos(h.lo), b = pos(h.hi), mid = (a + b) / 2, width = Math.abs(b - a);
      const edge = Math.abs(s - a) <= Math.abs(s - b) ? 'lo' : 'hi', toEdge = Math.min(Math.abs(s - a), Math.abs(s - b));
      if (width >= 16) {  // wide enough to grab an edge, or the centre line between them
        if (toEdge <= 5) return { handle: h, part: edge };
        if (Math.abs(s - mid) <= 5) return { handle: h, part: 'move' };
      } else {  // narrow: inside moves it, just outside an edge resizes it
        if (Math.abs(s - mid) <= Math.max(3, width / 2)) return { handle: h, part: 'move' };
        if (toEdge <= 6) return { handle: h, part: edge };
      }
    }
    return null;
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
      const u = Math.max(0, Math.min(255, Math.round(t * 255)));
      const k = (this.reversed ? 255 - u : u) * 3;
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
      ctx.fillText(v.toFixed(this.unit === 'm' ? 1 : 0), a.x + 2, y - 2);
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
    const hit = this.hitHandle(sx, sy);
    if (hit && hit.handle.label) { this.readout.textContent = hit.handle.label(hit.part); return; }
    const [x, y] = this.toWorld(sx, sy);
    const v = this.valueAt(x, y);
    this.readout.textContent = this.describe(x, y) + (Number.isFinite(v) ? `   ${v.toFixed(this.unit === 'm' ? 2 : 1)} ${this.unit}` : '');
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
      ctx.fillText(source.opening ? `Opening ${source.opening}…` : 'Waiting for data…', p.x + p.w / 2, p.y + p.h / 2);
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.rect(p.x, p.y, p.w, p.h);
    ctx.clip();
    if (this.image) {
      // Zooming can change how many columns or rows share a pixel: recombine from the full image.
      const [kx, ky] = this.combineFactors(), sh = this.shown;
      if (this.levels && (!sh || sh.kx !== kx || sh.ky !== ky)) this.colorize();
      const im = this.shown || this.image;
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
    if (source.opening) this.drawNotice(`Opening ${source.opening}…`);
    this.showDecimation();
  }

  // 1:N in yellow at the plot's bottom right while N columns (or rows) of data share each screen pixel.
  showDecimation() {
    const sh = this.image && this.view && this.shown, el = this.decimationEl;
    if (!sh || (sh.kx === 1 && sh.ky === 1)) { el.hidden = true; return; }
    el.textContent = sh.kx > 1 && sh.ky > 1 ? `1:${sh.kx} × 1:${sh.ky}` : `1:${Math.max(sh.kx, sh.ky)}`;
    const what = [sh.kx > 1 && `${sh.kx} columns`, sh.ky > 1 && `${sh.ky} rows`].filter(Boolean).join(' and ');
    const how = { mean: 'averaged', max: 'the strongest shown', decimate: 'one shown, the rest skipped' }[sh.how];
    el.title = `1 px = ${what} at this zoom level\n(${how}: Settings > Display > Combine)`;
    const p = this.plot;
    el.style.left = `${this.canvas.offsetLeft + p.x + p.w - 3}px`;
    el.style.top = `${this.canvas.offsetTop + p.y + p.h - 3}px`;
    el.hidden = false;
  }

  // A message over the plot, on a dark label so it reads over data.
  drawNotice(text) {
    const ctx = this.ctx, p = this.plot;
    ctx.font = '600 14px system-ui, sans-serif';
    const w = ctx.measureText(text).width + 32, h = 34, x = p.x + (p.w - w) / 2, y = p.y + (p.h - h) / 2;
    ctx.fillStyle = 'rgba(17, 19, 23, 0.85)';
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, 6);
    ctx.fill();
    ctx.fillStyle = '#e3e6eb';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, p.x + p.w / 2, y + h / 2);
    ctx.textBaseline = 'alphabetic';
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

// Draw a band between two world coordinates along one axis, with its centre line (dragged to move it).
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
  ctx.strokeStyle = color.replace('0.9)', '0.5)');
  ctx.beginPath();
  if (axis === 'y') { const m = (y0 + y1) / 2; ctx.moveTo(x0, m); ctx.lineTo(x1, m); }
  else { const m = (x0 + x1) / 2; ctx.moveTo(m, y0); ctx.lineTo(m, y1); }
  ctx.stroke();
}

// The slice bands as grid indices: rows [r0, r1) of the horizontal slice's depth band, columns [c0, c1) of the
// vertical slice's across-track band. Grid row r covers depth [r, r + 1) * cell size; column c across-track
// [c, c + 1) * cell size. A band covers every cell it touches, and at least one.
function gridBand([lo, hi]) {
  const b = state.settings.binSize_m, i0 = Math.floor(lo / b + 1e-6);
  return [i0, Math.max(i0 + 1, Math.ceil(hi / b - 1e-6))];
}
function depthRows() { return gridBand(state.slices.depth); }
function acrossCols() { return gridBand(state.slices.across); }
// Their world extents: the cells averaged.
function depthBand() { const b = state.settings.binSize_m, [r0, r1] = depthRows(); return [r0 * b, r1 * b]; }
function acrossBand() { const b = state.settings.binSize_m, [c0, c1] = acrossCols(); return [c0 * b, c1 * b]; }

// Move or resize a slice band (dragged on a plot): which is 'depth' or 'across'.
function setSlice(which, lo, hi) {
  const b = state.settings.binSize_m;
  if (hi < lo) [lo, hi] = [hi, lo];
  // On cell edges: the cells averaged are what the band shows. The width is rounded to whole cells and placed about
  // the centre, so resizing doesn't shift it (by more than half a cell, to land on cell edges).
  const width = Math.max(1, Math.round((hi - lo) / b)) * b, centre = (lo + hi) / 2;
  lo = Math.round((centre - width / 2) / b) * b;
  hi = lo + width;
  if (which === 'depth' && lo < topDepth()) { hi += topDepth() - lo; lo = topDepth(); }
  state.slices[which] = [lo, hi];
  scheduleRebuild(which === 'depth' ? { h: true } : { v: true });
}

// The pie's time on the slices: the swaths it averages, ending at its swath (x is swaths from the newest).
function pieSpan() {
  const last = buffer.newestSeq(), shown = state.pieShown;
  if (last === null || !shown) return null;
  return [shown.seq0 - last - 1, shown.seq1 - last];
}
function pieTimeHandle() {
  const span = pieSpan();
  if (!span) return [];
  return [{
    axis: 'x', kind: 'line', at: span[1], set: x => setPieSeq(buffer.newestSeq() + Math.round(x)),
    label: () => `Pie: ${utc(state.pieShown.t)} UTC (drag to step through time; to the right end to follow live data)`,
  }];
}
function drawPieTime(ctx, panel) {
  const span = pieSpan();
  if (!span) return;
  const p = panel.plot, [x0] = panel.toScreen(span[0], 0), [x1] = panel.toScreen(span[1], 0);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.10)';
  ctx.fillRect(x0, p.y, x1 - x0, p.h);
  ctx.strokeStyle = state.pieSeq === null ? 'rgba(255, 255, 255, 0.55)' : 'rgba(255, 255, 255, 0.9)';
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(x1, p.y); ctx.lineTo(x1, p.y + p.h); ctx.stroke();
  ctx.lineWidth = 1;
}

const sliceHandle = (axis, which) => ({
  axis, kind: 'band', lo: state.slices[which][0], hi: state.slices[which][1],
  set: (lo, hi) => setSlice(which, lo, hi), done: saveSlices,
  label: part => {
    const [a, b] = which === 'depth' ? depthBand() : acrossBand(), how = part === 'move' ? 'drag to move' : 'drag to resize';
    return which === 'depth' ? `Horizontal slice: ${a.toFixed(1)} to ${b.toFixed(1)} m deep (${how})`
      : `Vertical slice: ${a.toFixed(1)} to ${b.toFixed(1)} m across (${how})`;
  },
});

// The swath under x (swaths from the newest) on the slices, if held.
function swathAt(x) {
  const last = buffer.newestSeq();
  return last === null ? null : buffer.get(last + Math.ceil(x));
}
function describeSwath(x) {
  const sw = swathAt(x);
  return `${Math.max(0, Math.round(-x))} swaths ago${sw ? ' (' + utc(sw.t) + ' UTC)' : ''}`;
}

const panels = {
  pie: new Panel('pie', {
    xLabel: 'Across-track (m)   port ← → starboard',
    yLabel: 'Depth below transducer (m)',
    pinTop: true,
    equalAspect: true,
    describe: (x, y) => `across ${x.toFixed(1)} m, depth ${y.toFixed(1)} m`,
    overlay: (ctx, panel) => {
      if (!state.settings) return;
      band(ctx, panel, 'y', ...depthBand(), SLICE_H_COLOR);
      band(ctx, panel, 'x', ...acrossBand(), SLICE_V_COLOR);
    },
    handles: () => state.settings ? [sliceHandle('y', 'depth'), sliceHandle('x', 'across')] : [],
  }),
  horizontal: new Panel('horizontal', {
    xLabel: 'Swaths (0 = newest)',
    yLabel: 'Across-track (m)   port ↑',
    // Depths in the slice's band can only lie within it.
    valueRange: () => {
      if (state.hMode === 'amplitude' || !state.settings) return null;
      const [lo, hi] = depthBand();
      return { lo, hi };
    },
    describe: (x, y) => `${describeSwath(x)}, across ${y.toFixed(1)} m`,
    overlay: (ctx, panel) => {
      if (!state.settings) return;
      band(ctx, panel, 'y', ...acrossBand(), SLICE_V_COLOR);
      drawPieTime(ctx, panel);
    },
    handles: () => state.settings ? [...pieTimeHandle(), sliceHandle('y', 'across')] : [],
  }),
  vertical: new Panel('vertical', {
    xLabel: 'Swaths (0 = newest)',
    yLabel: 'Depth below transducer (m)',
    pinTop: true,
    describe: (x, y) => `${describeSwath(x)}, depth ${y.toFixed(1)} m`,
    overlay: (ctx, panel) => {
      if (!state.settings) return;
      band(ctx, panel, 'y', ...depthBand(), SLICE_H_COLOR);
      if (state.tracking === 'layers') drawLayers(ctx, panel);
      if (state.tracking === 'thermocline') drawThermocline(ctx, panel);
      drawPieTime(ctx, panel);
    },
    handles: () => state.settings ? [...pieTimeHandle(), sliceHandle('y', 'depth')] : [],
    side: drawSVP,
  }),
};

// ---------------------------------------------------------------------------
// Experimental: tracked layers and the sound speed profile on the vertical slice
// ---------------------------------------------------------------------------
const LAYER_COLORS = ['#ff9f1c', '#2ec4b6', '#e71d36', '#ffffff', '#a463f2', '#9bf6ff'];
const LAYER_MIN_COLUMNS = 10;  // only draw layers seen in at least this many columns

// x extent (swaths from the newest) of a tracking column.
function trackX(c, last) { return [c.seq0 - last - 1, c.seq1 - last]; }

// Outline each tracked layer: its top and bottom in every column it was found in.
function drawLayers(ctx, panel) {
  const last = buffer.newestSeq();
  if (last === null) return;
  const count = new Map();
  for (const c of state.tracks) for (const [id] of c.layers) count.set(id, (count.get(id) || 0) + 1);
  ctx.lineWidth = 1.5;
  for (const c of state.tracks) {
    const [a, b] = trackX(c, last), [x0] = panel.toScreen(a, 0), [x1] = panel.toScreen(b, 0);
    for (const [id, top, bottom] of c.layers) {
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
  for (let k = state.tracks.length - 1; k >= 0; k--) if (state.tracks[k].thermo) return state.tracks[k].thermo;
}

// The tracked thermocline bottom, one segment per column colored by the cast alert, and the depth the picks are
// compared with (dashed): the picks just after the cast, or the cast's thermocline bottom.
function drawThermocline(ctx, panel) {
  const p = panel.plot, newest = buffer.newestSeq();
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
    for (const c of newest === null ? [] : state.tracks) {
      const t = c.thermo;
      if (!t) continue;
      const [a, b] = trackX(c, newest), [x0, y] = panel.toScreen(a, t[0]), [x1] = panel.toScreen(b, 0);
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
// The swath buffer: every swath received (up to the swath history), from which the pie and both slices are built,
// so the slices can be moved and the pie stepped through time without asking the plotter for anything.
// Each swath is the plotter's grid cropped to its data, one byte per cell: base + q * step dB, NO_DATA where empty.
// ---------------------------------------------------------------------------
const NO_DATA = 255;
const buffer = {
  swaths: [],  // oldest first: {seq, t, ping, pos, row0, col0, rows, cols, base, step, q}, plus cached band sums
  bytes: 0,
  newestSeq() { return this.swaths.length ? this.swaths[this.swaths.length - 1].seq : null; },
  oldestSeq() { return this.swaths.length ? this.swaths[0].seq : null; },
  // Index of the last swath numbered seq or less, or -1.
  indexAtOrBefore(seq) {
    let lo = 0, hi = this.swaths.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.swaths[mid].seq <= seq) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return found;
  },
  get(seq) {
    const sw = this.swaths[this.indexAtOrBefore(seq)];
    return sw && sw.seq === seq ? sw : null;
  },
  add(sw) {
    this.swaths.push(sw);
    this.bytes += sw.q.length;
  },
  // Keep the newest swaths within both the swath history and its memory budget (the plotter keeps the same);
  // tracking results go with the swaths they cover.
  trim() {
    const s = state.settings, max = s ? s.historySwaths : Infinity, budget = s ? s.historyMemory_MB * 1e6 : Infinity;
    let drop = Math.max(0, this.swaths.length - max), bytes = this.bytes;
    for (let i = 0; i < drop; i++) bytes -= this.swaths[i].q.length;
    while (drop < this.swaths.length && bytes > budget) bytes -= this.swaths[drop++].q.length;
    if (drop) { this.swaths.splice(0, drop); this.bytes = bytes; }
    // Whether the memory budget, not the swath count, is what limits the history.
    this.memoryLimited = !!s && this.swaths.length < max && this.swaths.length > 0 &&
      this.bytes + this.bytes / this.swaths.length > budget;
    const oldest = this.oldestSeq();
    if (oldest === null) state.tracks = [];
    else if (state.tracks.length && state.tracks[0].seq1 < oldest) state.tracks = state.tracks.filter(c => c.seq1 >= oldest);
  },
  clear() { this.swaths = []; this.bytes = 0; },
};

// Per swath, cached while the band is the same: the vertical slice's sums over its columns [c0, c1), per row.
function swathVertical(sw, c0, c1) {
  const key = `${c0}:${c1}`;
  if (sw.v && sw.v.key === key) return sw.v;
  const sum = new Float32Array(sw.rows), n = new Uint16Array(sw.rows);
  const a = Math.max(c0, sw.col0) - sw.col0, b = Math.min(c1, sw.col0 + sw.cols) - sw.col0, q = sw.q, W = sw.cols;
  for (let r = 0; b > a && r < sw.rows; r++) {
    let acc = 0, k = 0;
    for (let i = r * W + a, end = r * W + b; i < end; i++) { const v = q[i]; if (v !== NO_DATA) { acc += v; k++; } }
    sum[r] = k * sw.base + sw.step * acc;
    n[r] = k;
  }
  return (sw.v = { key, sum, n });
}

// First and last row with data in each column of a swath (-1 where none).
function columnRows(sw) {
  if (sw.colRows) return sw.colRows;
  const first = new Int16Array(sw.cols).fill(-1), last = new Int16Array(sw.cols).fill(-1), q = sw.q, W = sw.cols;
  for (let r = 0; r < sw.rows; r++) {
    for (let c = 0; c < W; c++) {
      if (q[r * W + c] === NO_DATA) continue;
      if (first[c] < 0) first[c] = r;
      last[c] = r;
    }
  }
  return (sw.colRows = { first, last });
}

// Per swath, cached while the band is the same: the horizontal slice's sums over its rows [r0, r1), per column, and
// for the depth mode, the sums of linear amplitude times depth and of amplitude, in the columns where the swath
// covers the whole band (data at or above its top row and at or below its bottom row; elsewhere the fan's edge or
// the seafloor cuts the band and would bias the depth).
function swathHorizontal(sw, r0, r1, bin, depthMode) {
  const key = `${r0}:${r1}:${bin}:${depthMode}`;
  if (sw.h && sw.h.key === key) return sw.h;
  const C = sw.cols, sum = new Float32Array(C), n = new Uint16Array(C), zw = new Float32Array(C), w = new Float32Array(C);
  const a = Math.max(r0, sw.row0) - sw.row0, b = Math.min(r1, sw.row0 + sw.rows) - sw.row0, q = sw.q;
  const whole = depthMode && r0 >= sw.row0 && r1 <= sw.row0 + sw.rows;
  let rows = null, amp = null;
  if (whole) {
    rows = columnRows(sw);
    amp = sw.amp || (sw.amp = Float32Array.from({ length: 255 }, (_, k) => 10 ** ((sw.base + sw.step * k) / 20)));
  }
  for (let r = a; r < b; r++) {
    const z = (sw.row0 + r + 0.5) * bin;
    for (let c = 0, i = r * C; c < C; c++, i++) {
      const v = q[i];
      if (v === NO_DATA) continue;
      sum[c] += v;
      n[c]++;
      if (whole && rows.first[c] <= a && rows.last[c] >= b - 1) { const A = amp[v]; zw[c] += A * z; w[c] += A; }
    }
  }
  for (let c = 0; c < C; c++) sum[c] = n[c] * sw.base + sw.step * sum[c];
  return (sw.h = { key, sum, n, zw, w });
}

// A slice image from the buffer: which 'v' (rows: depth) or 'h' (rows: across-track). Its columns are the groups of
// along-track-average swaths (numbered k * A to (k + 1) * A - 1, the same groups the plotter tracks in), oldest at
// left. x is in swaths from the newest: swath n spans [n - newest - 1, n - newest].
function buildSlice(which) {
  const sw = buffer.swaths, s = state.settings;
  if (!sw.length) return null;
  const A = s.alongTrackAvg_swath, bin = s.binSize_m, depth = which === 'h' && state.hMode !== 'amplitude';
  const [i0, i1] = which === 'v' ? acrossCols() : depthRows();
  let lo = Infinity, hi = -Infinity;
  for (const w of sw) {
    const o = which === 'v' ? w.row0 : w.col0;
    lo = Math.min(lo, o);
    hi = Math.max(hi, o + (which === 'v' ? w.rows : w.cols));
  }
  const g0 = Math.floor(sw[0].seq / A), g1 = Math.floor(sw[sw.length - 1].seq / A), W = g1 - g0 + 1, H = hi - lo;
  const num = new Float32Array(W * H), den = new Float32Array(W * H);
  for (const w of sw) {
    const c = which === 'v' ? swathVertical(w, i0, i1) : swathHorizontal(w, i0, i1, bin, depth);
    const top = depth ? c.zw : c.sum, bottom = depth ? c.w : c.n;
    const g = Math.floor(w.seq / A) - g0, off = (which === 'v' ? w.row0 : w.col0) - lo;
    for (let i = 0; i < top.length; i++) {
      if (bottom[i] > 0) { const k = (off + i) * W + g; num[k] += top[i]; den[k] += bottom[i]; }
    }
  }
  const data = new Float32Array(W * H);
  for (let k = 0; k < data.length; k++) data[k] = den[k] > 0 ? num[k] / den[k] : NaN;
  const last = sw[sw.length - 1].seq;
  return { data, W, H, lo, g0, x0: g0 * A - last - 1, x1: (g1 + 1) * A - last - 1 };
}

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

let shownDepthRows = null;  // the horizontal slice's band when its depth colors were last set
function updateSlices(which = ['v', 'h']) {
  const s = state.settings;
  if (s && state.hMode !== 'amplitude') {
    // Depths lie within the band, so a new band needs a new color scale.
    const key = depthRows().join(':');
    if (key !== shownDepthRows) { shownDepthRows = key; panels.horizontal.resetLevels(); }
  }
  for (const [panel, kind] of [[panels.vertical, 'v'], [panels.horizontal, 'h']]) {
    if (!which.includes(kind)) continue;
    const im = s && buildSlice(kind);
    if (!im) { panel.setImage(null); continue; }
    const b = s.binSize_m, ext = dataExtentRows(im.data, im.W, im.H) || [0, im.H - 1];
    panel.setImage({
      w: im.W, h: im.H, data: im.data, x0: im.x0, x1: im.x1, y0: im.lo * b, y1: (im.lo + im.H) * b, ci0: im.g0, ri0: im.lo,
      // Auto-zoom shows the whole swath history, filling from the right.
      extent: { x0: Math.min(im.x0, -s.historySwaths), x1: im.x1, y0: (im.lo + ext[0]) * b, y1: (im.lo + ext[1] + 1) * b },
      shade: panel === panels.horizontal && state.hMode === 'shaded' ? hillshade(im.data, im.W, im.H, b) : null,
    });
  }
}

// The pie: the mean of along-track-average swaths, ending at the chosen one (or the newest).
function updatePie() {
  const sw = buffer.swaths, s = state.settings;
  if (!sw.length || !s) { state.pieShown = null; panels.pie.setImage(null); showPieTime(); return; }
  const A = s.alongTrackAvg_swath;
  const want = state.pieSeq === null ? buffer.newestSeq() : Math.max(buffer.oldestSeq(), state.pieSeq);
  const i1 = Math.max(0, buffer.indexAtOrBefore(want)), seq1 = sw[i1].seq;
  let i0 = i1;
  while (i0 > 0 && sw[i0 - 1].seq > seq1 - A) i0--;
  const group = sw.slice(i0, i1 + 1);
  let r0 = Infinity, r1 = -Infinity, c0 = Infinity, c1 = -Infinity;
  for (const w of group) {
    r0 = Math.min(r0, w.row0); r1 = Math.max(r1, w.row0 + w.rows);
    c0 = Math.min(c0, w.col0); c1 = Math.max(c1, w.col0 + w.cols);
  }
  const W = c1 - c0, H = r1 - r0, num = new Float32Array(W * H), den = new Uint16Array(W * H);
  for (const w of group) {
    const q = w.q;
    for (let r = 0; r < w.rows; r++) {
      const k0 = (w.row0 - r0 + r) * W + (w.col0 - c0), i0r = r * w.cols;
      for (let c = 0; c < w.cols; c++) {
        const v = q[i0r + c];
        if (v !== NO_DATA) { num[k0 + c] += w.base + w.step * v; den[k0 + c]++; }
      }
    }
  }
  const data = new Float32Array(W * H);
  for (let k = 0; k < data.length; k++) data[k] = den[k] ? num[k] / den[k] : NaN;
  state.pieShown = { seq0: group[0].seq, seq1, t: group[group.length - 1].t };
  const b = s.binSize_m, x0 = c0 * b, y0 = r0 * b, x1 = c1 * b, y1 = r1 * b;
  panels.pie.setImage({ w: W, h: H, data, x0, x1, y0, y1, ci0: c0, ri0: r0, extent: { x0, x1, y0, y1 } });
  showPieTime();
}

// Show the newest swaths in the pie (null), or the ones ending at swath seq: dragging to the newest end goes live.
function setPieSeq(seq) {
  const newest = buffer.newestSeq();
  state.pieSeq = newest === null || seq >= newest ? null : Math.max(buffer.oldestSeq(), seq);
  scheduleRebuild({ pie: true });
}
const liveButton = panels.pie.el.querySelector('.live'), pieTimeEl = panels.pie.el.querySelector('.pie-time');
liveButton.addEventListener('click', () => setPieSeq(Infinity));
function showPieTime() {
  const live = state.pieSeq === null, shown = state.pieShown;
  liveButton.setAttribute('aria-pressed', String(live));
  if (!shown) { pieTimeEl.textContent = ''; return; }
  const ago = buffer.newestSeq() - shown.seq1;
  pieTimeEl.textContent = `${utc(shown.t)} UTC` + (live ? '' : ` · ${ago} swath${ago === 1 ? '' : 's'} ago`);
}

// Rebuild the plots from the buffer at the next frame: parts {v, h, pie} (the vertical and horizontal slices), all if
// not given.
let rebuild = null;
function scheduleRebuild(parts = { v: true, h: true, pie: true }) {
  if (!rebuild) {
    rebuild = {};
    requestAnimationFrame(() => {
      const r = rebuild;
      rebuild = null;
      const slices = ['v', 'h'].filter(k => r[k]);
      if (slices.length) updateSlices(slices);
      if (r.pie) updatePie();
      for (const p of Object.values(panels)) p.draw();  // indicators drawn on the others
    });
  }
  Object.assign(rebuild, parts);
}

// Hillshading of a depth image (w x h, row-major, NaN where empty) as a factor per cell: the surface is lit from the
// upper left of the plot, 45 degrees up. Cells are taken as square, size metres on a side (along track, the real
// spacing depends on the vessel's speed), and depths as positive down.
const SHADE = { ambient: 0.35, light: [-0.5, -0.5, Math.SQRT1_2] };  // light: unit vector (right, down the plot, up)
function hillshade(z, w, h, size) {
  const out = new Float32Array(w * h).fill(1);
  const [lx, ly, lz] = SHADE.light;
  const at = (r, c) => (r < 0 || r >= h || c < 0 || c >= w) ? NaN : z[r * w + c];
  for (let r = 0; r < h; r++) {
    for (let c = 0; c < w; c++) {
      const z0 = z[r * w + c];
      if (Number.isNaN(z0)) continue;
      // Central differences, one-sided at edges and next to empty cells. Height is -depth.
      const zl = at(r, c - 1), zr = at(r, c + 1), zu = at(r - 1, c), zd = at(r + 1, c);
      const gx = -((Number.isNaN(zr) ? z0 : zr) - (Number.isNaN(zl) ? z0 : zl)) / ((Number.isNaN(zr) || Number.isNaN(zl) ? 1 : 2) * size);
      const gy = -((Number.isNaN(zd) ? z0 : zd) - (Number.isNaN(zu) ? z0 : zu)) / ((Number.isNaN(zd) || Number.isNaN(zu) ? 1 : 2) * size);
      const lit = Math.max(0, (-gx * lx - gy * ly + lz) / Math.hypot(gx, gy, 1));
      out[r * w + c] = SHADE.ambient + (1 - SHADE.ambient) * lit / lz;  // flat ground keeps its color
    }
  }
  return out;
}

function redrawAll() {
  updatePie();
  updateSlices();
}

function resetData() {
  buffer.clear();
  state.tracks = [];
  state.pieSeq = null;
  state.pieShown = null;
  showPieTime();
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
// Horizontal slice: amplitude, or the amplitude-weighted depth of the signal in its band (optionally shaded).
const hModeSelect = panels.horizontal.el.querySelector('.hmode');
hModeSelect.addEventListener('change', () => {
  const wasDepth = state.hMode !== 'amplitude';
  state.hMode = hModeSelect.value;
  const depth = state.hMode !== 'amplitude';
  if (depth !== wasDepth) {  // shading alone keeps the color scale
    panels.horizontal.resetLevels();
    panels.horizontal.setUnit(depth ? 'm' : 'dB');
  }
  updateSlices();
});

document.getElementById('grid').addEventListener('change', ev => {
  state.showGrid = ev.target.checked;
  for (const p of Object.values(panels)) p.draw();
});
document.getElementById('tracking').addEventListener('change', ev => {
  state.tracking = ev.target.value;
  panels.vertical.draw();
});
const combineSelect = document.getElementById('combine');
try { if (['mean', 'max', 'decimate'].includes(localStorage.getItem('wcp.combine'))) state.combine = localStorage.getItem('wcp.combine'); } catch (e) { /* default */ }
combineSelect.value = state.combine;
combineSelect.addEventListener('change', () => {
  state.combine = combineSelect.value;
  try { localStorage.setItem('wcp.combine', state.combine); } catch (e) { /* not kept */ }
  for (const p of Object.values(panels)) if (p.image && p.levels) { p.colorize(); p.draw(); }
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
  for (const input of settingsForm.querySelectorAll('input[name], select[name]')) {
    if (document.activeElement === input) continue;
    if (input.type === 'checkbox') input.checked = state.settings[input.name];
    else input.value = state.settings[input.name];
  }
  const d = state.settings.transducerDepth_m;
  document.getElementById('transducer-depth').textContent = d === null
    ? 'Transducer depth not yet known (waiting for the installation datagram)'
    : `Transducer ${d.toFixed(2)} m below the surface`;
  showBackgroundSwaths();
  markDirty(settingsForm);
}

// Background swaths applies only to the sidelobe suppression that averages recent swaths.
function showBackgroundSwaths() {
  document.getElementById('background-swaths').hidden = settingsForm.elements.sidelobeSuppression.value !== 'ping_mean';
}
settingsForm.elements.sidelobeSuppression.addEventListener('change', showBackgroundSwaths);

for (const name of ['sidelobeSuppression', 'dualSwath']) {
  settingsForm.elements[name].addEventListener('change', ev => {
    send({ type: 'settings', settings: { [name]: ev.target.value } });
  });
}

// The depth reference applies as soon as it is ticked, like the toolbar checkboxes.
settingsForm.elements.referenceToSurface.addEventListener('change', ev => {
  send({ type: 'settings', settings: { referenceToSurface: ev.target.checked } });
});

settingsForm.addEventListener('submit', ev => {
  ev.preventDefault();
  const requested = {};
  for (const input of settingsForm.querySelectorAll('input[name], select[name]')) {
    if (input.type === 'checkbox') requested[input.name] = input.checked;
    else if (input.tagName === 'SELECT') requested[input.name] = input.value;
    else if (input.value !== '') requested[input.name] = Number(input.value);
  }
  send({ type: 'settings', settings: requested });
  document.activeElement.blur();
});

// ---------------------------------------------------------------------------
// Data source: real-time or replay (shared: the server's source is the same for every viewer)
// ---------------------------------------------------------------------------
const sourceBar = document.getElementById('source-bar');
const sourceEl = {
  badge: document.getElementById('mode-badge'), error: document.getElementById('source-error'),
  realtime: document.getElementById('realtime-controls'), replay: document.getElementById('replay-controls'),
  summary: document.getElementById('realtime-summary'), state: document.getElementById('realtime-state'),
  browse: document.getElementById('replay-browse'), file: document.getElementById('replay-file'),
  play: document.getElementById('replay-play'), speed: document.getElementById('replay-speed'),
  position: document.getElementById('replay-position'), time: document.getElementById('replay-time'),
};
const realtimeConfig = document.getElementById('realtime-config');
const realtimeConfigToggle = document.getElementById('realtime-config-toggle');
let source = { mode: 'realtime', config: {} };  // latest 'source' message from the server
let seeking = false;  // the position slider is being dragged
let listedDir = null;  // directory the file menu was filled from

function sourceCommand(cmd, extra = {}) { send({ type: 'source', cmd, ...extra }); }
function utcTime(t) { return new Date(t * 1000).toISOString().slice(0, 19).replace('T', ' '); }
function joinPath(dir, name) { return dir.replace(/[\\/]+$/, '') + (dir.includes('\\') ? '\\' : '/') + name; }

function showSource() {
  const replaying = source.mode === 'replay', r = source.replay, c = source.config;
  sourceBar.dataset.mode = source.mode;
  for (const b of sourceBar.querySelectorAll('.segmented button')) {
    b.setAttribute('aria-pressed', String(b.dataset.mode === source.mode));
  }
  sourceEl.realtime.hidden = replaying;
  sourceEl.replay.hidden = !replaying;
  if (replaying) closeRealtimeConfig();

  sourceEl.badge.classList.toggle('idle', replaying && !(r && r.playing));
  sourceEl.badge.textContent = !replaying ? 'Real-time'
    : !r ? 'Replay · no file' : r.playing ? 'Replaying' : r.finished ? 'Replay ended' : 'Replay paused';
  sourceEl.summary.textContent = `UDP ${c.udpIp}:${c.udpPort}` + (c.multicast ? ` · multicast ${c.multicast}` : '');
  showOpening();

  sourceEl.file.hidden = !r;
  for (const el of [sourceEl.play, sourceEl.speed, sourceEl.position]) el.disabled = !r;
  if (r) {
    sourceEl.play.textContent = r.playing ? '❚❚ Pause' : (r.finished ? '▶ Play again' : '▶ Play');
    sourceEl.speed.value = String(r.speed);
    sourceEl.position.min = r.start;
    sourceEl.position.max = r.end;
    if (!seeking) sourceEl.position.value = r.time;
    const shown = seeking ? Number(sourceEl.position.value) : r.time;
    sourceEl.time.textContent = `${utcTime(shown)} UTC` + (r.finished ? ' · end' : '');
    if (listedDir !== c.dataDir) loadFileMenu();
    else if (sourceEl.file.value !== r.name) sourceEl.file.value = r.name;
  } else {
    sourceEl.play.textContent = '▶ Play';
    sourceEl.time.textContent = 'Browse… to choose a recording';
  }
  if (realtimeConfig.hidden) fillConfig();
}

// While the plotter opens a file it reads through it to index its datagrams, which takes a while for a large file or
// one on a slow or network drive, and nothing else happens meanwhile: say so, with the time taken so far.
let openingSince = null, openingTimer = null;
let shownOpening = null;  // what the plots last said was opening
function showOpening() {
  const badge = sourceEl.badge;
  if ((source.opening || null) !== shownOpening) {
    shownOpening = source.opening || null;
    for (const p of Object.values(panels)) p.draw();
  }
  if (!source.opening) {
    if (openingTimer) { clearInterval(openingTimer); openingTimer = null; openingSince = null; }
    badge.classList.remove('busy');
    badge.removeAttribute('title');
    return;
  }
  if (!openingTimer) { openingSince = Date.now(); openingTimer = setInterval(showOpening, 1000); }
  const seconds = Math.floor((Date.now() - openingSince) / 1000);
  badge.classList.add('busy');
  badge.textContent = `Opening ${source.opening}…` + (seconds ? ` ${seconds} s` : '');
  badge.title = 'Reading through the file to index its datagrams. A large file, or one on a slow or network drive, ' +
    'can take a while; data appears once it is done.';
}

// The file menu: the other recordings in the replayed file's directory.
async function loadFileMenu() {
  const dir = listedDir = source.config.dataDir;
  try {
    const listing = await (await fetch(`/browse?dir=${encodeURIComponent(dir)}`)).json();
    if (listing.error) throw new Error(listing.error);
    sourceEl.file.replaceChildren(...listing.files.map(f =>
      Object.assign(document.createElement('option'), { value: f.name, textContent: f.name })));
    if (source.replay) sourceEl.file.value = source.replay.name;
  } catch (e) {
    listedDir = null;
  }
}

for (const b of sourceBar.querySelectorAll('.segmented button')) {
  b.addEventListener('click', () => {
    if (b.dataset.mode === 'replay') {
      if (source.mode !== 'replay') chooseRecording();  // switches once a file is chosen
    } else if (source.mode !== 'realtime') {
      sourceCommand('realtime');
    }
  });
}
sourceEl.browse.addEventListener('click', () => chooseRecording());
sourceEl.file.addEventListener('change', () => {
  if (sourceEl.file.value) sourceCommand('open', { path: joinPath(source.config.dataDir, sourceEl.file.value) });
});
sourceEl.play.addEventListener('click', () => sourceCommand(source.replay && source.replay.playing ? 'pause' : 'play'));
sourceEl.speed.addEventListener('change', () => sourceCommand('speed', { speed: Number(sourceEl.speed.value) }));
sourceEl.position.addEventListener('input', () => { seeking = true; showSource(); });
sourceEl.position.addEventListener('change', () => {
  seeking = false;
  sourceCommand('seek', { time: Number(sourceEl.position.value) });
});

// Browsing the plotter computer's directories for a recording.
const browser = {
  dialog: document.getElementById('browser'), dir: document.getElementById('browser-dir'),
  up: document.getElementById('browser-up'), list: document.getElementById('browser-list'),
  error: document.getElementById('browser-error'), parent: null,
};
function sizeText(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
}
function browserItem(cls, icon, name, extra, onPick) {
  const li = document.createElement('li');
  li.className = cls;
  const b = document.createElement('button');
  b.type = 'button';
  b.innerHTML = '<span class="icon"></span><span class="name"></span><span class="size"></span>';
  b.querySelector('.icon').textContent = icon;
  b.querySelector('.name').textContent = name;
  b.querySelector('.size').textContent = extra;
  b.addEventListener('click', onPick);
  li.append(b);
  return li;
}
async function browseTo(dir) {
  browser.error.textContent = '';
  let listing;
  try {
    listing = await (await fetch(`/browse?dir=${encodeURIComponent(dir ?? '')}`)).json();
  } catch (e) {
    browser.error.textContent = 'Lost connection to the plotter.';
    return;
  }
  if (listing.error) { browser.error.textContent = listing.error; return; }
  browser.dir.value = listing.dir;
  browser.parent = listing.parent;
  browser.up.disabled = !listing.parent;
  const items = [
    ...listing.dirs.map(d => browserItem('dir', '▸', d + '/', '', () => browseTo(joinPath(listing.dir, d)))),
    ...listing.files.map(f => browserItem('file', '≋', f.name, sizeText(f.size), () => {
      sourceCommand('open', { path: joinPath(listing.dir, f.name) });
      browser.dialog.close();
    })),
  ];
  if (!listing.files.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No .kmall or .kmwcd files here' + (listing.dirs.length ? '; open a directory above.' : '.');
    items.push(li);
  }
  browser.list.replaceChildren(...items);
  browser.list.scrollTop = 0;
}
function openBrowser() {
  browser.dialog.showModal();
  browseTo(source.config.dataDir);
}

// Choosing a recording: in the operating system's own Open dialog, which the server shows on the plotter's computer
// when this browser is on it; otherwise (viewing from another computer, or no dialog available) in the page's browser.
let nativeDialog = true;
async function chooseRecording() {
  if (!nativeDialog) { openBrowser(); return; }
  sourceEl.browse.disabled = true;
  try {
    const result = await (await fetch('/choose-file', { method: 'POST' })).json();
    if (result.unavailable) { nativeDialog = false; openBrowser(); }
  } catch (e) {
    sourceEl.error.textContent = 'Lost connection to the plotter.';
  } finally {
    sourceEl.browse.disabled = false;
  }
}
browser.up.addEventListener('click', () => { if (browser.parent) browseTo(browser.parent); });
browser.dir.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); browseTo(browser.dir.value.trim()); } });
browser.dialog.addEventListener('click', ev => { if (ev.target === browser.dialog) browser.dialog.close(); });

// Real-time configuration menu.
function fillConfig() {
  for (const input of realtimeConfig.querySelectorAll('input')) input.value = source.config[input.name] ?? '';
  markDirty(realtimeConfig);
}
function configChanges() {
  const changed = {};
  for (const input of realtimeConfig.querySelectorAll('input')) {
    if (String(input.value).trim() !== String(source.config[input.name] ?? '')) changed[input.name] = input.value.trim();
  }
  return changed;
}
function closeRealtimeConfig() {
  realtimeConfig.hidden = true;
  realtimeConfigToggle.setAttribute('aria-expanded', 'false');
}
realtimeConfigToggle.addEventListener('click', () => {
  realtimeConfig.hidden = !realtimeConfig.hidden;
  realtimeConfigToggle.setAttribute('aria-expanded', String(!realtimeConfig.hidden));
  if (!realtimeConfig.hidden) fillConfig();
});
realtimeConfig.addEventListener('input', () => markDirty(realtimeConfig));
realtimeConfig.addEventListener('submit', ev => {
  ev.preventDefault();
  const changed = configChanges();
  if (Object.keys(changed).length) sourceCommand('config', { config: changed });
  closeRealtimeConfig();
});

// Apply buttons: highlighted while their form has changes not yet applied.
function markDirty(form) {
  const dirty = form === settingsForm ? settingsChanged() : Object.keys(configChanges()).length > 0;
  const button = form.querySelector('button[type="submit"]');
  button.classList.toggle('dirty', dirty);
}
function settingsChanged() {
  // Number fields only: the checkbox and menus apply at once.
  return [...settingsForm.querySelectorAll('input[type="number"]')].some(input =>
    input.value !== '' && Number(input.value) !== state.settings[input.name]);
}
settingsForm.addEventListener('input', () => markDirty(settingsForm));

// About
const aboutDialog = document.getElementById('about');
document.getElementById('about-open').addEventListener('click', () => aboutDialog.showModal());
aboutDialog.addEventListener('click', ev => { if (ev.target === aboutDialog) aboutDialog.close(); });  // backdrop

// ---------------------------------------------------------------------------
// Status line
// ---------------------------------------------------------------------------
const statusEl = document.getElementById('status');
let lastStats = null;
function showStats(stats) {
  const now = performance.now();
  // Rate of datagrams received live, or processed from the file being replayed.
  const replaying = source.mode === 'replay';
  const count = replaying ? stats.processed ?? 0 : stats.datagrams ?? 0;
  let rate = '';
  if (lastStats && lastStats.replaying === replaying && count >= lastStats.count) {
    rate = `${((count - lastStats.count) / ((now - lastStats.at) / 1000)).toFixed(1)} datagrams/s · `;
  }
  // Swaths skipped since the last update because the cell size is too small for the swath, and datagrams dropped
  // because processing couldn't keep up with them.
  const tooLarge = lastStats ? (stats.grid_too_large ?? 0) - lastStats.tooLarge : 0;
  const behind = lastStats ? (stats.behind ?? 0) - lastStats.behind : 0;
  if (!replaying) {
    // Whether real-time data is arriving, next to the source.
    const perSec = lastStats && !lastStats.replaying && count >= lastStats.count
      ? (count - lastStats.count) / ((now - lastStats.at) / 1000) : null;
    sourceEl.state.textContent = stats.error ? '' : perSec ? `receiving ${perSec.toFixed(1)} datagrams/s` : 'no data arriving';
    sourceEl.state.classList.toggle('ok', !stats.error && perSec > 0);
    if (stats.error) sourceEl.error.textContent = stats.error;
    else if (sourceEl.error.textContent.startsWith("Can't receive UDP")) sourceEl.error.textContent = '';
  }
  // The sonar's ping rate, from the data's timestamps (the recorded rate when replaying).
  if (stats.ping_rate_hz) {
    const only = state.settings && state.settings.dualSwath !== 'both' ? ` (${state.settings.dualSwath} shown)` : '';
    const swaths = stats.swaths_per_ping > 1 ? ` × ${stats.swaths_per_ping} swaths${only}` : '';
    rate = `${stats.ping_rate_hz.toFixed(1)} pings/s${swaths} · ` + rate;
  }
  lastStats = { at: now, count, replaying, tooLarge: stats.grid_too_large ?? 0, behind: stats.behind ?? 0 };
  statusEl.classList.toggle('error', tooLarge > 0 || behind > 0);
  statusEl.textContent = (tooLarge > 0 ? 'Cell size too small for this swath: swaths are being skipped · ' : '') +
    (behind > 0 ? `Processing can't keep up: ${behind} datagrams dropped · ` : '') +
    rate + (replaying ? `replaying · processed ${count}`
      : `received ${count} · incomplete dropped ${stats.discarded ?? 0}`) +
    ` · processing ${(stats.process_ms ?? 0).toFixed(0)} ms/datagram` +
    ` · holding ${buffer.swaths.length} swaths (${(buffer.bytes / 1e6).toFixed(0)} MB${buffer.memoryLimited ? ', memory limit' : ''})`;
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
let ws = null;
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function handleBinary(buf) {
  // uint32 header length, JSON header, then each swath's rows x cols bytes in turn.
  const headerLen = new DataView(buf).getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headerLen)));
  if (header.type !== 'swaths') return;
  let pos = 4 + headerLen;
  for (const meta of header.swaths) {
    const size = meta.rows * meta.cols;
    buffer.add({ ...meta, q: new Uint8Array(buf, pos, size) });
    pos += size;
  }
  buffer.trim();
  scheduleRebuild();
}

function handleText(msg) {
  if (msg.type === 'settings') {
    state.settings = msg.settings;
    showSettings();
    panels.pie.yLabel = panels.vertical.yLabel = depthLabel();
    // The top depth may have changed (heave allowance, depth reference).
    for (const p of [panels.pie, panels.vertical]) if (p.view && p.autoZoom) p.fit();
    buffer.trim();  // the swath history may be shorter
    scheduleRebuild();
  } else if (msg.type === 'reset') {
    resetData();
    if (msg.svp) { state.svp = null; panels.vertical.draw(); }
  } else if (msg.type === 'source') {
    if (msg.error) {
      sourceEl.error.textContent = msg.error;
    } else {
      if (msg.mode !== source.mode || msg.replay?.name !== source.replay?.name) sourceEl.error.textContent = '';
      source = msg;
      showSource();
    }
  } else if (msg.type === 'tracks') {
    state.tracks.push(...msg.tracks);
    buffer.trim();
    panels.vertical.draw();
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
  ws.onclose = ev => {
    statusEl.classList.add('error');
    statusEl.textContent = `Disconnected from plotter${ev.reason ? ` (${ev.reason})` : ''}, retrying…`;
    resetData();
    setTimeout(connect, 2000);
  };
}

connect();
