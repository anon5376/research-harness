/* research-harness background renderer.
 * Halftone dot field: a regular grid of round dots (4 CSS px cells), dot radius
 * = (cell/2)*sqrt(L), colour = 3-stop tint ramp interpolated in OKLab.
 * Classic script, no dependencies. Exposes window.RHBackground.
 */
(function () {
  'use strict';

  var CELL_CSS = 4;      // grid pitch in CSS px
  var LEVELS = 48;       // colour buckets (one path + one fill each)
  var FLOOR = 0.04;      // faint base dot drawn everywhere via a pattern
  var SKIP = 0.02;       // scene values below this never get their own dot
  var TAU = Math.PI * 2;

  var PRESETS = [
    { id: 'beam', label: 'Beam' },
    { id: 'arc', label: 'Arc' },
    { id: 'clouds', label: 'Clouds' },
    { id: 'aurora', label: 'Aurora' },
    { id: 'dunes', label: 'Dunes' },
    { id: 'orbit', label: 'Orbit' },
    { id: 'quiet', label: 'Quiet' },
    { id: 'off', label: 'Off' }
  ];

  var TINT_STOPS = {
    cobalt: ['#050b1f', '#1d4fd8', '#eaf1ff'],
    ice: ['#070a10', '#8a9bb8', '#f2f6fc'],
    paper: ['#0c0b09', '#aaa292', '#f7f3ec'],
    mono: ['#0b0b0c', '#6b6b73', '#f4f4f5']
  };
  var TINTS = [
    { id: 'cobalt', label: 'Cobalt', swatch: '#1d4fd8' },
    { id: 'ice', label: 'Ice', swatch: '#a9b8d0' },
    { id: 'paper', label: 'Paper', swatch: '#d9d3c7' },
    { id: 'mono', label: 'Mono', swatch: '#6b6b73' }
  ];

  /* ---------- maths helpers ---------- */

  function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
  function smooth(e0, e1, v) {
    var t = clamp01((v - e0) / (e1 - e0));
    return t * t * (3 - 2 * t);
  }

  // Integer hash -> [0,1). Deterministic across runs and browsers.
  function hash2(ix, iy, seed) {
    var h = (Math.imul(ix, 374761393) + Math.imul(iy, 668265263) + Math.imul(seed, 1442695041)) | 0;
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  }

  function vnoise(x, y, seed) {
    var ix = Math.floor(x), iy = Math.floor(y);
    var fx = x - ix, fy = y - iy;
    var ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
    var a = hash2(ix, iy, seed), b = hash2(ix + 1, iy, seed);
    var c = hash2(ix, iy + 1, seed), d = hash2(ix + 1, iy + 1, seed);
    return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
  }

  // Fractal value noise in [0,1].
  function fbm(x, y, seed, oct) {
    var s = 0, amp = 0.5, norm = 0;
    for (var i = 0; i < oct; i++) {
      s += amp * vnoise(x, y, seed + i * 31);
      norm += amp;
      amp *= 0.5;
      x = x * 2.03 + 17.1;
      y = y * 2.03 - 9.7;
    }
    return s / norm;
  }

  /* ---------- OKLab ---------- */

  function srgbToLin(c) {
    c /= 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }
  function linToSrgb(c) {
    c = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(Math.max(c, 0), 1 / 2.4) - 0.055;
    return Math.max(0, Math.min(255, Math.round(c * 255)));
  }
  function hexToOklab(hex) {
    var n = parseInt(hex.slice(1), 16);
    var r = srgbToLin((n >> 16) & 255), g = srgbToLin((n >> 8) & 255), b = srgbToLin(n & 255);
    var l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    var m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    var s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [
      0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    ];
  }
  function oklabToCss(lab) {
    var l = lab[0] + 0.3963377774 * lab[1] + 0.2158037573 * lab[2];
    var m = lab[0] - 0.1055613458 * lab[1] - 0.0638541728 * lab[2];
    var s = lab[0] - 0.0894841775 * lab[1] - 1.2914855480 * lab[2];
    l = l * l * l; m = m * m * m; s = s * s * s;
    return 'rgb(' +
      linToSrgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s) + ',' +
      linToSrgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s) + ',' +
      linToSrgb(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s) + ')';
  }

  var paletteCache = {};
  function palette(tintId) {
    if (paletteCache[tintId]) return paletteCache[tintId];
    var labs = TINT_STOPS[tintId].map(hexToOklab);
    function at(t) {
      var a, b, k;
      if (t < 0.5) { a = labs[0]; b = labs[1]; k = t * 2; } else { a = labs[1]; b = labs[2]; k = (t - 0.5) * 2; }
      return oklabToCss([a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]);
    }
    var buckets = [];
    for (var i = 0; i < LEVELS; i++) buckets.push(at((i + 0.5) / LEVELS));
    var pal = { bg: TINT_STOPS[tintId][0], floor: at(FLOOR), buckets: buckets, patterns: {} };
    paletteCache[tintId] = pal;
    return pal;
  }

  /* ---------- scenes ----------
   * Coordinates are height-normalised: y in [0,1] top to bottom, x in [0,A],
   * A = width/height. Each scene: optional prep(xs) with per-column x values,
   * then L(x, y, i, j) -> luminance (cell indices i, j for per-cell effects).
   */

  var A = 1.6; // aspect of the current render

  var beam = {
    L: function (x, y) {
      var ox = A + 0.04, oy = -0.07;
      var ca = 0.92, sa = 0.39;            // direction ~23 deg below horizontal, pointing left
      var px = x - ox, py = y - oy;
      var t = -px * ca + py * sa;          // distance along the beam
      var n = px * sa + py * ca;           // signed distance across it
      if (t < 0) t = 0;
      var w = 0.03 + 0.15 * t;             // tapered: narrow at the source
      var q = (n * n) / (w * w);
      var core = Math.exp(-q) * Math.exp(-t * 1.2);
      var haze = Math.exp(-q / 6) * Math.exp(-t * 2.3) * 0.5;
      if (core + haze < 0.004) return 0;
      var brk = fbm(t * 2.4, n * 10, 7, 4);
      var corner = Math.exp(-(px * px + py * py) / 0.035) * 0.45;
      return (core * 1.3 + haze) * (0.6 + 0.8 * brk) + corner;
    }
  };

  var quiet = { L: function (x, y) { return 0.22 * clamp01(beam.L(x, y)); } };

  var arc = {
    L: function (x, y) {
      var R = 1.15 * Math.max(A, 1);
      var cx = A * 0.78, cy = 0.72 + R;    // apex sits at y = 0.72, right of centre
      var dx = x - cx, dy = y - cy;
      var e = Math.sqrt(dx * dx + dy * dy) - R;   // > 0 above the horizon
      var lit = 0.35 + 0.65 * smooth(0.0, A, x);  // light comes from the right
      if (e >= 0) {
        var rim = Math.exp(-(e * e) / 0.00003) + 0.4 * Math.exp(-(e * e) / 0.0004);
        var glow = 0.55 * Math.exp(-e / 0.13) * (0.75 + 0.5 * fbm(x * 3, e * 14, 21, 3));
        return lit * (rim + glow);
      }
      var inner = Math.exp(-(e * e) / 0.00003) + 0.08 * Math.exp(e / 0.03);
      return lit * inner;
    }
  };

  // Cloud masses: lobes are [dxFromRightEdge, y, r].
  var CLOUDS = [
    { I: 1.0, top: 0.03, bottom: 0.34, lobes: [
      [0.66, 0.27, 0.075], [0.56, 0.21, 0.11], [0.44, 0.15, 0.13], [0.31, 0.11, 0.14],
      [0.18, 0.14, 0.12], [0.08, 0.22, 0.10], [0.47, 0.27, 0.09], [0.28, 0.26, 0.10],
      [0.14, 0.28, 0.08], [0.60, 0.30, 0.07], [0.38, 0.30, 0.09], [0.21, 0.31, 0.08],
      [0.06, 0.30, 0.07]] },
    { I: 0.62, top: 0.37, bottom: 0.53, lobes: [
      [0.30, 0.47, 0.06], [0.21, 0.44, 0.075], [0.12, 0.48, 0.06]] },
    { I: 0.45, top: 0.04, bottom: 0.15, lobes: [
      [0.98, 0.11, 0.045], [0.90, 0.08, 0.06], [0.82, 0.11, 0.045]] }
  ];
  var clouds = {
    L: function (x, y) {
      var best = 0;
      for (var k = 0; k < CLOUDS.length; k++) {
        var c = CLOUDS[k];
        if (y < c.top - 0.06 || y > c.bottom + 0.05) continue;
        var wx = x + 0.04 * (fbm(x * 5, y * 5, 40 + k, 3) - 0.5);
        var wy = y + 0.03 * (fbm(x * 5 + 7.3, y * 5, 50 + k, 3) - 0.5);
        // Gaussian lobes overlap without gaps, so a mass has no holes; noise only roughens its rim.
        var D = 0;
        for (var m = 0; m < c.lobes.length; m++) {
          var lb = c.lobes[m];
          var ddx = wx - (A - lb[0]), ddy = wy - lb[1];
          D += Math.exp(-(ddx * ddx + ddy * ddy) / (0.32 * lb[2] * lb[2]));
        }
        if (D < 0.04) continue;
        var cov = smooth(0.18, 0.55, D + 0.26 * (fbm(x * 11, y * 11, 60 + k, 3) - 0.5));
        cov *= smooth(c.bottom + 0.07, c.bottom - 0.05, wy);             // soft base, no ruler line
        var t = clamp01((wy - c.top) / (c.bottom - c.top));
        var light = (1 - 0.68 * Math.pow(t, 0.9)) * (0.88 + 0.24 * fbm(x * 8, y * 8, 70 + k, 3));
        var v = cov * light * c.I;
        if (v > best) best = v;
      }
      return best;
    }
  };

  // Aurora curtains: u0/u1 are horizontal extents as a fraction of width.
  var CURTAINS = [
    { u0: -0.1, u1: 0.36, base: 0.31, amp: 0.035, f: 7.0, ph: 1.0, h: 0.20, I: 0.5 },
    { u0: 0.22, u1: 0.70, base: 0.21, amp: 0.028, f: 5.5, ph: 2.3, h: 0.16, I: 0.8 },
    { u0: 0.55, u1: 1.1, base: 0.36, amp: 0.04, f: 4.5, ph: 0.4, h: 0.25, I: 1.0 },
    { u0: 0.78, u1: 1.1, base: 0.14, amp: 0.022, f: 9.0, ph: 3.1, h: 0.11, I: 0.45 }
  ];
  var aurora = {
    edge: null, ends: null,
    prep: function (xs) {
      var n = xs.length;
      this.edge = []; this.ends = [];
      for (var k = 0; k < CURTAINS.length; k++) {
        var c = CURTAINS[k], e = new Float32Array(n), s = new Float32Array(n);
        for (var i = 0; i < n; i++) {
          var x = xs[i], u = x / A;
          e[i] = c.base + c.amp * Math.sin(x * c.f + c.ph) + 0.6 * c.amp * Math.sin(x * c.f * 2.7 + c.ph * 1.7) +
            0.035 * (fbm(x * 3, k * 3.1, 80 + k, 3) - 0.5);
          s[i] = smooth(c.u0, c.u0 + 0.1, u) * smooth(c.u1, c.u1 - 0.1, u);
        }
        this.edge.push(e); this.ends.push(s);
      }
    },
    L: function (x, y, i) {
      if (y > 0.5) return 0;
      var sum = 0;
      for (var k = 0; k < CURTAINS.length; k++) {
        var s = this.ends[k][i];
        if (s <= 0) continue;
        var c = CURTAINS[k];
        var d = this.edge[k][i] - y;                 // height above the lower edge
        if (d < -0.015) continue;
        var dp = d > 0 ? d : 0;
        var v = s * smooth(-0.015, 0.004, d) * Math.exp(-dp / c.h) * (1 + 0.7 * Math.exp(-dp / 0.014));
        if (v < 0.01) continue;
        var rays = 0.45 + 0.55 * fbm(x * 34 + 0.5 * Math.sin(y * 7 + k), y * 1.3 + k * 5.7, 90 + k, 3);
        sum += c.I * v * rays;
      }
      return sum * smooth(0.5, 0.3, y);
    }
  };

  var DUNE_N = 6;
  var dunes = {
    crest: null, slope: null,
    prep: function (xs) {
      var n = xs.length;
      this.crest = []; this.slope = [];
      for (var k = 0; k < DUNE_N; k++) {
        var c = new Float32Array(n), sl = new Float32Array(n);
        var amp = 0.02 + 0.007 * k, f = 3.4 - 0.32 * k, ph = k * 1.9 + 0.4;
        for (var i = 0; i < n; i++) {
          var x = xs[i];
          c[i] = 0.62 + k * 0.068 + amp * Math.sin(x * f + ph) + 0.45 * amp * Math.sin(x * f * 2.3 + ph * 2.1) +
            0.006 * (fbm(x * 6, k, 110 + k, 2) - 0.5);
          sl[i] = amp * (f * Math.cos(x * f + ph) + 0.45 * f * 2.3 * Math.cos(x * f * 2.3 + ph * 2.1));
        }
        this.crest.push(c); this.slope.push(sl);
      }
    },
    L: function (x, y, i) {
      if (y < 0.5) return 0;
      var k = -1;
      for (var m = DUNE_N - 1; m >= 0; m--) { if (this.crest[m][i] <= y) { k = m; break; } }
      if (k < 0) {
        var d0 = this.crest[0][i] - y;              // faint haze above the farthest ridge
        return 0.1 * Math.exp(-d0 / 0.04);
      }
      var dd = y - this.crest[k][i];
      var lit = 0.5 + 0.5 * Math.tanh(-this.slope[k][i] * 6);   // light from the right
      var I = 0.45 + 0.11 * k;
      var v = I * (Math.exp(-dd / 0.026) * (0.3 + 0.7 * lit) + 0.2 * Math.exp(-dd / 0.1));
      return v * (0.85 + 0.3 * fbm(x * 24, y * 24, 130, 2));
    }
  };

  var RINGS = [
    { rx: 0.95, ry: 0.40, rot: -0.42, I: 1.0 },
    { rx: 1.38, ry: 0.60, rot: -0.36, I: 0.7 }
  ];
  var orbit = {
    L: function (x, y, i, j) {
      var ox = A + 0.12, oy = -0.32;
      var px = x - ox, py = y - oy;
      var cxr = x - A, cyr = y;                     // offset from the top-right corner
      var fall = Math.exp(-Math.sqrt(cxr * cxr + cyr * cyr) / 0.85);
      var v = 0.3 * Math.exp(-(cxr * cxr + cyr * cyr) / 0.03);
      for (var k = 0; k < RINGS.length; k++) {
        var r = RINGS[k], cr = Math.cos(r.rot), sr = Math.sin(r.rot);
        var qx = px * cr + py * sr, qy = -px * sr + py * cr;
        var g = (qx * qx) / (r.rx * r.rx) + (qy * qy) / (r.ry * r.ry) - 1;
        var gx = 2 * qx / (r.rx * r.rx), gy = 2 * qy / (r.ry * r.ry);
        var d = g / Math.sqrt(gx * gx + gy * gy);    // approx distance to the ellipse
        var line = Math.exp(-(d * d) / 0.000012) + 0.28 * Math.exp(-(d * d) / 0.0002);
        v += r.I * line * (0.3 + 0.7 * fall);
      }
      var h = hash2(i, j, 991);
      if (h < 0.0032) {
        var b = hash2(i, j, 992);
        v += 0.22 + 0.7 * b * b;
      }
      return v;
    }
  };

  var SCENES = { beam: beam, quiet: quiet, arc: arc, clouds: clouds, aurora: aurora, dunes: dunes, orbit: orbit };

  /* ---------- render ---------- */

  function floorPattern(ctx, pal, cell) {
    if (pal.patterns[cell]) return pal.patterns[cell];
    var c = document.createElement('canvas');
    c.width = cell; c.height = cell;
    var g = c.getContext('2d');
    g.fillStyle = pal.bg;
    g.fillRect(0, 0, cell, cell);
    g.fillStyle = pal.floor;
    g.beginPath();
    g.arc(cell / 2, cell / 2, (cell / 2) * Math.sqrt(FLOOR), 0, TAU);
    g.fill();
    var p = ctx.createPattern(c, 'repeat');
    pal.patterns[cell] = p;
    return p;
  }

  function render(canvas, opts) {
    opts = opts || {};
    var presetId = (SCENES[opts.preset] || opts.preset === 'off') ? opts.preset : 'beam';
    var tintId = TINT_STOPS[opts.tint] ? opts.tint : 'cobalt';
    var dpr = opts.dpr > 0 ? opts.dpr : (window.devicePixelRatio || 1);
    var wCss = opts.width > 0 ? opts.width : (canvas.clientWidth || 300);
    var hCss = opts.height > 0 ? opts.height : (canvas.clientHeight || 150);
    var W = Math.max(1, Math.round(wCss * dpr)), H = Math.max(1, Math.round(hCss * dpr));
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;

    var ctx = canvas.getContext('2d');
    var pal = palette(tintId);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = pal.bg;
    ctx.fillRect(0, 0, W, H);
    if (presetId === 'off') return;

    var cell = Math.max(2, Math.round(CELL_CSS * dpr));
    var half = cell / 2;
    ctx.fillStyle = floorPattern(ctx, pal, cell);
    ctx.fillRect(0, 0, W, H);

    var scene = SCENES[presetId];
    var cols = Math.ceil(W / cell), rows = Math.ceil(H / cell);
    A = W / H;
    var xs = new Float32Array(cols);
    for (var i = 0; i < cols; i++) xs[i] = ((i + 0.5) * cell) / H;
    if (scene.prep) scene.prep(xs);

    var paths = new Array(LEVELS);
    var minL = FLOOR > SKIP ? FLOOR : SKIP;   // smaller dots are already on the floor pattern
    for (var j = 0; j < rows; j++) {
      var cy = (j + 0.5) * cell;
      var y = cy / H;
      var dy = (y - 0.525) / 0.225, dy2 = dy * dy;
      for (i = 0; i < cols; i++) {
        var x = xs[i];
        var L = scene.L(x, y, i, j);
        if (!(L > minL)) continue;
        // Keep the composer zone dark: L <= 0.07 inside the ellipse
        // x 25-75%, y 30-75%, feathered outward.
        var dx = (x / A - 0.5) / 0.25;
        var s = smooth(1.0, 1.75, Math.sqrt(dx * dx + dy2));
        L *= 0.35 + 0.65 * s;
        var cap = 0.07 + 0.93 * s;
        if (L > cap) L = cap;
        if (L > 1) L = 1;
        if (L <= minL) continue;
        var b = (L * LEVELS) | 0;
        if (b >= LEVELS) b = LEVELS - 1;
        var p = paths[b] || (paths[b] = new Path2D());
        var r = half * Math.sqrt(L);
        var cx = (i + 0.5) * cell;
        p.moveTo(cx + r, cy);
        p.arc(cx, cy, r, 0, TAU);
      }
    }
    for (var k = 0; k < LEVELS; k++) {
      if (!paths[k]) continue;
      ctx.fillStyle = pal.buckets[k];
      ctx.fill(paths[k]);
    }
  }

  window.RHBackground = { presets: PRESETS, tints: TINTS, render: render };
})();
