// schematic.js — the RTL IDE's Schematic tab: what the code builds, one
// module at a time, drawn left to right (inputs on the left, outputs on the
// right, a bus as one thick wire with its width). Yosys reads the design as
// synthesis does (controller/schematic.py); this file turns its JSON netlist
// into a graph, lays it out (schlayout.js, in a worker for big modules) and
// draws it as SVG in the LanEx look: blueprint grid, cells on the surface
// tone with blue borders, ports as small square vias, the selected net lit.
//
// Click a cell: what it is and where it comes from, with Go to source.
// Double-click a submodule to open it; the path above goes back. A module
// over CELL_LIMIT cells opens on its ports with a signal finder: pick a
// signal to draw what drives it (its fan-in, back to registers and inputs).
//
//   initSchematic(ctx)   ctx = { openAt(rel, line, col, len), note(msg, bad), setPanel(name), top() }
import { api, fmt } from "../api.js";
import { layoutGraph } from "./schlayout.js";
import { rank } from "./fuzzy.js";

const esc = (s) => fmt.escape(String(s === undefined || s === null ? "" : s));
export const CELL_LIMIT = 1000;
const CONE_LIMIT = 150;
const CH = 6.7;            // mono 11 px, per character
const NS = "http://www.w3.org/2000/svg";

const OPS = {
  $add: "+", $sub: "−", $mul: "×", $div: "÷", $mod: "%", $pow: "**", $and: "&", $or: "|", $xor: "^", $xnor: "^~",
  $eq: "==", $ne: "!=", $eqx: "===", $nex: "!==", $lt: "<", $le: "≤", $gt: ">", $ge: "≥", $shl: "<<", $shr: ">>",
  $sshl: "<<<", $sshr: ">>>", $shift: ">>", $shiftx: ">>", $logic_and: "&&", $logic_or: "||", $logic_not: "!", $not: "~",
  $neg: "−", $pos: "+", $reduce_and: "&", $reduce_or: "|", $reduce_xor: "^", $reduce_xnor: "^~", $reduce_bool: "|",
  $_AND_: "&", $_OR_: "|", $_XOR_: "^", $_NOT_: "~", $_NAND_: "~&", $_NOR_: "~|", $_XNOR_: "^~", $_ANDNOT_: "&~", $_ORNOT_: "|~",
  $_BUF_: "▷",
};
const FF = /^\$(_?(s|a|ad|al|sdff|sdffe|sdffce|dffe|adffe|aldffe|dffsr|dffsre|dff|dlatch|adlatch|sr|ff)[A-Z0-9_]*_?|_DFF.*|_SDFF.*|_DLATCH.*)$/i;
const MUX = /^\$(mux|pmux|bmux|_MUX_|_NMUX_|_MUX4_|_MUX8_|_MUX16_)$/;

export function isRegister(type) { return /^\$(dff|adff|sdff|dffe|adffe|sdffe|sdffce|aldff|aldffe|dffsr|dffsre|dlatch|adlatch|sr|_DFF|_SDFF|_DFFE|_DLATCH)/i.test(type); }

// "$paramod\adder\W=s32'00000000000000000000000000001000" -> "adder W=8"
export function moduleLabel(name, mod) {
  const hdl = mod && mod.attributes && mod.attributes.hdlname;
  const m = String(name).match(/^\$paramod\\([^\\]+)\\?(.*)$/) || String(name).match(/^\$paramod\$[0-9a-f]+\\(.+)$/);
  if (!m) return hdl || String(name).replace(/^\\/, "");
  const params = (m[2] || "").split("\\").filter(Boolean).map((p) => {
    const q = p.match(/^([^=]+)=(?:s?(\d+)')?([01xz]+)$/i);
    if (!q) return p;
    const bits = q[3];
    return q[1] + "=" + (/^[01]+$/.test(bits) && bits.length <= 52 ? parseInt(bits, 2) : bits);
  });
  return (hdl || m[1]) + (params.length ? " " + params.join(" ") : "");
}

// "src/top.v:21.13-21.16|src/top.v:21.9-22.29" -> { rel, line, col, len }
export function srcOf(attr) {
  const first = String(attr || "").split("|")[0];
  const m = first.match(/^(.*?):(\d+)(?:\.(\d+))?(?:-(\d+)(?:\.(\d+))?)?$/);
  if (!m) return null;
  const line = +m[2], col = m[3] ? +m[3] : 1;
  const len = m[5] && +m[4] === line ? Math.max(0, +m[5] - col) : 0;
  return { rel: m[1].replace(/^\/work\//, "").replace(/^\.\//, ""), line, col, len };
}

function widthText(n) { return n > 1 ? "[" + (n - 1) + ":0]" : ""; }

function cellShape(c, json) {
  const t = c.type;
  if (json && json.modules && json.modules[t]) return "inst";      // a submodule ($paramod\adder\W=… too)
  if (FF.test(t) || isRegister(t)) return "ff";
  if (MUX.test(t)) return "mux";
  if (OPS[t]) return "op";
  if (!t.startsWith("$")) return "inst";
  return "box";
}

// Yosys JSON (one module) -> the layout graph and what the drawing needs.
export function netlistToGraph(json, modName, { cone = null } = {}) {
  const mod = json.modules[modName];
  if (!mod) throw new Error("No module " + modName + " in the netlist.");
  const nodes = [], edges = [], meta = new Map();
  const driver = new Map();   // bit -> [nodeId, port]
  const netName = new Map();  // bit -> name
  for (const [name, nn] of Object.entries(mod.netnames || {})) {
    for (const b of nn.bits || []) {
      if (typeof b !== "number") continue;
      const prev = netName.get(b);
      if (!prev || (prev.hide && !nn.hide_name) || (!nn.hide_name && name.length < prev.name.length && prev.hide === !!nn.hide_name))
        netName.set(b, { name, hide: !!nn.hide_name, bits: nn.bits, src: nn.attributes && nn.attributes.src });
    }
  }
  let sort = 0;
  for (const [pname, p] of Object.entries(mod.ports || {})) {
    const id = "p:" + pname;
    const isOut = p.direction === "output";
    const label = pname + (p.bits.length > 1 ? " " + widthText(p.bits.length) : "");
    nodes.push({ id, kind: isOut ? "out" : "in", w: Math.max(40, label.length * CH + 26), h: 22, ins: isOut ? ["i"] : [], outs: isOut ? [] : ["o"], sort: sort++ });
    meta.set(id, { kind: isOut ? "out" : "in", name: pname, label, width: p.bits.length, dir: p.direction });
    if (!isOut) p.bits.forEach((b) => { if (typeof b === "number") driver.set(b, [id, "o"]); });
  }
  const cells = Object.entries(mod.cells || {}).filter(([, c]) => c.type !== "$scopeinfo");
  for (const [cname, c] of cells) {
    const id = "c:" + cname;
    const dirs = c.port_directions || {};
    const conns = c.connections || {};
    const ports = Object.keys(conns);
    const outs = ports.filter((p) => (dirs[p] || (/^(Y|Q|O|OUT|Z)$/.test(p) ? "output" : "input")) === "output");
    let ins = ports.filter((p) => !outs.includes(p));
    // clock and enable last, data first: reads like the textbook symbol
    const rank_ = (p) => (/^(CLK|C|EN|E|ARST|SRST|R|S|CLR|SET)$/.test(p) ? 2 : /^S$/.test(p) ? 1 : 0);
    ins = ins.sort((a, b) => rank_(a) - rank_(b));
    const shape = cellShape(c, json);
    const subMod = shape === "inst" ? json.modules[c.type] : null;
    const title = shape === "op" ? OPS[c.type] : shape === "mux" ? "MUX" : shape === "ff" ? (c.type.toLowerCase().includes("latch") ? "LATCH" : "DFF")
      : shape === "inst" ? cname.replace(/^\\/, "").split(".").pop()
      : c.type.replace(/^\$_?/, "").replace(/_$/, "").toUpperCase();
    const sub = shape === "inst" ? moduleLabel(c.type, subMod) : "";
    const n = Math.max(ins.length, outs.length, 1);
    const labelW = Math.max(title.length, sub.length) * CH;
    const portW = Math.max(0, ...ins.map((p) => p.length), 0) * 6 + Math.max(0, ...outs.map((p) => p.length)) * 6;
    const w = shape === "op" ? Math.max(42, title.length * 9 + 22) : shape === "mux" ? 42 : Math.max(64, labelW + 20, portW + 28);
    const head = shape === "inst" ? 34 : shape === "box" || shape === "ff" ? 18 : 0;
    const h = Math.max(shape === "op" ? 34 : 40, n * 18 + head + 8);
    nodes.push({ id, kind: "cell", w, h, ins, outs, padTop: head });
    const width = Math.max(1, ...outs.map((p) => (conns[p] || []).length));
    meta.set(id, { kind: "cell", shape, name: cname.replace(/^\\/, ""), hidden: !!c.hide_name, type: c.type, title, sub,
      src: c.attributes && c.attributes.src, width, module: shape === "inst" && json.modules[c.type] ? c.type : null, ins, outs });
    for (const p of outs) (conns[p] || []).forEach((b) => { if (typeof b === "number") driver.set(b, [id, p]); });
  }
  // sinks: each input port's bits, grouped by who drives them
  const consts = new Map();   // nodeId:port -> text
  const sinks = [];
  for (const [pname, p] of Object.entries(mod.ports || {})) if (p.direction === "output") sinks.push(["p:" + pname, "i", p.bits]);
  for (const [cname, c] of cells) {
    const conns = c.connections || {};
    for (const p of meta.get("c:" + cname).ins) sinks.push(["c:" + cname, p, conns[p] || []]);
  }
  let eid = 0;
  for (const [nid, port, bits] of sinks) {
    const groups = new Map();
    const cbits = [];
    bits.forEach((b) => {
      if (typeof b !== "number") { cbits.push(b); return; }
      const d = driver.get(b);
      if (!d) return;
      const k = d[0] + "\u0001" + d[1];
      if (!groups.has(k)) groups.set(k, { d, bits: [] });
      groups.get(k).bits.push(b);
    });
    if (cbits.length && cbits.length === bits.length) {
      const v = cbits.slice().reverse().join("");
      consts.set(nid + "\u0001" + port, /^[01]+$/.test(v) && v.length > 4 ? bits.length + "'h" + parseInt(v, 2).toString(16).toUpperCase() : bits.length + "'b" + v);
    }
    for (const g of groups.values()) {
      const nm = netName.get(g.bits[0]);
      edges.push({ id: "e" + (eid++), from: g.d, to: [nid, port], net: g.d[0] + ":" + g.d[1], width: g.bits.length,
        name: nm ? nm.name.replace(/^\\/, "") : "", bits: g.bits });
    }
  }
  if (cone) return coneOf({ nodes, edges, meta, consts }, cone);
  return { graph: { nodes, edges }, meta, consts, netName, stats: { cells: cells.length } };
}

// The fan-in of the net named `cone`: its drivers, back to registers and
// module inputs, at most CONE_LIMIT cells.
function coneOf(all, netNameOrNode) {
  const byTo = new Map();
  for (const e of all.edges) { if (!byTo.has(e.to[0])) byTo.set(e.to[0], []); byTo.get(e.to[0]).push(e); }
  const seeds = all.edges.filter((e) => e.name === netNameOrNode).map((e) => e.from[0]);
  if (!seeds.length && all.meta.has(netNameOrNode)) seeds.push(netNameOrNode);
  const keep = new Set(seeds);
  const queue = seeds.slice();
  let cut = 0;
  while (queue.length) {
    const n = queue.shift();
    const m = all.meta.get(n);
    if (m && m.kind === "cell" && isRegister(m.type) && !seeds.includes(n)) continue;
    for (const e of byTo.get(n) || []) {
      if (keep.has(e.from[0])) continue;
      if ([...keep].filter((k) => k.startsWith("c:")).length >= CONE_LIMIT) { cut++; continue; }
      keep.add(e.from[0]);
      queue.push(e.from[0]);
    }
  }
  // the net's own sinks that are outputs, so it ends somewhere
  for (const e of all.edges) if (seeds.includes(e.from[0]) && e.to[0].startsWith("p:")) keep.add(e.to[0]);
  const nodes = all.nodes.filter((n) => keep.has(n.id));
  const edges = all.edges.filter((e) => keep.has(e.from[0]) && keep.has(e.to[0]));
  return { graph: { nodes, edges }, meta: all.meta, consts: all.consts, stats: { cells: nodes.filter((n) => n.id.startsWith("c:")).length, cut, cone: netNameOrNode } };
}

// ------------------------------------------------------------------ the view

let ctx = null;
const S = {
  netlist: null, job: null, building: false, stale: false, level: "rtl", path: [], cone: null,
  layout: null, graph: null, meta: null, consts: null, view: { s: 1, tx: 0, ty: 0 }, sel: null, worker: null, seq: 0,
  command: "", builtMs: 0,
};

export function schematicPaneHtml() {
  return "<div class='sch-bar'>" +
    "<nav class='sch-path' id='sch-path' aria-label='Module path'></nav><span class='ide-spacer'></span>" +
    "<label class='sch-lvl'><span class='sr-only'>Detail</span><select id='sch-level' aria-label='How much detail'>" +
      "<option value='rtl'>RTL (as written)</option><option value='simple'>Simplified</option><option value='gates'>Gates</option></select></label>" +
    "<input id='sch-find' class='inp sch-find' type='text' spellcheck='false' placeholder='Find a signal' aria-label='Find a signal' list='sch-nets'/>" +
    "<datalist id='sch-nets'></datalist>" +
    "<button class='iconbtn iconbtn-sm' id='sch-fit' title='Show it all (F)' aria-label='Show it all'><svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' aria-hidden='true'><path d='M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'/></svg></button>" +
    "<button class='iconbtn iconbtn-sm' id='sch-out' title='Zoom out (−)' aria-label='Zoom out'><svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M5 12h14'/></svg></button>" +
    "<button class='iconbtn iconbtn-sm' id='sch-in' title='Zoom in (+)' aria-label='Zoom in'><svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M12 5v14M5 12h14'/></svg></button>" +
    "<button class='btn btn-sm' id='sch-build'>Draw</button>" +
    "</div><div class='sch-stale' id='sch-stale' hidden>The design changed since this was drawn. <button class='btn btn-ghost btn-sm' data-sch='build'>Draw again</button></div>" +
    "<div class='sch-stage' id='sch-stage' tabindex='0' aria-label='Schematic. Drag to move, wheel to zoom, click a cell for its source.'>" +
      "<div class='sch-msg' id='sch-msg'></div></div>" +
    "<div class='sch-foot' id='sch-foot' aria-live='polite'></div>";
}

export function initSchematic(c) {
  ctx = c;
  const stage = document.getElementById("sch-stage");
  if (!stage || stage._wired) return { build, markStale, showFor };
  stage._wired = true;
  paintMsg();
  document.getElementById("sch-build").addEventListener("click", () => build());
  document.getElementById("sch-level").addEventListener("change", (e) => { S.level = e.target.value; build(); });
  document.getElementById("sch-fit").addEventListener("click", fit);
  document.getElementById("sch-in").addEventListener("click", () => zoomBy(1.25));
  document.getElementById("sch-out").addEventListener("click", () => zoomBy(0.8));
  document.getElementById("sch-stale").addEventListener("click", (e) => { if (e.target.closest("[data-sch='build']")) build(); });
  const find = document.getElementById("sch-find");
  find.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findSignal(find.value.trim()); } });
  find.addEventListener("change", () => findSignal(find.value.trim()));
  document.getElementById("sch-path").addEventListener("click", (e) => {
    const b = e.target.closest("[data-depth]");
    if (!b) return;
    S.path = S.path.slice(0, +b.dataset.depth + 1);
    S.cone = null;
    draw();
  });
  document.getElementById("sch-foot").addEventListener("click", (e) => {
    const b = e.target.closest("[data-sch]");
    if (!b) return;
    if (b.dataset.sch === "src" && S.sel) gotoSrc(S.sel);
    else if (b.dataset.sch === "open" && S.sel) openSub(S.sel);
    else if (b.dataset.sch === "cone" && S.sel) { S.cone = S.sel.startsWith("e:") ? S.sel.slice(2) : S.sel; draw(); }
    else if (b.dataset.sch === "whole") { S.cone = null; draw(); }
    else if (b.dataset.sch === "copy") { navigator.clipboard?.writeText(S.command).then(() => ctx.note("Copied the Yosys command."), () => ctx.note(S.command)); }
  });
  wirePanZoom(stage);
  return { build, markStale, showFor };
}

export function markStale() {
  if (S.netlist && !S.building) { S.stale = true; const b = document.getElementById("sch-stale"); if (b) b.hidden = false; }
}

function paintMsg(html) {
  const m = document.getElementById("sch-msg");
  if (!m) return;
  m.hidden = !(html !== undefined ? html : !S.netlist);
  m.innerHTML = html !== undefined ? html
    : "<div class='sch-empty'><h4>See what the code builds</h4><p>Yosys reads the files the config builds, the way synthesis does, " +
      "and LanEx draws one module at a time: inputs on the left, outputs on the right, a bus as one thick wire. " +
      "Click a cell to find its line; double-click a submodule to open it.</p>" +
      "<button class='btn btn-primary btn-sm' data-sch-go='1'>Draw the schematic</button></div>";
  const go = m.querySelector("[data-sch-go]");
  if (go) go.addEventListener("click", () => build());
}

function foot(html) { const f = document.getElementById("sch-foot"); if (f) f.innerHTML = html || ""; }

export async function build() {
  ctx.setPanel("schem");
  if (S.building) return;
  S.building = true;
  S.stale = false;
  document.getElementById("sch-stale").hidden = true;
  const btn = document.getElementById("sch-build");
  btn.disabled = true;
  paintMsg("<div class='sch-empty'><p><span class='ss-spin' aria-hidden='true'></span> Reading the design with Yosys…</p>" +
    "<button class='btn btn-ghost btn-sm' data-sch-cancel='1'>Cancel</button></div>");
  document.querySelector("[data-sch-cancel]")?.addEventListener("click", () => { if (S.job) api.ideSchematicResult(S.job, true).catch(() => {}); });
  clearSvg();
  let r;
  try {
    r = await api.ideSchematic({ level: S.level });
  } catch (ex) {
    S.building = false; btn.disabled = false;
    paintMsg("<div class='sch-empty sch-err'><p><b>Couldn't draw the schematic.</b> " + esc(ex.message || ex) + "</p>" +
      "<button class='btn btn-sm' data-sch-go='1'>Try again</button></div>");
    document.querySelector("[data-sch-go]")?.addEventListener("click", () => build());
    return;
  }
  S.job = r.id;
  S.command = r.command || "";
  const t0 = Date.now();
  for (;;) {
    let res;
    try { res = await api.ideSchematicResult(r.id); } catch (_e) { res = { state: "failed", error: "Lost touch with the server." }; }
    if (res.state === "running") { await new Promise((ok) => setTimeout(ok, 250)); if (Date.now() - t0 > 180000) break; continue; }
    S.building = false; btn.disabled = false;
    if (res.state !== "done") {
      const err = res.error || (res.state === "cancelled" ? "Stopped." : "Yosys stopped.");
      const loc = srcOf((err.match(/([\w./-]+\.(?:s?v|vh|svh|vhdl?)):(\d+)/) || []).slice(1, 3).join(":"));
      paintMsg("<div class='sch-empty sch-err'><p><b>" + (res.state === "cancelled" ? "Stopped." : "Yosys couldn't read the design.") + "</b> " +
        esc(err) + "</p>" + (loc ? "<button class='btn btn-sm' data-sch-loc='1'>Open " + esc(loc.rel + ":" + loc.line) + "</button> " : "") +
        "<button class='btn btn-ghost btn-sm' data-sch-go='1'>Try again</button>" +
        "<p class='sch-cmd'><code>" + esc(S.command) + "</code></p></div>");
      document.querySelector("[data-sch-go]")?.addEventListener("click", () => build());
      document.querySelector("[data-sch-loc]")?.addEventListener("click", () => ctx.openAt(loc.rel, loc.line, 1, 0));
      return;
    }
    try { S.netlist = JSON.parse(res.json); } catch (_e) { paintMsg("<div class='sch-empty sch-err'><p>The netlist couldn't be read.</p></div>"); return; }
    S.builtMs = res.ms || 0;
    const top = res.top || ctx.top();
    S.path = [{ mod: findModule(top), inst: null }];
    S.cone = null;
    await draw();
    return;
  }
  S.building = false; btn.disabled = false;
}

function findModule(name) {
  const mods = Object.keys((S.netlist && S.netlist.modules) || {});
  if (mods.includes(name)) return name;
  const top = mods.find((m) => S.netlist.modules[m].attributes && S.netlist.modules[m].attributes.top);
  return top || mods[0];
}

// Open the schematic on the module defined at this place (editor → schematic).
export function showFor(moduleName) {
  if (!S.netlist) { build(); return; }
  const m = Object.keys(S.netlist.modules).find((k) => k === moduleName || moduleLabel(k, S.netlist.modules[k]).split(" ")[0] === moduleName);
  if (!m) { ctx.note(moduleName + " isn't in the drawn design (it may not be under the top module).", true); return; }
  S.path = [{ mod: m, inst: null }];
  S.cone = null;
  ctx.setPanel("schem");
  draw();
}

function paintPath() {
  const nav = document.getElementById("sch-path");
  if (!nav) return;
  nav.innerHTML = S.path.map((p, i) => {
    const label = i === 0 ? moduleLabel(p.mod, S.netlist.modules[p.mod]) : p.inst + " (" + moduleLabel(p.mod, S.netlist.modules[p.mod]) + ")";
    return (i ? "<span class='sch-sep' aria-hidden='true'>›</span>" : "") +
      "<button class='sch-crumb" + (i === S.path.length - 1 ? " is-on" : "") + "' data-depth='" + i + "'>" + esc(label) + "</button>";
  }).join("") + (S.cone ? "<span class='sch-sep'>›</span><span class='sch-crumb is-on'>what drives " + esc(S.cone.replace(/^c:/, "")) + "</span>" : "");
}

async function draw() {
  if (!S.netlist) return;
  const cur = S.path[S.path.length - 1];
  paintPath();
  let g;
  try { g = netlistToGraph(S.netlist, cur.mod, { cone: S.cone }); } catch (ex) { paintMsg("<div class='sch-empty sch-err'><p>" + esc(ex.message || ex) + "</p></div>"); return; }
  const dl = document.getElementById("sch-nets");
  if (dl) {
    const names = [...new Set(Object.entries(S.netlist.modules[cur.mod].netnames || {}).filter(([, n]) => !n.hide_name).map(([k]) => k.replace(/^\\/, "")))];
    dl.innerHTML = names.slice(0, 2000).map((n) => "<option value='" + esc(n) + "'>").join("");
  }
  const ncell = g.stats.cells;
  if (!S.cone && ncell > CELL_LIMIT) {
    clearSvg();
    paintMsg("<div class='sch-empty'><h4>" + ncell.toLocaleString() + " cells: too many to draw readably at once</h4>" +
      "<p>Pick a signal in <b>Find a signal</b> to draw what drives it, back to the registers and inputs. Or switch to RTL or Simplified, which have fewer cells.</p></div>");
    foot("<span class='sch-stat'>" + esc(moduleLabel(cur.mod, S.netlist.modules[cur.mod])) + " · " + ncell + " cells</span>");
    document.getElementById("sch-find").focus();
    return;
  }
  S.graph = g.graph; S.meta = g.meta; S.consts = g.consts;
  const seq = ++S.seq;
  paintMsg(ncell > 80 ? "<div class='sch-empty'><p><span class='ss-spin' aria-hidden='true'></span> Laying out " + ncell + " cells…</p></div>" : "");
  if (ncell <= 80) paintMsg("");
  let lay;
  try { lay = await layoutAsync(g.graph, ncell > 120); } catch (ex) {
    paintMsg("<div class='sch-empty sch-err'><p><b>The layout failed.</b> " + esc(ex.message || ex) + "</p></div>");
    return;
  }
  if (seq !== S.seq) return;
  S.layout = lay;
  paintMsg("");
  document.getElementById("sch-msg").hidden = true;
  render();
  const cut = g.stats.cut ? " · " + g.stats.cut + " more drivers not drawn" : "";
  foot("<span class='sch-stat'>" + esc(moduleLabel(cur.mod, S.netlist.modules[cur.mod])) + " · " + ncell + (ncell === 1 ? " cell" : " cells") +
    (S.builtMs ? " · read in " + (S.builtMs / 1000).toFixed(1) + " s" : " · read before, unchanged") + ", laid out in " + lay.ms + " ms" + cut + "</span>" +
    (S.cone ? "<button class='btn btn-ghost btn-sm' data-sch='whole'>Whole module</button>" : "") +
    "<span class='ide-spacer'></span><button class='btn btn-ghost btn-sm' data-sch='copy' title='" + esc(S.command) + "'>Copy Yosys command</button>");
  fit(true);
}

function layoutAsync(graph, useWorker) {
  if (!useWorker || typeof Worker === "undefined") return Promise.resolve(layoutGraph(graph));
  if (S.worker) { S.worker.terminate(); S.worker = null; }
  return new Promise((resolve, reject) => {
    let w;
    try { w = new Worker(new URL("./schworker.js", import.meta.url), { type: "module" }); } catch (_e) { resolve(layoutGraph(graph)); return; }
    S.worker = w;
    const id = Date.now();
    w.onmessage = (e) => { w.terminate(); if (S.worker === w) S.worker = null; if (e.data.error) reject(new Error(e.data.error)); else resolve(e.data.layout); };
    w.onerror = (e) => { w.terminate(); if (S.worker === w) S.worker = null; reject(new Error(e.message || "worker failed")); };
    w.postMessage({ id, graph });
  });
}

function clearSvg() {
  const stage = document.getElementById("sch-stage");
  stage?.querySelector("svg.sch-svg")?.remove();
  S.layout = null;
  foot("");
}

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function render() {
  const stage = document.getElementById("sch-stage");
  stage.querySelector("svg.sch-svg")?.remove();
  const lay = S.layout;
  const svg = el("svg", { class: "sch-svg", role: "img", "aria-label": "Schematic" });
  const defs = el("defs", {}, svg);
  const pat = el("pattern", { id: "sch-grid", width: 24, height: 24, patternUnits: "userSpaceOnUse" }, defs);
  el("path", { d: "M 24 0 L 0 0 0 24", class: "sch-gridline" }, pat);
  const root = el("g", { class: "sch-root" }, svg);
  el("rect", { x: -2000, y: -2000, width: lay.width + 4000, height: lay.height + 4000, fill: "url(#sch-grid)", class: "sch-bg" }, root);
  const wires = el("g", { class: "sch-wires" }, root);
  const labels = el("g", { class: "sch-wlabels" }, root);
  for (const e of S.graph.edges) {
    const pts = lay.edges[e.id];
    if (!pts) continue;
    const d = "M" + pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L");
    const path = el("path", { d, class: "sch-w" + (e.width > 1 ? " is-bus" : ""), "data-net": e.net, "data-e": e.id }, wires);
    const t = el("title", {}, path);
    t.textContent = (e.name || "net") + (e.width > 1 ? " " + widthText(e.width) : "");
  }
  // a width label once per bus trunk, at its driver
  const seen = new Set();
  for (const e of S.graph.edges) {
    if (e.width <= 1 || seen.has(e.net)) continue;
    seen.add(e.net);
    const pts = lay.edges[e.id];
    if (!pts || pts.length < 2) continue;
    const tx = el("text", { x: pts[0][0] + 4, y: pts[0][1] - 4, class: "sch-wl" }, labels);
    tx.textContent = (e.name ? e.name + " " : "") + widthText(e.width);
  }
  const cells = el("g", { class: "sch-cells" }, root);
  for (const n of S.graph.nodes) {
    const p = lay.nodes[n.id];
    const m = S.meta.get(n.id);
    if (!p || !m) continue;
    const g = el("g", { class: "sch-c is-" + (m.kind === "cell" ? m.shape : m.kind), "data-id": n.id, transform: "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")", tabindex: "-1" }, cells);
    const title = el("title", {}, g);
    title.textContent = m.kind === "cell" ? (m.shape === "inst" ? m.name + " (" + m.sub + ")" : m.type + " " + m.name) + (m.src ? " · " + m.src.split("|")[0] : "")
      : (m.dir || "") + " " + m.label;
    if (m.kind === "in" || m.kind === "out") {
      const w = p.w, h = p.h;
      const pts = m.kind === "in" ? `0,0 ${w - 9},0 ${w},${h / 2} ${w - 9},${h} 0,${h}` : `9,0 ${w},0 ${w},${h} 9,${h} 0,${h / 2}`;
      el("polygon", { points: pts, class: "sch-pin" }, g);
      const t = el("text", { x: m.kind === "in" ? 8 : 14, y: h / 2 + 4, class: "sch-pt" }, g);
      t.textContent = m.label;
    } else if (m.shape === "mux") {
      el("polygon", { points: `0,0 ${p.w},${Math.min(10, p.h * 0.2)} ${p.w},${p.h - Math.min(10, p.h * 0.2)} 0,${p.h}`, class: "sch-body" }, g);
      const t = el("text", { x: p.w / 2, y: p.h / 2 + 4, class: "sch-op is-small", "text-anchor": "middle" }, g);
      t.textContent = "MUX";
    } else if (m.shape === "op") {
      el("rect", { x: 0, y: 0, width: p.w, height: p.h, rx: p.h / 2.4, class: "sch-body" }, g);
      const t = el("text", { x: p.w / 2, y: p.h / 2 + 5, class: "sch-op", "text-anchor": "middle" }, g);
      t.textContent = m.title;
    } else {
      el("rect", { x: 0, y: 0, width: p.w, height: p.h, rx: 3, class: "sch-body" + (m.shape === "inst" ? " is-inst" : "") }, g);
      if (m.shape === "inst") el("line", { x1: 0, y1: 0.5, x2: p.w, y2: 0.5, class: "sch-accent" }, g);
      const t = el("text", { x: p.w / 2, y: m.shape === "inst" ? 15 : 14, class: "sch-ct", "text-anchor": "middle" }, g);
      t.textContent = m.title;
      if (m.sub) { const s = el("text", { x: p.w / 2, y: 27, class: "sch-cs", "text-anchor": "middle" }, g); s.textContent = m.sub; }
      if (m.shape === "ff") {
        const clk = (n.ins || []).find((q) => /^(CLK|C)$/.test(q));
        if (clk) { const y = p.ports[clk][1] - p.y; el("path", { d: `M0 ${y - 5} L7 ${y} L0 ${y + 5}`, class: "sch-clk" }, g); }
      }
    }
    // ports: small square vias with their names inside the box
    if (m.kind === "cell") {
      for (const q of n.ins || []) {
        const [px, py] = p.ports[q];
        el("rect", { x: -3, y: py - p.y - 3, width: 6, height: 6, class: "sch-via" }, g);
        if (m.shape !== "op" && !(m.shape === "mux" && /^[AB]$/.test(q))) {
          const t = el("text", { x: 6, y: py - p.y + 3.5, class: "sch-pn" }, g); t.textContent = q;
        }
        const k = S.consts.get(n.id + "\u0001" + q);
        if (k) { const t = el("text", { x: -6, y: py - p.y - 4, class: "sch-k", "text-anchor": "end" }, g); t.textContent = k; }
      }
      for (const q of n.outs || []) {
        const [px, py] = p.ports[q];
        el("rect", { x: p.w - 3, y: py - p.y - 3, width: 6, height: 6, class: "sch-via" }, g);
        if (m.shape === "inst" || m.shape === "box" || (m.shape === "ff" && q !== "Q")) {
          const t = el("text", { x: p.w - 6, y: py - p.y + 3.5, class: "sch-pn", "text-anchor": "end" }, g); t.textContent = q;
        }
      }
    }
  }
  stage.appendChild(svg);
  svg.addEventListener("click", onClick);
  svg.addEventListener("dblclick", onDbl);
  svg.addEventListener("mouseover", (e) => { const w = e.target.closest(".sch-w"); hoverNet(w ? w.dataset.net : null); });
  svg.addEventListener("mouseout", (e) => { if (e.target.closest(".sch-w")) hoverNet(null); });
  applyView();
}

function hoverNet(net) {
  document.querySelectorAll("#sch-stage .sch-w.is-hot").forEach((p) => p.classList.remove("is-hot"));
  if (net) document.querySelectorAll("#sch-stage .sch-w[data-net='" + CSS.escape(net) + "']").forEach((p) => p.classList.add("is-hot"));
}

function select(id) {
  S.sel = id;
  document.querySelectorAll("#sch-stage .is-sel").forEach((x) => x.classList.remove("is-sel"));
  if (!id) { paintFootDefault(); return; }
  if (id.startsWith("e:")) {
    const net = id.slice(2);
    document.querySelectorAll("#sch-stage .sch-w[data-net='" + CSS.escape(net) + "']").forEach((p) => p.classList.add("is-sel"));
    const e = S.graph.edges.find((x) => x.net === net);
    const drv = S.meta.get(e.from[0]);
    const sinks = S.graph.edges.filter((x) => x.net === net).length;
    foot("<span class='sch-selt'><span class='an-eyebrow'>Net</span> <code>" + esc(e.name || "(unnamed)") + "</code> " + esc(widthText(e.width)) +
      " · driven by " + esc(drv.kind === "cell" ? drv.title + " " + drv.name : drv.name) + " · " + sinks + (sinks === 1 ? " load" : " loads") + "</span>" +
      "<span class='ide-spacer'></span>" + (e.name ? "<button class='btn btn-ghost btn-sm' data-sch='cone'>What drives it</button>" : ""));
    return;
  }
  const g = document.querySelector("#sch-stage .sch-c[data-id='" + CSS.escape(id) + "']");
  if (g) g.classList.add("is-sel");
  const m = S.meta.get(id);
  if (!m) return;
  const src = srcOf(m.src);
  const what = m.kind === "cell" ? (m.shape === "inst" ? "instance " + m.name + " of " + m.sub : m.title + " (" + m.type.replace(/^\$/, "") + ")" + (m.hidden ? "" : " " + m.name))
    : (m.kind === "in" ? "input " : "output ") + m.label;
  foot("<span class='sch-selt'><span class='an-eyebrow'>Selected</span> " + esc(what) + (src ? " · <code>" + esc(src.rel + ":" + src.line) + "</code>" : "") + "</span>" +
    "<span class='ide-spacer'></span>" +
    (src ? "<button class='btn btn-sm' data-sch='src'>Go to source</button>" : "") +
    (m.module ? "<button class='btn btn-ghost btn-sm' data-sch='open'>Open " + esc(m.sub.split(" ")[0]) + "</button>" : "") +
    (m.kind === "cell" ? "<button class='btn btn-ghost btn-sm' data-sch='cone'>What drives it</button>" : ""));
}

function paintFootDefault() {
  const cur = S.path[S.path.length - 1];
  if (!cur || !S.layout) return;
  foot("<span class='sch-stat'>" + esc(moduleLabel(cur.mod, S.netlist.modules[cur.mod])) + " · " + S.graph.nodes.filter((n) => n.id.startsWith("c:")).length + " cells · click a cell for its source</span>");
}

function onClick(e) {
  if (S._dragged) { S._dragged = false; return; }
  const c = e.target.closest(".sch-c");
  if (c) { select(c.dataset.id); return; }
  const w = e.target.closest(".sch-w");
  if (w) { select("e:" + w.dataset.net); return; }
  select(null);
}

function onDbl(e) {
  const c = e.target.closest(".sch-c");
  if (!c) return;
  const m = S.meta.get(c.dataset.id);
  if (m && m.module) openSub(c.dataset.id);
  else if (m) gotoSrc(c.dataset.id);
}

function openSub(id) {
  const m = S.meta.get(id);
  if (!m || !m.module) return;
  S.path.push({ mod: m.module, inst: m.name });
  S.cone = null;
  S.sel = null;
  draw();
}

function gotoSrc(id) {
  const m = S.meta.get(id);
  const src = m && srcOf(m.src);
  if (!src) { ctx.note("Yosys gave no source line for this cell (VHDL cells carry none).", true); return; }
  ctx.openAt(src.rel, src.line, src.col, src.len);
}

function findSignal(q) {
  if (!q || !S.netlist) return;
  const cur = S.path[S.path.length - 1];
  const names = Object.entries(S.netlist.modules[cur.mod].netnames || {}).filter(([, n]) => !n.hide_name).map(([k]) => k.replace(/^\\/, ""));
  const hit = names.includes(q) ? q : (rank(q, names, (x) => x, { limit: 1 })[0] || {}).item;
  if (!hit) { ctx.note("No signal matches " + q + " in this module.", true); return; }
  const big = !S.cone && (S.graph ? S.graph.nodes.filter((n) => n.id.startsWith("c:")).length : CELL_LIMIT + 1) > CELL_LIMIT;
  const onScreen = S.graph && S.graph.edges.some((e) => e.name === hit);
  if (big || !S.layout || !onScreen) { S.cone = hit; draw().then(() => focusNet(hit)); return; }
  focusNet(hit);
}

function focusNet(name) {
  const e = S.graph && S.graph.edges.find((x) => x.name === name);
  if (!e || !S.layout) return;
  select("e:" + e.net);
  const pts = S.layout.edges[e.id];
  if (pts) centerOn(pts[0][0], pts[0][1]);
}

// ------------------------------------------------------------------ pan and zoom

function applyView() {
  const root = document.querySelector("#sch-stage .sch-root");
  if (root) root.setAttribute("transform", "translate(" + S.view.tx.toFixed(1) + "," + S.view.ty.toFixed(1) + ") scale(" + S.view.s.toFixed(4) + ")");
}

function stageSize() {
  const st = document.getElementById("sch-stage");
  return { w: st.clientWidth || 800, h: st.clientHeight || 400 };
}

// Show the whole module, never bigger than reading size. On a first draw
// (`readable`) a module too big to read whole starts at its inputs instead.
export function fit(readable = false) {
  if (!S.layout) return;
  const { w, h } = stageSize();
  const s = Math.max(0.02, Math.min(1.25, (w - 40) / S.layout.width, (h - 40) / S.layout.height));
  S.view = { s, tx: (w - S.layout.width * s) / 2, ty: (h - S.layout.height * s) / 2 };
  applyView();
  // Too small to read whole: say how to get in (the overview stays honest).
  const stat = document.querySelector("#sch-foot .sch-stat");
  if (readable === true && stat && s < 0.3) {
    stat.textContent += " · too small to read whole: wheel to zoom in, or Find a signal to draw only what drives it";
  }
}

function zoomBy(f, cx, cy) {
  const { w, h } = stageSize();
  const x = cx === undefined ? w / 2 : cx, y = cy === undefined ? h / 2 : cy;
  const s2 = Math.min(4, Math.max(0.04, S.view.s * f));
  S.view.tx = x - (x - S.view.tx) * (s2 / S.view.s);
  S.view.ty = y - (y - S.view.ty) * (s2 / S.view.s);
  S.view.s = s2;
  applyView();
}

function centerOn(x, y) {
  const { w, h } = stageSize();
  if (S.view.s < 0.7) S.view.s = 0.9;
  S.view.tx = w / 2 - x * S.view.s;
  S.view.ty = h / 2 - y * S.view.s;
  applyView();
}

function wirePanZoom(stage) {
  const local = (e) => {
    const r = stage.getBoundingClientRect();
    const z = r.width && stage.clientWidth ? r.width / stage.clientWidth : 1;   // the app's CSS zoom
    return [(e.clientX - r.left) / z, (e.clientY - r.top) / z];
  };
  stage.addEventListener("wheel", (e) => {
    if (!S.layout) return;
    e.preventDefault();
    const [x, y] = local(e);
    zoomBy(Math.exp(-e.deltaY * 0.0015), x, y);
  }, { passive: false });
  let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    if (!S.layout || e.button !== 0) return;
    const [x, y] = local(e);
    drag = { x, y, tx: S.view.tx, ty: S.view.ty, moved: false, id: e.pointerId };
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const [x, y] = local(e);
    if (!drag.moved && Math.hypot(x - drag.x, y - drag.y) < 4) return;
    if (!drag.moved) { drag.moved = true; try { stage.setPointerCapture(drag.id); } catch (_e) { /* fine */ } stage.classList.add("is-drag"); }
    S.view.tx = drag.tx + (x - drag.x);
    S.view.ty = drag.ty + (y - drag.y);
    applyView();
  });
  const end = () => { if (drag && drag.moved) S._dragged = true; drag = null; stage.classList.remove("is-drag"); };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  stage.addEventListener("keydown", (e) => {
    if (!S.layout || e.ctrlKey || e.metaKey || e.altKey) return;
    const step = 60;
    if (e.key === "f" || e.key === "F") { e.preventDefault(); fit(); }
    else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.25); }
    else if (e.key === "-") { e.preventDefault(); zoomBy(0.8); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); S.view.tx += step; applyView(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); S.view.tx -= step; applyView(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); S.view.ty += step; applyView(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); S.view.ty -= step; applyView(); }
    else if (e.key === "Escape" && S.sel) { e.preventDefault(); e.stopPropagation(); select(null); }
    else if (e.key === "Enter" && S.sel && !S.sel.startsWith("e:")) { e.preventDefault(); const m = S.meta.get(S.sel); if (m && m.module) openSub(S.sel); else gotoSrc(S.sel); }
  });
}
