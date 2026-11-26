// floorplan-canvas.js — a die drawn to scale, with macros you can place.
//
// Reusable: give it a floorplan model (/api/macros/floorplan) and placements
// ({macro, name, location, orientation}); it draws the die, the pin and ring
// margin, the core with its lit edge, the cell rows, the power straps, every
// macro with its keep-out ring, power pins and the vias where a strap lands on
// them, and lets you drag, nudge and turn macros on the site grid. Legality
// comes from macro-place.js, so the canvas and the server always agree.
//
//   const fp = createFloorplanCanvas(host, { editable, onChange, onSelect, onCursor, onEnter });
//   fp.set(model, placements); fp.select(name); fp.fit(); fp.zoomBy(1.25);
//   fp.setSnap("site" | "mfg"); fp.setShow({ rows, power, keepout, pins });
//   fp.placements(); fp.destroy();
//
// Keyboard (a macro has focus): arrows move one site or row, Shift+arrows
// ten, Alt+arrows one manufacturing-grid step; R turns it 180°, F mirrors it
// left-right, V top-bottom; Enter asks for the X field (onEnter); Esc
// cancels a drag. Wheel zooms around the pointer; drag the empty die to pan.

import { footprint, xformRect, xformPoint, straps, crossings, snap, gridOf, fmtUm, problems, ROTATED, ORIENT_WORDS } from "./macro-place.js";

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let _uid = 0;

export function createFloorplanCanvas(host, opts = {}) {
  const id = "fpc" + (++_uid);
  const st = {
    model: null, pl: [], sel: null, view: null, snap: "site",
    show: { rows: true, power: true, keepout: true, pins: true },
    drag: null, pan: null, raf: 0, probs: [], focusName: null,
  };
  host.classList.add("fpc");
  host.innerHTML = "<svg class='fpc-svg' xmlns='http://www.w3.org/2000/svg' role='group' aria-label='Floorplan, drawn to scale'></svg>" +
    "<div class='fpc-scalebar' aria-hidden='true'><i></i><span></span></div>";
  const svg = host.querySelector("svg");
  const bar = host.querySelector(".fpc-scalebar");

  // ---------------------------------------------------------- geometry ----
  const dieBox = () => (st.model && st.model.die && st.model.die.box) || null;
  function fitView() {
    const d = dieBox();
    if (!d) return null;
    const W = d[2] - d[0], H = d[3] - d[1], pad = Math.max(W, H) * 0.04;
    return [d[0] - pad, -(d[3] + pad), W + 2 * pad, H + 2 * pad];
  }
  // pixels per micrometre at the current view (viewBox "meet")
  function scale() {
    const v = st.view, r = svg.getBoundingClientRect();
    if (!v || !r.width || !r.height) return 1;
    return Math.min(r.width / v[2], r.height / v[3]);
  }
  function toWorld(e) {
    const p = svg.createSVGPoint();
    p.x = e.clientX; p.y = e.clientY;
    const m = svg.getScreenCTM();
    if (!m) return [0, 0];
    const q = p.matrixTransform(m.inverse());
    return [q.x, -q.y];
  }

  // ---------------------------------------------------------- drawing -----
  function schedule() {
    if (st.raf) return;
    st.raf = requestAnimationFrame(() => { st.raf = 0; draw(); });
  }

  function draw() {
    const M = st.model, d = dieBox();
    if (!M || !d) { svg.innerHTML = ""; bar.hidden = true; return; }
    if (!st.view) st.view = fitView();
    const v = st.view;
    svg.setAttribute("viewBox", v.join(" "));
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const k = scale();
    const px = (n) => n / k;                 // n screen pixels in µm
    const core = M.core && M.core.box;
    const site = M.site || {};
    const halo = M.halo || { h: 10, v: 10 };
    st.probs = problems(M, st.pl);
    const worst = (name) => {
      const ps = st.probs.filter((p) => p.inst === name);
      return ps.some((p) => p.hard) ? "fail" : ps.length ? "warn" : "ok";
    };
    const parts = [];
    parts.push("<defs>" +
      "<pattern id='" + id + "-hatch' patternUnits='userSpaceOnUse' width='" + px(7) + "' height='" + px(7) + "' patternTransform='rotate(45)'>" +
        "<rect width='" + px(1) + "' height='" + px(7) + "' class='fpc-hatch-l'/></pattern>" +
      "<pattern id='" + id + "-fail' patternUnits='userSpaceOnUse' width='" + px(6) + "' height='" + px(6) + "' patternTransform='rotate(45)'>" +
        "<rect width='" + px(1.5) + "' height='" + px(6) + "' class='fpc-fail-l'/></pattern>" +
      (core && site.h ? "<pattern id='" + id + "-rows' patternUnits='userSpaceOnUse' x='" + core[0] + "' y='" + (-core[3]) + "' width='" + (core[2] - core[0]) + "' height='" + 2 * site.h + "'>" +
        "<rect width='" + (core[2] - core[0]) + "' height='" + site.h + "' class='fpc-row-a'/>" +
        (site.h * k >= 5 ? "<rect width='" + (core[2] - core[0]) + "' height='" + px(1) + "' class='fpc-row-l'/><rect y='" + site.h + "' width='" + (core[2] - core[0]) + "' height='" + px(1) + "' class='fpc-row-l'/>" : "") +
        "</pattern>" : "") +
      "</defs>");
    // die and margin
    parts.push("<rect class='fpc-die' x='" + d[0] + "' y='" + (-d[3]) + "' width='" + (d[2] - d[0]) + "' height='" + (d[3] - d[1]) + "'/>");
    if (core) {
      parts.push("<path class='fpc-margin' fill='url(#" + id + "-hatch)' fill-rule='evenodd' d='M" + d[0] + " " + (-d[3]) + "h" + (d[2] - d[0]) + "v" + (d[3] - d[1]) + "h" + (-(d[2] - d[0])) + "z" +
        "M" + core[0] + " " + (-core[3]) + "h" + (core[2] - core[0]) + "v" + (core[3] - core[1]) + "h" + (-(core[2] - core[0])) + "z'/>");
      if (st.show.rows) parts.push("<rect class='fpc-rows' fill='url(#" + id + "-rows)' x='" + core[0] + "' y='" + (-core[3]) + "' width='" + (core[2] - core[0]) + "' height='" + (core[3] - core[1]) + "'/>");
      // keep-outs cut the rows: drawn under the straps and macros
      if (st.show.keepout) {
        for (const p of st.pl) {
          const b = boxOfP(p);
          if (!b) continue;
          parts.push("<rect class='fpc-ko' x='" + (b[0] - halo.h) + "' y='" + (-(b[3] + halo.v)) + "' width='" + (b[2] - b[0] + 2 * halo.h) + "' height='" + (b[3] - b[1] + 2 * halo.v) + "'/>");
        }
      }
      if (st.show.power) {
        const vs = straps(M, "v"), hs = straps(M, "h");
        for (const [net, a, b] of vs) parts.push("<line class='fpc-sv is-" + net + "' x1='" + (a + b) / 2 + "' x2='" + (a + b) / 2 + "' y1='" + (-core[1]) + "' y2='" + (-core[3]) + "' style='stroke-width:" + Math.max(1, (b - a) * k).toFixed(2) + "px'/>");
        for (const [net, a, b] of hs) parts.push("<line class='fpc-sh is-" + net + "' y1='" + (-(a + b) / 2) + "' y2='" + (-(a + b) / 2) + "' x1='" + core[0] + "' x2='" + core[2] + "' style='stroke-width:" + Math.max(1, (b - a) * k).toFixed(2) + "px'/>");
      }
      parts.push("<rect class='fpc-core' x='" + core[0] + "' y='" + (-core[3]) + "' width='" + (core[2] - core[0]) + "' height='" + (core[3] - core[1]) + "'/>");
      parts.push(text(core[0] + px(6), core[3] - px(14), "CORE " + fmtUm(core[2] - core[0]) + " × " + fmtUm(core[3] - core[1]) + " µm", "fpc-cap", px(10.5)));
    }
    // ghost of a drag
    if (st.drag && st.drag.from && st.drag.moved) {
      const p = st.pl.find((q) => q.name === st.drag.name);
      const g = p && M.macros[p.macro];
      if (g && g.ok) {
        const [w, h] = footprint(g.w, g.h, p.orientation);
        parts.push("<rect class='fpc-ghost' x='" + st.drag.from[0] + "' y='" + (-(st.drag.from[1] + h)) + "' width='" + w + "' height='" + h + "'/>");
      }
    }
    // macros
    for (const p of st.pl) {
      const g = M.macros[p.macro];
      const b = boxOfP(p);
      if (!b || !g || !g.ok) continue;
      const w = b[2] - b[0], h = b[3] - b[1];
      const state = worst(p.name);
      const sel = st.sel === p.name;
      const label = p.name + ", " + p.macro + ", at " + fmtUm(b[0]) + ", " + fmtUm(b[1]) + " micrometres, " + p.orientation + " (" + (ORIENT_WORDS[p.orientation] || "") + "), " +
        (state === "ok" ? "no problems" : state === "warn" ? "a warning" : "a problem");
      const inner = [];
      inner.push("<rect class='fpc-m-body' x='" + b[0] + "' y='" + (-b[3]) + "' width='" + w + "' height='" + h + "'/>");
      if (state === "fail") inner.push("<rect class='fpc-m-failfill' fill='url(#" + id + "-fail)' x='" + b[0] + "' y='" + (-b[3]) + "' width='" + w + "' height='" + h + "'/>");
      const vl = ((M.pdn || {}).vlayer || "").toLowerCase();
      if (st.show.power) {
        for (const r of g.prects || []) {
          if (String(r[2]).toLowerCase() !== vl) continue;
          const t = xformRect(r.slice(3, 7), g.w, g.h, p.orientation);
          inner.push("<rect class='fpc-pp is-" + r[1] + "' x='" + (b[0] + t[0]) + "' y='" + (-(b[1] + t[3])) + "' width='" + Math.max(t[2] - t[0], px(1)) + "' height='" + Math.max(t[3] - t[1], px(1)) + "'/>");
        }
        if (g.grid === "default" && !ROTATED.has(p.orientation)) {
          const s = px(5);
          for (const [x, y, net] of crossings(M, p).marks) inner.push("<rect class='fpc-via is-" + net + "' x='" + (x - s / 2) + "' y='" + (-(y + s / 2)) + "' width='" + s + "' height='" + s + "'/>");
        }
      }
      if (st.show.pins && (g.pins || []).length && w * k > 40) {
        const s = px(2.2);
        for (const [x, y] of g.pins) {
          const [tx, ty] = xformPoint(x, y, g.w, g.h, p.orientation);
          inner.push("<rect class='fpc-pin' x='" + (b[0] + tx - s / 2) + "' y='" + (-(b[1] + ty + s / 2)) + "' width='" + s + "' height='" + s + "'/>");
        }
      }
      if (w * k > 70 && h * k > 28) {
        inner.push(text(b[0] + px(8), b[3] - px(16), p.name, "fpc-m-name", px(13)));
        if (h * k > 46) inner.push(text(b[0] + px(8), b[3] - px(31), p.macro + " · " + p.orientation, "fpc-m-sub", px(10.5)));
      }
      parts.push("<g class='fpc-m is-" + state + (sel ? " is-sel" : "") + (opts.editable ? " is-edit" : "") + "' data-inst='" + esc(p.name) + "' tabindex='0' role='button' aria-pressed='" + (sel ? "true" : "false") +
        "' aria-label='" + esc(label) + "'>" + inner.join("") + "</g>");
    }
    // distance labels while dragging or for the selection
    const focus = st.drag ? st.drag.name : st.sel;
    if (core && focus) {
      const p = st.pl.find((q) => q.name === focus);
      const b = p && boxOfP(p);
      if (b) parts.push(dims(b, core, k, px));
    }
    svg.innerHTML = parts.join("");
    // scale bar: a round length about 90 px long
    const um = niceLen(90 / k);
    bar.hidden = false;
    bar.querySelector("i").style.width = (um * k).toFixed(1) + "px";
    bar.querySelector("span").textContent = fmtUm(um) + " µm";
    if (st.focusName) {
      const el = svg.querySelector("[data-inst='" + cssEsc(st.focusName) + "']");
      if (el) el.focus({ preventScroll: true });
      st.focusName = null;
    }
  }

  function boxOfP(p) {
    const g = st.model && st.model.macros[p.macro];
    if (!g || !g.ok || !p.location) return null;
    const [w, h] = footprint(g.w, g.h, p.orientation);
    return [p.location[0], p.location[1], p.location[0] + w, p.location[1] + h];
  }

  function text(x, y, s, cls, size) {
    return "<text class='" + cls + "' x='" + x + "' y='" + (-y) + "' style='font-size:" + size.toFixed(3) + "px'>" + esc(s) + "</text>";
  }

  function dims(b, core, k, px) {
    const out = [];
    const line = (x1, y1, x2, y2) => "<line class='fpc-dim' x1='" + x1 + "' y1='" + (-y1) + "' x2='" + x2 + "' y2='" + (-y2) + "'/>";
    const tag = (x, y, s) => {
      const w = px(8 + s.length * 6.4), h = px(16);
      return "<g class='fpc-dimtag'><rect x='" + (x - w / 2) + "' y='" + (-y - h / 2) + "' width='" + w + "' height='" + h + "' rx='" + px(3) + "'/>" +
        "<text x='" + x + "' y='" + (-y) + "' style='font-size:" + px(10.5).toFixed(3) + "px'>" + esc(s) + "</text></g>";
    };
    const cy = (b[1] + b[3]) / 2, cx = (b[0] + b[2]) / 2;
    const L = b[0] - core[0], R = core[2] - b[2], B = b[1] - core[1], T = core[3] - b[3];
    if (L * k > 24) out.push(line(core[0], cy, b[0], cy) + tag((core[0] + b[0]) / 2, cy, fmtUm(L)));
    if (R * k > 24) out.push(line(b[2], cy, core[2], cy) + tag((b[2] + core[2]) / 2, cy, fmtUm(R)));
    if (B * k > 24) out.push(line(cx, core[1], cx, b[1]) + tag(cx, (core[1] + b[1]) / 2, fmtUm(B)));
    if (T * k > 24) out.push(line(cx, b[3], cx, core[3]) + tag(cx, (b[3] + core[3]) / 2, fmtUm(T)));
    return "<g class='fpc-dims' aria-hidden='true'>" + out.join("") + "</g>";
  }

  // ---------------------------------------------------------- editing -----
  function emit(final) {
    if (opts.onChange) opts.onChange(st.pl.map((p) => Object.assign({}, p, { location: p.location ? p.location.slice() : null })), st.probs, { final });
  }
  function setLoc(name, loc, orient) {
    const p = st.pl.find((q) => q.name === name);
    if (!p) return;
    if (loc) p.location = [Number(loc[0].toFixed(4)), Number(loc[1].toFixed(4))];
    if (orient) {
      // turning keeps the block's centre where it was, on the grid
      const g = st.model.macros[p.macro];
      if (p.location && g && g.ok) {
        const [w0, h0] = footprint(g.w, g.h, p.orientation), [w1, h1] = footprint(g.w, g.h, orient);
        const [gx, gy] = gridOf(st.model, st.snap);
        p.location = [snap(p.location[0] + (w0 - w1) / 2, gx), snap(p.location[1] + (h0 - h1) / 2, gy)];
      }
      p.orientation = orient;
    }
  }
  function clampToDie(p, x, y) {
    const d = dieBox(), g = st.model.macros[p.macro];
    const [w, h] = footprint(g.w, g.h, p.orientation);
    return [Math.min(Math.max(x, d[0]), d[2] - w), Math.min(Math.max(y, d[1]), d[3] - h)];
  }
  function toggleOrient(p, key) {
    const g = st.model.macros[p.macro];
    const allowed = (g && g.orients) || ["N"];
    const pairs = { r: { N: "S", S: "N", FN: "FS", FS: "FN" }, f: { N: "FN", FN: "N", S: "FS", FS: "S" }, v: { N: "FS", FS: "N", S: "FN", FN: "S" } };
    const next = (pairs[key] || {})[p.orientation];
    return next && allowed.includes(next) ? next : null;
  }

  svg.addEventListener("pointerdown", (e) => {
    if (!st.model || e.button !== 0) return;
    const g = e.target.closest("[data-inst]");
    const [x, y] = toWorld(e);
    if (g) {
      const name = g.dataset.inst;
      select(name, true);
      if (!opts.editable) return;
      const p = st.pl.find((q) => q.name === name);
      if (!p || !p.location) return;
      st.drag = { name, from: p.location.slice(), dx: x - p.location[0], dy: y - p.location[1], moved: false, id: e.pointerId };
      svg.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    st.pan = { x: e.clientX, y: e.clientY, view: st.view.slice(), id: e.pointerId };
    svg.setPointerCapture(e.pointerId);
    svg.classList.add("is-panning");
  });
  svg.addEventListener("pointermove", (e) => {
    if (!st.model) return;
    const [x, y] = toWorld(e);
    if (opts.onCursor) opts.onCursor(x, y);
    if (st.drag) {
      const p = st.pl.find((q) => q.name === st.drag.name);
      const [gx, gy] = gridOf(st.model, e.altKey ? "mfg" : st.snap);
      let nx = snap(x - st.drag.dx, gx), ny = snap(y - st.drag.dy, gy);
      [nx, ny] = clampToDie(p, nx, ny);
      if (!st.drag.moved && Math.abs(nx - st.drag.from[0]) < 1e-9 && Math.abs(ny - st.drag.from[1]) < 1e-9) return;
      st.drag.moved = true;
      setLoc(p.name, [nx, ny]);
      schedule();
      emit(false);
      return;
    }
    if (st.pan) {
      const k = scale();
      const v = st.pan.view;
      st.view = [v[0] - (e.clientX - st.pan.x) / k, v[1] - (e.clientY - st.pan.y) / k, v[2], v[3]];
      schedule();
    }
  });
  const endPointer = () => {
    if (st.drag) {
      const moved = st.drag.moved;
      st.drag = null;
      draw();
      if (moved) emit(true);
    }
    if (st.pan) { st.pan = null; svg.classList.remove("is-panning"); }
  };
  svg.addEventListener("pointerup", endPointer);
  svg.addEventListener("pointercancel", endPointer);
  svg.addEventListener("wheel", (e) => {
    if (!st.view) return;
    e.preventDefault();
    const [x, y] = toWorld(e);
    zoomAround(e.deltaY < 0 ? 1.18 : 1 / 1.18, x, -y);
  }, { passive: false });
  svg.addEventListener("keydown", (e) => {
    const gEl = e.target.closest && e.target.closest("[data-inst]");
    if (!gEl || !st.model) return;
    const p = st.pl.find((q) => q.name === gEl.dataset.inst);
    if (!p) return;
    if (e.key === "Escape" && st.drag) {
      setLoc(p.name, st.drag.from);
      st.drag = null; draw(); emit(true);
      e.preventDefault();
      return;
    }
    if (e.key === "Enter" || e.key === " ") { select(p.name, true); if (e.key === "Enter" && opts.onEnter) opts.onEnter(p.name); e.preventDefault(); return; }
    if (!opts.editable || !p.location) return;
    const dirs = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
    if (dirs[e.key]) {
      const [gx, gy] = gridOf(st.model, e.altKey ? "mfg" : "site");
      const n = e.shiftKey ? 10 : 1;
      let nx = snap(p.location[0] + dirs[e.key][0] * gx * n, gx), ny = snap(p.location[1] + dirs[e.key][1] * gy * n, gy);
      [nx, ny] = clampToDie(p, nx, ny);
      setLoc(p.name, [nx, ny]);
      st.focusName = p.name; st.sel = p.name;
      draw(); emit(true);
      e.preventDefault();
      return;
    }
    const key = e.key.toLowerCase();
    if ((key === "r" || key === "f" || key === "v") && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const next = toggleOrient(p, key);
      if (next) { setLoc(p.name, null, next); st.focusName = p.name; draw(); emit(true); }
      e.preventDefault();
    }
  });

  function select(name, notify) {
    st.sel = name;
    draw();
    if (notify && opts.onSelect) opts.onSelect(name);
  }

  function zoomAround(f, wx, wy) {
    const v = st.view;
    const nw = v[2] / f, nh = v[3] / f;
    const fv = fitView();
    if (fv && (nw > fv[2] * 3 || nw < fv[2] / 400)) return;
    st.view = [wx - (wx - v[0]) / f, wy - (wy - v[1]) / f, nw, nh];
    schedule();
  }

  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => schedule()) : null;
  if (ro) ro.observe(host);

  return {
    set(model, placements) {
      const keepView = st.model && model && st.model.die && model.die && JSON.stringify(st.model.die.box) === JSON.stringify(model.die.box);
      st.model = model;
      st.pl = (placements || []).map((p) => Object.assign({}, p, { location: p.location ? p.location.slice() : null }));
      if (!keepView) st.view = null;
      draw();
      return st.probs;
    },
    select(name) { select(name, false); },
    focus(name) { st.focusName = name; st.sel = name; draw(); },
    fit() { st.view = fitView(); draw(); },
    zoomBy(f) {
      if (!st.view) return;
      const v = st.view;
      zoomAround(f, v[0] + v[2] / 2, v[1] + v[3] / 2);
    },
    setSnap(mode) { st.snap = mode; },
    setShow(show) { Object.assign(st.show, show); draw(); },
    placements() { return st.pl.map((p) => Object.assign({}, p)); },
    problems() { return st.probs.slice(); },
    destroy() { if (ro) ro.disconnect(); if (st.raf) cancelAnimationFrame(st.raf); host.innerHTML = ""; host.classList.remove("fpc"); },
  };
}

function niceLen(raw) {
  if (!(raw > 0)) return 1;
  const e = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / e;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * e;
}

function cssEsc(s) {
  return (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape(s) : String(s).replace(/["'\\\]\[]/g, "\\$&");
}
