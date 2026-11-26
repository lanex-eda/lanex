// macro-place.js — where a hard macro may sit, and why not. Pure: no DOM.
//
// The model is /api/macros/floorplan (controller/macrolib.py floorplan): the
// die, the core OpenROAD makes from it, the site grid, the power straps and
// each block's footprint and power pins. A placement is
// {macro, name, location: [x, y] | null, orientation}. The rules here are the
// server's placement_problems, word for word, so the canvas, the macro card
// and the run check say the same thing.

export const ROTATED = new Set(["E", "W", "FE", "FW"]);
export const ORIENT_WORDS = {
  N: "As drawn", S: "Turned 180°", FN: "Mirrored left-right", FS: "Mirrored top-bottom",
  W: "Turned 90° left", E: "Turned 90° right", FW: "Turned and mirrored", FE: "Turned and mirrored",
};

export function footprint(w, h, o) { return ROTATED.has(o) ? [h, w] : [w, h]; }

export function xformPoint(x, y, w, h, o) {
  switch (o) {
    case "S": return [w - x, h - y];
    case "FN": return [w - x, y];
    case "FS": return [x, h - y];
    case "W": return [h - y, x];
    case "E": return [y, w - x];
    case "FW": return [y, x];
    case "FE": return [h - y, w - x];
    default: return [x, y];
  }
}

export function xformRect(r, w, h, o) {
  const [ax, ay] = xformPoint(r[0], r[1], w, h, o);
  const [bx, by] = xformPoint(r[2], r[3], w, h, o);
  return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)];
}

// A coordinate rounded to a step counted from the origin, without float dust.
export function snap(v, step) {
  if (!(step > 0)) return v;
  return Number((Math.round(v / step) * step).toFixed(4));
}

export function snapUp(v, step) {
  if (!(step > 0)) return v;
  return Number((Math.ceil(v / step - 1e-6) * step).toFixed(4));
}

// The grid a drag or a nudge lands on: "site" (site width by row height),
// "mfg" (the manufacturing grid), "off" (still the manufacturing grid: no
// coordinate is ever finer than the mask can draw).
export function gridOf(model, mode) {
  const mfg = model.mfg || 0.005;
  const s = model.site || {};
  if (mode === "site" && s.w && s.h) return [s.w, s.h];
  return [mfg, mfg];
}

export function fmtUm(v) {
  if (!Number.isFinite(v)) return "–";
  const r = Math.round(v * 1000) / 1000;
  return Math.abs(r - Math.round(r)) < 1e-9 ? String(Math.round(r)) : r.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}
const f2 = (v) => (Math.abs(v - Math.round(v)) > 1e-9 ? v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") : String(Math.round(v)));

// Bands of the core grid's straps: axis "h" = horizontal straps (y extent),
// "v" = vertical (x extent). Power first, ground one width plus spacing on.
export function straps(model, axis) {
  const core = model.core && model.core.box;
  const p = (model.pdn || {})[axis] || {};
  if (!core || !p.pitch) return [];
  const [lo, hi] = axis === "h" ? [core[1], core[3]] : [core[0], core[2]];
  const out = [];
  for (let k = 0; k < 2000; k++) {
    const a = lo + p.off + k * p.pitch;
    if (a > hi) break;
    const b = a + p.w + p.s;
    out.push(["power", a, a + p.w]);
    if (b + p.w <= hi + 1e-6) out.push(["ground", b, b + p.w]);
  }
  return out;
}

// Where horizontal straps land on the block's vertical power pins: one via
// site per crossing. Returns {power, ground, marks: [[x, y, net]]} (marks in
// die coordinates, for the canvas).
export function crossings(model, inst) {
  const g = (model.macros || {})[inst.macro] || {};
  const out = { power: 0, ground: 0, marks: [] };
  if (!g.ok || !inst.location) return out;
  const vl = ((model.pdn || {}).vlayer || "").toLowerCase();
  const hs = straps(model, "h");
  for (const r of g.prects || []) {
    if (String(r[2]).toLowerCase() !== vl) continue;
    const [x0, y0, x1, y1] = xformRect(r.slice(3, 7), g.w, g.h, inst.orientation);
    if (y1 - y0 <= x1 - x0) continue;
    const ay0 = inst.location[1] + y0, ay1 = inst.location[1] + y1;
    for (const [net, a, b] of hs) {
      if (net === r[1] && a < ay1 && b > ay0) {
        out[net] += 1;
        out.marks.push([inst.location[0] + (x0 + x1) / 2, (Math.max(a, ay0) + Math.min(b, ay1)) / 2, net]);
      }
    }
  }
  return out;
}

export function boxOf(model, p) {
  const g = (model.macros || {})[p.macro] || {};
  if (!g.ok || !p.location) return null;
  const [w, h] = footprint(g.w, g.h, p.orientation || "N");
  return [p.location[0], p.location[1], p.location[0] + w, p.location[1] + h];
}

// Every reason a macro position would fail, before a run finds it. The words
// are controller/macrolib.py placement_problems'.
export function problems(model, placements) {
  const out = [];
  const core = model.core && model.core.box;
  const site = model.site || {};
  const sw = site.w || 0, sh = site.h || 0;
  const mfg = model.mfg || 0.005;
  const halo = model.halo || { h: 10, v: 10 };
  const pdn = model.pdn || {};
  const macros = model.macros || {};
  const eps = 1e-4;
  const placed = [];
  const add = (code, hard, p, message, fix = "") => out.push({ code, hard, inst: p.name, macro: p.macro, message, fix });
  for (const p of placements || []) {
    const g = macros[p.macro] || {};
    if (!g.ok) continue;
    if (!p.location) {
      add("unplaced", true, p, p.name + " has no position yet. LibreLane places a macro only where you put it, so the run would stop at the power grid (PDN-0235 unplaced macros).",
        "Place it on the Cells & macros tab (Macros, Arrange for me).");
      continue;
    }
    const o = p.orientation || "N";
    const box = boxOf(model, p);
    const [x, y] = p.location;
    placed.push([p, box]);
    if (!(g.orients || []).includes(o)) {
      if (ROTATED.has(o)) add("orient", true, p, p.name + " is turned 90° (" + o + "). Its " + pdn.vlayer + " power pins then run beside the " + pdn.hlayer +
        " straps, so no strap can reach them (the run would stop with PDN-0233).", "Use N, S, FN or FS.");
      else add("orient", true, p, p.name + "'s LEF doesn't allow orientation " + o + " (SYMMETRY " + (g.symmetry || "none") + ").",
        "Use " + (g.orients || ["N"]).join(", ") + ".");
    }
    if (core) {
      const sides = [];
      if (box[0] < core[0] - eps) sides.push(f2(core[0] - box[0]) + " µm on the left (the core starts at x = " + f2(core[0]) + " µm)");
      if (box[1] < core[1] - eps) sides.push(f2(core[1] - box[1]) + " µm at the bottom (the core starts at y = " + f2(core[1]) + " µm)");
      if (box[2] > core[2] + eps) sides.push(f2(box[2] - core[2]) + " µm on the right (the core ends at x = " + f2(core[2]) + " µm)");
      if (box[3] > core[3] + eps) sides.push(f2(box[3] - core[3]) + " µm at the top (the core ends at y = " + f2(core[3]) + " µm)");
      if (sides.length) add("outside", true, p, p.name + " is outside the core by " + sides.join(" and ") + ". Macros must sit inside the core, where the rows and the power grid are.",
        "Move it inside the core outline.");
    }
    if (sw && sh && (Math.abs(x / sw - Math.round(x / sw)) > 1e-4 || Math.abs(y / sh - Math.round(y / sh)) > 1e-4)) {
      add("grid", false, p, p.name + " is off the site grid (" + f2(sw) + " × " + f2(sh) + " µm). LibreLane places it where you say, so its pins may land between routing tracks.",
        "Snap it to x = " + f2(Math.round(x / sw) * sw) + ", y = " + f2(Math.round(y / sh) * sh) + " µm.");
    } else if (mfg && (Math.abs(x / mfg - Math.round(x / mfg)) > 1e-3 || Math.abs(y / mfg - Math.round(y / mfg)) > 1e-3)) {
      add("mfg", false, p, p.name + " is off the " + f2(mfg * 1000) + " nm manufacturing grid.", "Snap it to the grid.");
    }
    if (g.grid === "default" && !ROTATED.has(o)) {
      const c = crossings(model, p);
      const miss = ["power", "ground"].filter((n) => c[n] === 0);
      if (miss.length) add("power", true, p, "No " + pdn.hlayer + " power strap crosses " + p.name + "'s " +
        miss.map((n) => (n === "power" ? "VDD" : "ground")).join(" or ") + " pins, so it would get no power (the run would stop with PDN-0233). The straps are " +
        f2((pdn.h || {}).pitch || 0) + " µm apart.", "Move it up or down until a strap crosses it.");
    } else if (g.grid === "none") {
      add("power", true, p, "LibreLane's default power grid can't reach " + p.macro + "'s power pins (on " + (g.power_layers || []).join(", ") +
        "), and LanEx doesn't know a grid for this block.", "Write a PDN_CFG with a grid for it and set PDN_CFG in your config.");
    }
  }
  for (let a = 0; a < placed.length; a++) {
    for (let b = a + 1; b < placed.length; b++) {
      const [ia, ba] = placed[a], [ib, bb] = placed[b];
      if (ba[0] < bb[2] - eps && bb[0] < ba[2] - eps && ba[1] < bb[3] - eps && bb[1] < ba[3] - eps) {
        add("overlap", true, ia, ia.name + " overlaps " + ib.name + ". Macros can't overlap.", "Move one of them.");
      }
    }
  }
  if (core && sw) {
    const { okW, cellW, goneW } = channelWidths(model);
    for (const [p, box] of placed) {
      const hb = [box[0] - halo.h, box[1] - halo.v, box[2] + halo.h, box[3] + halo.v];
      const gaps = [["left", hb[0] - core[0]], ["right", core[2] - hb[2]]];
      for (const [q, ob] of placed) {
        if (q === p) continue;
        const ohb = [ob[0] - halo.h, ob[1] - halo.v, ob[2] + halo.h, ob[3] + halo.v];
        if (ohb[1] < hb[3] && hb[1] < ohb[3] && ohb[0] >= hb[2]) gaps.push([q.name + " on the right", ohb[0] - hb[2]]);
      }
      for (const [side, gap] of gaps) {
        if (gap > goneW && gap < cellW) {
          add("channel", false, p, p.name + " leaves a " + f2(gap) + " µm strip of cell rows between its keep-out and " +
            (side === "left" || side === "right" ? "the " + side + " core edge" : side) + ". " +
            (gap < okW ? "Power straps can't reach rows that narrow, so the power grid may stop with PDN-0179."
              : "The placer puts buffers there, next to the block's pins, and a strip that narrow may not fit them (DPL-0036 detailed placement failed)."),
            "Close the gap, or leave at least " + f2(cellW) + " µm.");
        }
      }
    }
  }
  return out;
}

// How wide a strip of rows beside a macro must be: under goneW the rows
// vanish (narrower than two end caps); under okW no power strap pair fits
// (PDN-0179); under cellW the placer's buffers by the block's pins may not
// fit (a 7 µm strip failed detailed placement on a real sky130 run).
export function channelWidths(model) {
  const vp = ((model.pdn || {}).v) || {};
  const sw = (model.site || {}).w || 0.46;
  const okW = 2 * (vp.w || 1.6) + (vp.s || 1.7) + 2.0;
  return { okW, cellW: Math.max(okW, 20), goneW: 2 * (model.endcap_w || 3 * sw) };
}

// The nearest spot to a placement where none of the hard rules fire, on the
// site grid, searching outward ring by ring. null when there is none.
export function nearestLegal(model, placements, name) {
  const core = model.core && model.core.box;
  const i = placements.findIndex((p) => p.name === name);
  if (!core || i < 0) return null;
  const p = placements[i];
  const g = (model.macros || {})[p.macro] || {};
  if (!g.ok) return null;
  const [sw, sh] = gridOf(model, "site");
  const o = (g.orients || ["N"]).includes(p.orientation) ? p.orientation : (g.orients || ["N"])[0];
  const [w, h] = footprint(g.w, g.h, o);
  const start = p.location || [core[0], core[1]];
  const hardAt = (x, y) => {
    const test = placements.map((q, k) => (k === i ? Object.assign({}, q, { location: [x, y], orientation: o }) : q));
    return problems(model, test).some((pr) => pr.hard && pr.inst === name);
  };
  const cx = (x) => Math.min(Math.max(x, core[0]), core[2] - w), cy = (y) => Math.min(Math.max(y, core[1]), core[3] - h);
  const x0 = snapUp(cx(start[0]), sw), y0 = snapUp(cy(start[1]), sh);
  const stepX = Math.max(sw, snap((core[2] - core[0]) / 80, sw)), stepY = Math.max(sh, snap((core[3] - core[1]) / 80, sh));
  for (let r = 0; r <= 80; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
        const x = snap(cx(x0 + dx * stepX), sw), y = snap(cy(y0 + dy * stepY), sh);
        if (x < core[0] - 1e-6 || y < core[1] - 1e-6) continue;
        if (!hardAt(x, y)) return { location: [x, y], orientation: o };
      }
    }
  }
  return null;
}

// "Arrange for me": every placement without a position (and every one not
// in *locked* when all is true) gets one, biggest block first, on the site
// grid, with its keep-out inside the core, crossed by power straps, and as
// far as it can be from the core edges and the other blocks (the narrowest
// channel of rows as wide as possible): a strip of rows too narrow for the
// placer's buffers fails detailed placement, and one too narrow for a power
// strap fails the power grid. Returns {placements, placed, failed}.
export function arrange(model, placements, { all = false, locked = new Set() } = {}) {
  const core = model.core && model.core.box;
  const out = placements.map((p) => Object.assign({}, p));
  if (!core) return { placements: out, placed: [], failed: out.filter((p) => !p.location).map((p) => p.name) };
  const halo = model.halo || { h: 10, v: 10 };
  const { cellW, goneW } = channelWidths(model);
  const [sw, sh] = gridOf(model, "site");
  const todo = out.filter((p) => (!p.location || (all && !locked.has(p.name))) && ((model.macros || {})[p.macro] || {}).ok);
  for (const p of todo) p.location = null;
  // keep-out rectangles of the blocks already placed
  const keep = out.filter((p) => p.location && boxOf(model, p)).map((p) => {
    const b = boxOf(model, p);
    return [b[0] - halo.h, b[1] - halo.v, b[2] + halo.h, b[3] + halo.v];
  });
  const size = (p) => { const g = model.macros[p.macro]; return g.w * g.h; };
  todo.sort((a, b) => size(b) - size(a));
  const placedNames = [], failed = [];
  const W = core[2] - core[0], H = core[3] - core[1];
  const stepX = Math.max(sw, snap(W / 140, sw)), stepY = Math.max(sh, snap(H / 140, sh));
  const narrow = (g) => g > goneW && g < cellW;
  for (const p of todo) {
    const g = model.macros[p.macro];
    const o = (g.orients || ["N"]).includes(p.orientation) ? p.orientation : (g.orients || ["N"])[0];
    p.orientation = o;
    const [w, h] = footprint(g.w, g.h, o);
    let best = null;
    for (let y = snapUp(core[1] + halo.v, sh); y + h + halo.v <= core[3] + 1e-6; y = snap(y + stepY, sh)) {
      for (let x = snapUp(core[0] + halo.h, sw); x + w + halo.h <= core[2] + 1e-6; x = snap(x + stepX, sw)) {
        const k = [x - halo.h, y - halo.v, x + w + halo.h, y + h + halo.v];
        // the channels this keep-out leaves: to the four core edges, and to each placed block
        const gaps = [k[0] - core[0], core[2] - k[2], k[1] - core[1], core[3] - k[3]];
        let ok = !narrow(gaps[0]) && !narrow(gaps[1]);
        for (const q of keep) {
          const dx = Math.max(q[0] - k[2], k[0] - q[2]), dy = Math.max(q[1] - k[3], k[1] - q[3]);
          if (dx < 0 && dy < 0) { ok = false; break; }            // keep-outs overlap
          if (dy < 0 && narrow(dx)) { ok = false; break; }         // a narrow strip of rows between them
          gaps.push(Math.max(dx, dy));
        }
        if (!ok) continue;
        if (g.grid === "default") {
          const c = crossings(model, { macro: p.macro, name: p.name, location: [x, y], orientation: o });
          if (!c.power || !c.ground) continue;
        }
        const score = Math.min(...gaps);
        if (!best || score > best.score + 1e-9) best = { score, x, y };
      }
    }
    if (best) {
      p.location = [best.x, best.y];
      keep.push([best.x - halo.h, best.y - halo.v, best.x + w + halo.h, best.y + h + halo.v]);
      placedNames.push(p.name);
    } else failed.push(p.name);
  }
  return { placements: out, placed: placedNames, failed };
}

// The placements from a floorplan model, as the canvas edits them.
export function placementsOf(model) {
  return (model.instances || []).map((i) => ({ macro: i.macro, name: i.name, location: i.location ? [Number(i.location[0]), Number(i.location[1])] : null, orientation: i.orientation || "N" }));
}

export function samePlacements(a, b) {
  if (a.length !== b.length) return false;
  const key = (p) => p.macro + "\u0000" + p.name + "\u0000" + (p.location ? p.location.join(",") : "-") + "\u0000" + p.orientation;
  const A = a.map(key).sort(), B = b.map(key).sort();
  return A.every((k, i) => k === B[i]);
}
