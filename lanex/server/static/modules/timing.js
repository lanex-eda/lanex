// timing.js — Verify's timing explorer: the worst paths per corner, and one
// path in full.
//
// The way timing debug reads in PrimeTime, Tempus and Vivado, cut to what a
// LibreLane run has: which corner is worst for setup and for hold (two corner
// grids), how close its endpoints are to failing (a histogram with bins on 0),
// the endpoints worst first (one row per endpoint: report_checks gives one path
// each), and the path itself: where the time goes, the required time written
// out as a sum (clock edge, capture clock, uncertainty, pessimism removed, the
// flip-flop's setup or hold time, output delay), and every pin with its cell,
// edge, fanout, load, slew, incremental and total delay. The launch and capture
// clock trees fold into one row each (OpenROAD's timing widget does the same).
//
// Everything is parsed from the run's own STA reports by controller/timing.py
// (/api/timing/paths, /api/timing/path); nothing is re-timed. The Δ column is
// against the app's baseline (context.js), so there is no second picker.

import { api, fmt } from "./api.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";
import { histogramOption } from "./barchart.js";
import { renderFileText } from "./fileview.js";
import { cornerGridHtml, cornerWords, wireGridKeys } from "./cornergrid.js";
import { slack as mfSlack } from "./metricfmt.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));
// The time unit, from the payload (controller/timing.py TIME_UNIT; LibreLane's
// corner.tcl sets ns).
let U = "ns";
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
// OpenSTA writes about 1e30 for "no path": that is not a slack.
const timed = (v) => (isNum(v) && Math.abs(v) < 1e12 ? v : null);

export const KIND_WORDS = {
  "in-reg": { short: "in → reg", tip: "From an input pin to a flip-flop. Its budget includes the input delay the SDC sets (IO_DELAY_CONSTRAINT, a share of the clock)." },
  "reg-reg": { short: "reg → reg", tip: "Between two flip-flops: only your logic and the clock tree." },
  "reg-out": { short: "reg → out", tip: "From a flip-flop to an output pin. The output delay is kept for whatever is outside the chip." },
  "in-out": { short: "in → out", tip: "Straight from an input to an output, no flip-flop in between." },
  reset: { short: "reset", tip: "Reset timing: a reset input must change far enough from the clock edge (recovery for setup, removal for hold)." },
};
const KIND_ORDER = ["reg-reg", "in-reg", "reg-out", "in-out", "reset"];

const TERM_TIPS = {
  "clock edge": "The clock edge this check is against (from the SDC's clock).",
  "launch clock": "How late the clock reaches the start flip-flop through the clock tree.",
  "capture clock": "How late the clock reaches the end flip-flop through the clock tree.",
  uncertainty: "Margin the SDC adds for clock jitter and skew it can't model (set_clock_uncertainty; CLOCK_UNCERTAINTY_CONSTRAINT in LibreLane's default SDC).",
  "pessimism removed": "Clock reconvergence pessimism removal: the part of the clock tree the launch and capture clocks share can't be both slow and fast at once, so OpenSTA gives that time back.",
  "input delay": "Time the SDC says the signal spends outside the chip before it reaches this input (IO_DELAY_CONSTRAINT).",
  "output delay": "Time the SDC keeps for whatever is outside the chip after this output (IO_DELAY_CONSTRAINT).",
};
function termTip(word) {
  if (TERM_TIPS[word]) return TERM_TIPS[word];
  if (/^setup time/.test(word)) return "How long before the clock edge the flip-flop needs its input steady (from the cell's library).";
  if (/^hold time/.test(word)) return "How long after the clock edge the flip-flop needs its input to stay steady (from the cell's library).";
  if (/^recovery time/.test(word)) return "How long before the clock edge a reset must be released (from the cell's library).";
  if (/^removal time/.test(word)) return "How long after the clock edge a reset must stay asserted (from the cell's library).";
  if (/^clock to Q/.test(word)) return "The start flip-flop's own delay from its clock edge to its output.";
  if (/^through/.test(word)) return "The data path: every cell and wire between the start and the end.";
  return "";
}

// A slack: signed, 3 decimals; a failure never rounds to 0.
// The one slack formatter (metricfmt.js), under the name this module exports.
export function fmtSlack(v, d = 3) { return mfSlack(v, d); }

const n3 = (v) => (isNum(v) ? v.toFixed(3) : "");

// "10.268 = 10.000 clock edge + 0.651 capture clock − 0.100 uncertainty …"
export function termsText(total, terms) {
  const parts = (terms || []).map((t, i) => (i === 0 ? "" : t.v < 0 ? " − " : " + ") + Math.abs(t.v).toFixed(3) + " " + t.word);
  return (isNum(total) ? total.toFixed(3) + " = " : "") + parts.join("");
}

// The names a layout highlight needs (contract with Layout, lane 3).
export function highlightOf(p, tag) {
  const h = p.highlight || {};
  const endPin = lastPin(p) || p.end;
  return {
    kind: "path",
    label: p.check + " · " + p.corner + " · " + p.start + " → " + endPin + " · " + fmtSlack(p.slack) + " " + U,
    insts: h.insts || [], nets: h.nets || [], pins: h.pins || [], ports: h.ports || [],
    clock: h.clock || { launch: { insts: [], nets: [] }, capture: { insts: [], nets: [] } },
    corner: p.corner, check: p.check, slack: p.slack, run: tag,
  };
}

function lastPin(p) {
  const rows = (p.data && p.data.rows) || [];
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].pin || rows[i].port) return rows[i].pin || rows[i].port;
  return null;
}

// ---------------------------------------------------------------- state
const S = {
  tag: "", base: "", rep: null, kind: "setup", corner: null, only: "", q: "", bin: null,
  list: null, sel: null, detail: null, token: 0, dtoken: 0, open: { launch: false, capture: false }, mounted: false,
};
const ROW_H = 34;

// The section's host; verify.js calls this whenever the run, the baseline or
// the verify payload changes.
// Another page (Analytics' corner grid) asks for one corner's paths: now,
// when this run's explorer is up, else when it next draws.
let _want = null;
export function pickCorner(tag, kind, corner) {
  if (S.tag === tag && S.rep && document.getElementById("vf-tx")?.isConnected) {
    S.kind = kind; S.corner = corner; S.bin = null; S.sel = null; loadList();
  } else _want = { tag, kind, corner };
}

export async function renderTimingExplorer({ tag, base, rep }) {
  const host = document.getElementById("vf-tx");
  if (!host) return;
  const runChanged = tag !== S.tag;
  if (runChanged) { S.corner = null; S.sel = null; S.bin = null; S.q = ""; S.only = ""; S.detail = null; }
  if (_want && _want.tag === tag) { S.kind = _want.kind; S.corner = _want.corner; S.bin = null; S.sel = null; _want = null; }
  S.tag = tag; S.base = base || ""; S.rep = rep;
  wire(host);
  const t = (rep && rep.details && rep.details.timing) || {};
  if (t.no_clock) {
    host.innerHTML = gridsHtml(t) + noClockHtml(t);
    return;
  }
  host.innerHTML = gridsHtml(t) + "<div class='an-card tx-card' id='tx-card'><div class='tx-loading'>Reading the timing reports…</div></div>";
  await loadList();
}

function gridsHtml(t) {
  const rows = (t.corners || []).filter((r) => r.corner);
  if (rows.length < 2) return "";
  const ns = (v) => fmtSlack(v, 3);
  const tip = (kind) => (r) => {
    const g = (r.gates || {})[kind];
    const ws = r[kind + "_ws"], tns = r[kind + "_tns"], vio = r[kind + "_vio"];
    const r2r = r[kind + "_r2r"];
    return r.corner + " — " + cornerWords(r.corner) + "\n" +
      "Worst " + kind + " slack " + ns(ws) + " " + U + " · total negative " + ns(tns) + " " + U + " · " + (isNum(vio) ? vio : "—") + " failing endpoints" +
      (isNum(r2r) && Math.abs(r2r) < 1e12 ? "\nRegister to register: " + ns(r2r) + " " + U : "") +
      (kind === "setup" && isNum(r.fmax) ? "\nFastest clock for register-to-register paths here: " + r.fmax.toFixed(1) + " MHz (" + r.period_min + " " + U + ")" : "") +
      (g === true ? "\nA " + kind + " violation here fails the run" : g === false ? "\nA " + kind + " violation here only warns" : "");
  };
  const card = (kind, title) => {
    const ws = (r) => timed(r[kind + "_ws"]);
    const vals = rows.map(ws).filter(isNum);
    const fail = rows.filter((r) => isNum(ws(r)) && ws(r) < 0);
    const worst = vals.length ? Math.min(...vals) : null;
    const wr = rows.find((r) => r[kind + "_ws"] === worst);
    // No clock reached the registers but input-to-output paths were timed:
    // their slack is real, but it is not the chip's timing.
    const ioOnly = !!(t.no_clock && t.no_clock.io_paths);
    const sum = !vals.length ? "not timed" : ioOnly ? "input-to-output paths only · worst " + ns(worst) + " " + U + " · no flip-flop timed" : fail.length
      ? fail.length + " of " + rows.length + " corners fail · worst " + ns(worst) + " " + U + " in " + (wr ? wr.corner : "")
      : "all " + rows.length + " corners pass · worst " + ns(worst) + " " + U;
    return "<section class='an-card tx-gcard" + (S.kind === kind ? " is-on" : "") + (fail.length ? " is-fail" : "") + "' data-tx-gkind='" + kind + "'>" +
      "<div class='tx-gh'><span class='an-eyebrow'>" + esc(title) + "</span><span class='an-u'>" + esc(U) + "</span></div>" +
      cornerGridHtml(rows, { value: ws, selected: S.kind === kind ? (S.corner || "") : "", gate: (r) => (r.gates || {})[kind],
        fmt: (v) => ns(v), tip: tip(kind), kind, label: title }) +
      "<p class='tx-gsum" + (fail.length ? " is-fail" : "") + "'>" + esc(sum) + "</p></section>";
  };
  return "<div class='tx-grids'>" + card("setup", "Setup · worst slack by corner") + card("hold", "Hold · worst slack by corner") +
    "<p class='tx-glegend'><span>● fails the run here</span><span>○ only warns</span><span>◆ worst corner</span><span>Click a corner to list its paths.</span></p></div>";
}

function noClockHtml(t) {
  const nc = t.no_clock || {};
  const port = Array.isArray(nc.clock_port) ? nc.clock_port.join(", ") : nc.clock_port;
  return "<div class='an-card tx-card'><div class='tx-empty is-warn'>" + icon("alert", { size: 18 }) +
    "<div><b>" + (nc.io_paths ? "No clock reached the registers, so OpenSTA timed only the paths from an input straight to an output."
      : "No clock reached the registers, so OpenSTA timed no path in any corner.") + "</b>" +
    "<p>" + esc((nc.registers ? nc.registers + " flip-flops" : "The flip-flops") + (isNum(nc.unclocked) ? ", " + nc.unclocked + " clock pins with no clock" : "") +
      ". CLOCK_PORT is " + (port ? "\"" + port + "\"" + (nc.port_missing ? ", and the top module has no port by that name" : "") : "not set") + ".") + "</p>" +
    "<p>Set CLOCK_PORT to the clock input of the top module and run again. Until then \"0 violations\" means nothing was checked.</p>" +
    "<button class='btn btn-ghost btn-sm' data-go='setup' data-go-setting='CLOCK_PORT'>" + icon("chevron", { size: 12 }) + " Set CLOCK_PORT</button></div></div></div>";
}

async function loadList({ keepSel = false } = {}) {
  const my = ++S.token;
  const card = document.getElementById("tx-card");
  let data;
  try {
    data = await api.timingList({ tag: S.tag, kind: S.kind, corner: S.corner || "", q: S.q, only: S.only, base: S.base });
  } catch (ex) {
    if (my !== S.token) return;
    if (card) card.innerHTML = "<div class='tx-empty is-fail'>" + icon("alert", { size: 18 }) + "<div><b>Couldn't read the timing reports.</b><p>" + esc(ex.message || ex) +
      "</p><p>The numbers above come from the run's metrics; the raw reports may be damaged. Refresh to try again.</p></div></div>";
    return;
  }
  if (my !== S.token) return;
  S.list = data;
  U = (data && data.unit) || "ns";
  if (data.ok) S.corner = data.corner;
  if (!keepSel || !data.ok || !data.rows.some((r) => r.i === S.sel)) S.sel = data.ok && data.rows.length ? data.rows[0].i : null;
  paintGridsSel();
  paintCard();
  if (S.sel != null) loadDetail(S.sel);
}

function paintGridsSel() {
  const host = document.getElementById("vf-tx");
  if (!host) return;
  host.querySelectorAll(".tx-gcard").forEach((c) => c.classList.toggle("is-on", c.dataset.txGkind === S.kind));
  host.querySelectorAll(".cg-b").forEach((b) => {
    const on = b.dataset.cgKind === S.kind && b.dataset.cgCorner === S.corner;
    b.closest("td").classList.toggle("is-sel", on);
    b.setAttribute("aria-pressed", String(on));
  });
}

function rowsShown() {
  const L = S.list;
  if (!L || !L.ok) return [];
  if (!S.bin) return L.rows;
  const [a, b] = S.bin;
  return L.rows.filter((r) => r.slack >= a && r.slack < b);
}

function paintCard() {
  const card = document.getElementById("tx-card");
  const L = S.list;
  if (!card || !L) return;
  if (!L.ok) {
    card.innerHTML = "<div class='tx-empty'>" + icon("help", { size: 18 }) + "<div><b>" + esc(L.error || "No timing report for this run.") + "</b>" +
      "<p>Timing paths come from the sign-off timing step (OpenROAD.STAPostPNR). A run that stopped before it has none.</p></div></div>";
    return;
  }
  const kinds = KIND_ORDER.filter((k) => (L.kinds || {})[k]);
  const seg = "<div class='seg tx-seg' role='group' aria-label='Setup or hold'>" + ["setup", "hold"].map((k) =>
    "<button class='seg-btn" + (S.kind === k ? " is-active" : "") + "' data-tx-kind='" + k + "' aria-pressed='" + (S.kind === k) + "'>" + (k === "setup" ? "Setup" : "Hold") + "</button>").join("") + "</div>";
  const chips = kinds.length > 1 ? "<div class='tx-kinds' role='group' aria-label='Path kind'>" +
    "<button class='tx-kchip" + (!S.only ? " is-on" : "") + "' data-tx-only='' aria-pressed='" + !S.only + "'>All <span>" + L.total + "</span></button>" +
    kinds.map((k) => "<button class='tx-kchip" + (S.only === k ? " is-on" : "") + "' data-tx-only='" + k + "' aria-pressed='" + (S.only === k) + "' data-an-tip='" + esc(KIND_WORDS[k].tip) + "'>" +
      esc(KIND_WORDS[k].short) + " <span>" + L.kinds[k] + "</span></button>").join("") + "</div>" : "";
  const find = "<label class='tx-find'>" + icon("search", { size: 13 }) + "<input type='search' class='inp' id='tx-q' placeholder='Find a pin, net or cell…' aria-label='Find a pin, net or cell on the paths' value='" + esc(S.q) + "'/></label>";
  const words = cornerWords(L.corner);
  const shown = rowsShown();
  const binChip = S.bin ? "<button class='tx-binchip' data-tx-bin='clear' aria-label='Clear the slack range filter'>slack " + esc(fmtSlack(S.bin[0], 2) + " to " + fmtSlack(S.bin[1], 2)) + " " + U + " ×</button>" : "";
  const viol = L.violating ? "<b class='is-bad'>" + L.violating + " below 0 " + U + "</b>" : "none below 0 " + U;
  const head = "<div class='tx-bar'><div class='tx-title'><span class='an-eyebrow'>" + esc((S.kind === "setup" ? "Setup" : "Hold") + " paths in") + "</span>" +
    "<h3><code>" + esc(L.corner) + "</code>" + (words ? "<small>" + esc(words) + "</small>" : "") + "</h3></div>" +
    "<div class='tx-tools'>" + seg + chips + find + "</div></div>";
  const hist = "<div class='tx-histbox'><div class='tx-hist' id='tx-hist' role='img' aria-label='" + esc(L.total + " endpoints in " + L.corner + ", " + (L.violating || "none") + " below 0 " + U + ", the worst at " + fmtSlack(L.worst) + " " + U) + "'></div>" +
    "<p class='tx-histcap'>" + L.total + " endpoints · " + viol + (L.worst != null ? " · worst " + esc(fmtSlack(L.worst)) + " " + U : "") + binChip + "</p></div>";
  const listHead = "<div class='tx-lh' aria-hidden='true'><span>Slack <small>" + U + "</small></span><span>Endpoint</span><span>From</span><span>Kind</span>" +
    (L.base_tag ? "<span title='" + esc("Change against the baseline run " + L.base_tag + ", same endpoint and corner") + "'>Δ <small>" + U + "</small></span>" : "") + "</div>";
  const list = "<div class='tx-list" + (L.base_tag ? " has-base" : "") + "'>" + listHead +
    "<div class='tx-scroll' id='tx-scroll' role='listbox' tabindex='0' aria-label='Endpoints, worst first' aria-activedescendant='" + (S.sel != null ? "tx-o-" + S.sel : "") + "'>" +
    "<div class='tx-pad' id='tx-pad'></div></div>" +
    "<p class='tx-lfoot'>" + esc(shown.length === L.total ? L.total + " endpoints in this corner, worst first." : shown.length + " of " + L.total + " endpoints shown.") +
    " Each row is the worst path to that endpoint, as OpenSTA reported it." +
    (L.truncated ? " OpenSTA's report lists the worst 1000 per path group; the violation count above comes from the metrics." : "") + "</p></div>";
  const src = L.source || {};
  card.innerHTML = head + hist + "<div class='tx-split'>" + list + "<div class='tx-detail' id='tx-detail' aria-live='polite'>" +
    (shown.length ? "<div class='tx-loading'>Reading the path…</div>" : "<div class='tx-empty'><div><b>No endpoint matches.</b><p>Clear the search or the filters above.</p></div></div>") + "</div></div>" +
    "<p class='an-foot tx-foot'>From <code>" + esc(src.rel || "") + "</code>" + (src.tag && src.tag !== S.tag ? " in run " + esc(src.tag) + " (this run was resumed from it)" : "") +
    ". Times in " + esc(U) + "; capacitance in fF.</p>";
  paintList();
  drawHist();
}

function rowHtml(r, top) {
  const k = KIND_WORDS[r.kind] || { short: r.kind };
  const d = S.list.base_tag && isNum(r.base) ? r.slack - r.base : null;
  const bad = r.slack < 0;
  return "<div class='tx-row" + (r.i === S.sel ? " is-sel" : "") + (bad ? " is-bad" : "") + "' role='option' id='tx-o-" + r.i + "' aria-selected='" + (r.i === S.sel) +
    "' data-tx-i='" + r.i + "' style='top:" + top + "px'>" +
    "<span class='tx-sl'>" + esc(fmtSlack(r.slack)) + "</span>" +
    "<span class='tx-ep' title='" + esc(r.end + " (" + r.end_desc + ")") + "'>" + esc(r.end) + "</span>" +
    "<span class='tx-fr' title='" + esc(r.start + " (" + r.start_desc + ")") + "'>" + esc(r.start) + "</span>" +
    "<span class='tx-kd'>" + esc(k.short) + "</span>" +
    (S.list.base_tag ? "<span class='tx-dl" + (d == null ? "" : d < -0.0005 ? " is-worse" : d > 0.0005 ? " is-better" : "") + "' title='" +
      esc(isNum(r.base) ? "In " + S.list.base_tag + ": " + fmtSlack(r.base) + " " + U : "Not in " + S.list.base_tag) + "'>" + (d == null ? "—" : esc(fmtSlack(d))) + "</span>" : "") +
    "</div>";
}

// A windowed list: only the rows in view are in the DOM (a report holds up
// to 1000 paths per path group).
function paintList() {
  const sc = document.getElementById("tx-scroll"), pad = document.getElementById("tx-pad");
  if (!sc || !pad) return;
  const rows = rowsShown();
  pad.style.height = rows.length * ROW_H + "px";
  const draw = () => {
    const h = sc.clientHeight || 480;
    const first = Math.max(0, Math.floor(sc.scrollTop / ROW_H) - 8);
    const last = Math.min(rows.length, first + Math.ceil(h / ROW_H) + 16);
    let out = "";
    for (let i = first; i < last; i++) out += rowHtml(rows[i], i * ROW_H);
    pad.innerHTML = out;
  };
  sc._txDraw = draw;
  draw();
}

function scrollToSel() {
  const sc = document.getElementById("tx-scroll");
  const rows = rowsShown();
  const k = rows.findIndex((r) => r.i === S.sel);
  if (!sc || k < 0) return;
  const top = k * ROW_H;
  if (top < sc.scrollTop) sc.scrollTop = top;
  else if (top + ROW_H > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + ROW_H - sc.clientHeight;
  sc._txDraw && sc._txDraw();
  sc.setAttribute("aria-activedescendant", "tx-o-" + S.sel);
}

function select(i, { focusList = false } = {}) {
  if (i == null || i === S.sel) return;
  S.sel = i;
  scrollToSel();
  loadDetail(i);
  if (focusList) document.getElementById("tx-scroll")?.focus();
}

async function drawHist() {
  const el = document.getElementById("tx-hist");
  const h = S.list && S.list.histogram;
  if (!el || !h || !h.counts || !h.counts.length) { if (el) el.innerHTML = ""; return; }
  if (!window.echarts) {
    try { await (await import("./loading.js")).loadECharts(); } catch (_e) {
      el.innerHTML = "<p class='muted'>Couldn't load the chart library. The endpoints are listed below.</p>";
      return;
    }
  }
  const node = document.getElementById("tx-hist");
  if (!node) return;
  const opt = histogramOption({ edges: h.edges, counts: h.counts, unit: U, what: "endpoints" });
  if (!opt) return;
  opt.grid = { left: 6, right: 14, top: 22, bottom: 24, containLabel: true };
  // The zero line stays; its words are in the caption under the chart (a
  // label on a short chart is drawn sideways over the bars).
  const ml = opt.series && opt.series[0] && opt.series[0].markLine;
  if (ml) ml.label = { show: false };
  let chart = window.echarts.getInstanceByDom(node);
  if (!chart) chart = window.echarts.init(node);
  chart.setOption(opt, true);
  chart.off("click");
  chart.on("click", (p) => {
    const k = p && p.value ? p.value[3] : null;
    if (k == null) return;
    S.bin = [h.edges[k], h.edges[k + 1]];
    paintCard();
  });
  requestAnimationFrame(() => chart.resize());
}

async function loadDetail(i) {
  const my = ++S.dtoken;
  const host = document.getElementById("tx-detail");
  if (!host) return;
  host.classList.add("is-loading");
  let p;
  try {
    p = await api.timingPath({ tag: S.tag, kind: S.kind, corner: S.corner, i, base: S.base });
  } catch (ex) {
    if (my !== S.dtoken) return;
    host.classList.remove("is-loading");
    host.innerHTML = "<div class='tx-empty is-fail'>" + icon("alert", { size: 18 }) + "<div><b>Couldn't read this path.</b><p>" + esc(ex.message || ex) + "</p></div></div>";
    return;
  }
  if (my !== S.dtoken) return;
  S.detail = p;
  host.classList.remove("is-loading");
  host.innerHTML = p.ok ? pathDetailHtml(p) : "<div class='tx-empty is-fail'><div><b>" + esc(p.error || "Couldn't read this path.") + "</b></div></div>";
}

// ---------------------------------------------------------------- the path
const PARTS = [
  ["launch", "launch clock", "var(--chart-4)"], ["io_in", "input delay", "var(--chart-2)"], ["drive", "input driver", "var(--chart-2)"],
  ["clk_q", "clock → Q", "var(--chart-5)"], ["cells", "logic", "var(--chart-1)"], ["wires", "wires", "var(--chart-3)"],
];

function budgetHtml(p) {
  const b = p.parts || {};
  if (!isNum(p.arrival) || !isNum(p.required)) return "";
  const hold = p.check === "hold" || p.check === "removal";
  const t0 = (p.launch && p.launch.edge) || 0;
  const span = Math.max(p.arrival, p.required, isNum(p.period) ? t0 + p.period : 0) - Math.min(t0, 0);
  const scale = (span || 1) * (hold ? 1.18 : 1.04);
  const pct = (v) => (Math.max(0, v) / scale) * 100;
  const segs = PARTS.filter(([k]) => (b[k] || 0) > 0.0005).map(([k, label, c]) =>
    "<i style='width:" + pct(b[k]).toFixed(2) + "%;background:" + c + "' data-an-tip='" + esc(label + ": " + b[k].toFixed(3) + " " + U) + "'></i>").join("");
  const bad = !p.met;
  const reqTip = (hold ? "Must not arrive before " : "Needed by ") + p.required.toFixed(3) + " " + U;
  const over = bad
    ? hold ? "<span class='dx-bud-over' style='left:" + pct(p.arrival - t0).toFixed(2) + "%;width:" + pct(p.required - p.arrival).toFixed(2) + "%'></span>"
      : "<span class='dx-bud-over' style='left:" + pct(p.required - t0).toFixed(2) + "%;width:" + pct(p.arrival - p.required).toFixed(2) + "%'></span>"
    : "";
  return "<div class='tx-sec'><div class='vf-k'>Where the time goes</div><div class='dx-bud' role='img' aria-label='" +
    esc("Arrives at " + p.arrival.toFixed(3) + " " + U + "; " + reqTip.toLowerCase()) + "'>" +
    "<div class='dx-bud-bar'>" + segs +
      "<span class='dx-bud-req' style='left:" + pct(p.required - t0).toFixed(2) + "%' data-an-tip='" + esc(reqTip) + "'></span>" + over +
      (isNum(p.period) ? "<span class='dx-bud-per' style='left:" + pct(p.period).toFixed(2) + "%' data-an-tip='" + esc("The capture clock edge, " + p.period.toFixed(3) + " " + U + " after the launch edge") + "'></span>" : "") +
    "</div><div class='dx-bud-k'>" + PARTS.filter(([k]) => (b[k] || 0) > 0.0005).map(([k, label, c]) =>
      "<span><i style='background:" + c + "'></i>" + esc(label) + " <b>" + b[k].toFixed(3) + "</b></span>").join("") +
    "<span class='dx-bud-sum'>arrives <b>" + p.arrival.toFixed(3) + "</b> · " + (hold ? "must not arrive before" : "needed by") + " <b>" + p.required.toFixed(3) + "</b> " + U + "</span></div></div></div>";
}

function sumHtml(label, total, terms, cls) {
  return "<div class='tx-sum " + (cls || "") + "'><span class='vf-k'>" + esc(label) + "</span><span class='tx-sum-t'><b>" + esc(n3(total)) + "</b><span class='tx-eq'>=</span>" +
    (terms || []).map((t, i) => "<span class='tx-term" + (Math.abs(t.v) < 0.0005 && i > 0 ? " is-zero" : "") + "'" + (termTip(t.word) ? " tabindex='0' data-an-tip='" + esc(termTip(t.word)) + "'" : "") + ">" +
      (i === 0 ? "" : "<span class='tx-op'>" + (t.v < 0 ? "−" : "+") + "</span>") + "<b>" + Math.abs(t.v).toFixed(3) + "</b> " + esc(t.word) + "</span>").join("") + "</span></div>";
}

function pinCell(r) {
  if (r.kind === "port") return "<code>" + esc(r.port) + "</code><small>" + (r.dir === "in" ? "input port" : "output port") + "</small>";
  if (r.kind === "pin") return "<code title='" + esc(r.pin + " (" + r.lib + ")") + "'>" + esc(r.pin) + "</code><small>" + esc(r.cell || "") + "</small>";
  return "<span class='tx-tw'>" + esc(r.word || r.label || "") + "</span>";
}

function stageRows(rows, scale) {
  return rows.map((r) => {
    const wire = (r.kind === "pin" || r.kind === "port") && !r.driver;
    const bar = isNum(r.delay) && r.delay > 0 && scale > 0 ? "<i class='tx-ibar' style='width:" + Math.min(100, (r.delay / scale) * 100).toFixed(1) + "%'></i>" : "";
    return "<tr class='tx-r-" + esc(r.kind) + (wire ? " is-wire" : "") + "'>" +
      "<td class='tx-pin'>" + pinCell(r) + (r.net && r.driver && r.net !== r.port ? "<small class='tx-net'>drives " + esc(r.net) + "</small>" : "") + "</td>" +
      "<td class='tx-edge'>" + (r.edge ? "<span aria-label='" + (r.edge === "r" ? "rising" : "falling") + "'>" + (r.edge === "r" ? "↑" : "↓") + "</span>" : "") + "</td>" +
      "<td class='n'>" + (isNum(r.fanout) ? r.fanout : "") + "</td>" +
      "<td class='n'>" + (isNum(r.cap) ? (r.cap * 1000).toFixed(2) : "") + "</td>" +
      "<td class='n'>" + n3(r.slew) + "</td>" +
      "<td class='n tx-incr'>" + bar + "<span>" + n3(r.delay) + "</span>" + (wire && isNum(r.delay) ? "<small>wire</small>" : "") + "</td>" +
      "<td class='n'>" + n3(r.time) + "</td></tr>";
  }).join("");
}

function foldRow(part, sec, scale) {
  const open = S.open[part];
  const words = part === "launch"
    ? (sec.cells || sec.to ? "Launch clock: " + sec.cells + " cell" + (sec.cells === 1 ? "" : "s") + (sec.to ? " to " + sec.to : "") : "Launch: input port, no clock tree")
    : (sec.cells || sec.to ? "Capture clock: " + sec.cells + " cell" + (sec.cells === 1 ? "" : "s") + (sec.to ? " to " + sec.to : "") : "Capture: output port, no clock tree");
  return "<tr class='tx-fold'><td colspan='7'><button class='tx-foldb' data-tx-fold='" + part + "' aria-expanded='" + open + "'>" +
    "<span class='vf-chev'>" + icon("chevron", { size: 12 }) + "</span>" + esc(words) + (sec.ideal ? " (ideal clock)" : "") +
    "<b>" + esc(n3(sec.delay)) + " " + U + "</b></button></td></tr>" + (open ? stageRows(sec.rows || [], scale) : "");
}

export function pathDetailHtml(p) {
  const hold = p.check === "hold" || p.check === "removal";
  const k = KIND_WORDS[p.kind] || { short: p.kind };
  const endPin = lastPin(p) || p.end;
  const sl = p.met
    ? "<span class='tx-slack is-ok'>" + esc(fmtSlack(p.slack)) + " " + U + "</span><span class='vf-pill is-pass'>Met</span>"
    : "<span class='tx-slack is-bad'>Missed by " + esc(Math.abs(p.slack).toFixed(3)) + " " + U + "</span><span class='vf-pill is-fail'>Violated</span>";
  // A corner whose (very big) report isn't read yet shows "open": a click reads it.
  const others = (p.others || []).filter((o) => isNum(o.slack) || o.unread);
  const worstO = others.some((o) => isNum(o.slack)) ? Math.min(...others.filter((o) => isNum(o.slack)).map((o) => o.slack)) : null;
  const scale = isNum(p.period) && p.period > 0 ? p.period : Math.max(0.05, p.arrival || 0);
  const base = p.base && isNum(p.base.slack) ? "<span class='tx-base' title='Same endpoint and corner in the baseline run'>In " + esc(p.base.tag) + ": <b>" + esc(fmtSlack(p.base.slack)) + "</b> " + U + " (Δ " +
    esc(fmtSlack(p.slack - p.base.slack)) + ")</span>" : p.base ? "<span class='tx-base'>Not in " + esc(p.base.tag) + "</span>" : "";
  return "<div class='tx-dh'><div class='tx-dh-l'>" + sl + "<span class='tx-meta'>" + esc(p.check + " · " + p.corner + " · path group " + (p.group || "—") + " · " + k.short) + "</span></div>" +
      "<div class='tx-acts'>" +
        "<button class='btn btn-ghost btn-sm' data-tx-act='layout' title='Highlights this path&#39;s cells and wires on the layout of this run.'>" + icon("layers", { size: 13 }) + " Show on layout</button>" +
        "<button class='btn btn-ghost btn-sm' data-tx-act='report' title='" + esc("Open " + (p.source && p.source.rel) + " at this path, as OpenSTA wrote it") + "'>" + icon("eye", { size: 13 }) + " In the report</button>" +
        "<button class='btn btn-ghost btn-sm' data-tx-act='copy' title='Copy this path as OpenSTA printed it'>" + icon("file", { size: 13 }) + " Copy</button>" +
      "</div></div>" +
    "<p class='tx-route'><code>" + esc(p.start) + "</code><small>" + esc(p.start_in ? "input port" : "flip-flop") + "</small><span class='tx-arrow'>→</span><code>" + esc(endPin) + "</code>" +
      "<small>" + esc(p.end_out ? "output port" : (p.end_cell || "")) + "</small>" + base + "</p>" +
    budgetHtml(p) +
    "<div class='tx-sums'>" + sumHtml(hold ? "Must arrive after" : "Needed by", p.required, p.required_terms, "is-req") +
      sumHtml("Arrives at", p.arrival, p.arrival_terms, "is-arr") + "</div>" +
    "<div class='tx-tscroll'><table class='data-table tx-table'><thead><tr><th>Pin</th><th><span class='sr-only'>Edge</span></th><th class='n'>Fanout</th><th class='n'>Cap <small>fF</small></th>" +
      "<th class='n'>Slew <small>" + U + "</small></th><th class='n'>Incr <small>" + U + "</small></th><th class='n'>Arrival <small>" + U + "</small></th></tr></thead><tbody>" +
      foldRow("launch", p.launch || {}, scale) + stageRows((p.data && p.data.rows) || [], scale) + foldRow("capture", p.capture || {}, scale) +
    "</tbody></table></div>" +
    (others.length > 1 ? "<div class='tx-others'><span class='vf-k'>Same endpoint in other corners</span><div class='tx-ochips'>" +
      others.map((o) => "<button class='tx-ochip" + (o.corner === p.corner ? " is-sel" : "") + (o.slack < 0 ? " is-bad" : "") + (o.slack === worstO ? " is-worst" : "") +
        "' data-tx-corner='" + esc(o.corner) + "' data-tx-ci='" + o.i + "' data-an-tip='" + esc(o.corner + " — " + cornerWords(o.corner)) + "'>" +
        "<span>" + esc(o.corner) + "</span><b>" + esc(o.unread ? "open" : fmtSlack(o.slack)) + "</b></button>").join("") + "</div></div>" : "");
}

// ---------------------------------------------------------------- events
function wire(host) {
  if (host._txWired) return;
  host._txWired = true;
  wireGridKeys(host);
  host.addEventListener("click", (e) => {
    const t = e.target;
    const cg = t.closest(".cg-b");
    if (cg) {
      const kind = cg.dataset.cgKind, corner = cg.dataset.cgCorner;
      if (kind === S.kind && corner === S.corner) return;
      S.kind = kind; S.corner = corner; S.bin = null; S.sel = null;
      loadList();
      return;
    }
    const kb = t.closest("[data-tx-kind]");
    if (kb) {
      if (kb.dataset.txKind === S.kind) return;
      S.kind = kb.dataset.txKind;
      // Keep the corner only when it is the worst one for the new check too.
      S.corner = null; S.bin = null; S.sel = null;
      loadList();
      return;
    }
    const ob = t.closest("[data-tx-only]");
    if (ob) { S.only = ob.dataset.txOnly; S.bin = null; loadList({ keepSel: true }); return; }
    if (t.closest("[data-tx-bin]")) { S.bin = null; paintCard(); if (S.sel != null) loadDetail(S.sel); return; }
    const row = t.closest("[data-tx-i]");
    if (row) { select(Number(row.dataset.txI)); return; }
    const fold = t.closest("[data-tx-fold]");
    if (fold) { S.open[fold.dataset.txFold] = !S.open[fold.dataset.txFold]; if (S.detail) document.getElementById("tx-detail").innerHTML = pathDetailHtml(S.detail); return; }
    const oc = t.closest("[data-tx-corner]");
    if (oc) {
      if (oc.dataset.txCorner === S.corner) return;
      S.corner = oc.dataset.txCorner; S.bin = null;
      const want = oc.dataset.txCi === "null" || oc.dataset.txCi === "undefined" ? null : Number(oc.dataset.txCi);
      loadList().then(() => { if (want != null) select(want); });
      return;
    }
    const act = t.closest("[data-tx-act]");
    if (act && S.detail) action(act.dataset.txAct, S.detail);
  });
  host.addEventListener("input", (e) => {
    if (e.target.id !== "tx-q") return;
    clearTimeout(host._txQ);
    host._txQ = setTimeout(() => { S.q = e.target.value; S.bin = null; loadList({ keepSel: true }).then(() => document.getElementById("tx-q")?.focus()); }, 260);
  });
  host.addEventListener("scroll", (e) => { if (e.target.id === "tx-scroll" && e.target._txDraw) e.target._txDraw(); }, true);
  host.addEventListener("keydown", (e) => {
    if (e.target.id !== "tx-scroll") return;
    const rows = rowsShown();
    const k = rows.findIndex((r) => r.i === S.sel);
    const page = Math.max(1, Math.floor((e.target.clientHeight || 400) / ROW_H) - 1);
    const to = { ArrowDown: k + 1, ArrowUp: k - 1, Home: 0, End: rows.length - 1, PageDown: k + page, PageUp: k - page }[e.key];
    if (to === undefined) {
      if (e.key === "Enter") document.querySelector("#tx-detail .tx-acts .btn")?.focus();
      return;
    }
    e.preventDefault();
    const r = rows[Math.max(0, Math.min(rows.length - 1, to))];
    if (r) select(r.i);
  });
}

async function action(what, p) {
  if (what === "layout") {
    const { go } = await import("./nav.js");
    go("layout", { run: S.tag, highlight: highlightOf(p, S.tag) });
    return;
  }
  if (what === "copy") {
    try { await navigator.clipboard.writeText(p.text || ""); toast.show("Path copied as OpenSTA printed it", "success"); }
    catch (_e) { toast.show("Clipboard not available", "warn"); }
    return;
  }
  if (what === "report") openReportAt(p.source && p.source.tag, p.source && p.source.rel, p.line);
}

// A report in the file viewer, at a line.
export async function openReportAt(tag, rel, line) {
  if (!tag || !rel) return;
  const { customDialog } = await import("./dialog.js");
  customDialog({
    title: rel, wide: true,
    bodyHtml: "<div class='vf-viewer'><p class='muted'>Loading " + esc(rel) + "…</p></div>",
    onMount: async (dlg) => {
      const host = dlg.querySelector(".vf-viewer");
      try {
        const resp = await fetch(api.runFileUrl(tag, rel), { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        let text = await resp.text();
        const cap = 8 * 1024 * 1024;
        if (text.length > cap) text = text.slice(0, cap) + "\n\n… (first 8 MB shown; download the file for the rest)";
        renderFileText(host, text, { tag, path: rel, title: rel, line: Number.isInteger(line) ? line : null });
      } catch (ex) {
        host.innerHTML = "<p class='pill pill-fail'>Couldn't load " + esc(rel) + ": " + esc(ex.message || ex) + "</p>";
      }
    },
  });
}
