// cornergrid.js — a PVT corner set as a small grid: process across (fast,
// typical, slow), wire parasitics down (min, nominal, max RC), one number per
// cell. Reads as "which corner is worst, and does it fail the run" at a glance,
// where a 9-row table makes you scan. Used by Verify's timing explorer; any
// per-corner number (slack, power, Fmax) can use it.
//
//   parseCorner("max_ss_100C_1v60") -> { rc: "max", process: "ss", speed: "slow", temp: 100, volt: 1.6 }
//   cornerWords("max_ss_100C_1v60") -> "slow process, 100 °C, 1.60 V, max wire RC"
//   cornerGridHtml(rows, { value, selected, gate, fmt, tip, kind }) -> HTML
//
// A corner set that doesn't parse into rows and columns (IHP's
// nom_fast_1p32V_m40C has one RC, fine; an unknown naming isn't) falls back to
// one row ordered slow -> typical -> fast.

const PROC = {
  ff: "fast", fast: "fast", f: "fast",
  tt: "typical", typ: "typical", typical: "typical", t: "typical",
  ss: "slow", slow: "slow", s: "slow",
  sf: "skewed", fs: "skewed",
};
const SPEED_ORDER = { fast: 0, typical: 1, skewed: 2, slow: 3 };
const RC_ORDER = { min: 0, nom: 1, max: 2 };
const RC_WORDS = { min: "min wire RC", nom: "nominal wire RC", max: "max wire RC" };

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function parseCorner(name) {
  const out = { name: String(name || ""), rc: null, process: null, speed: null, temp: null, volt: null };
  for (const raw of out.name.split("_")) {
    const t = raw.toLowerCase();
    if (t in RC_ORDER && out.rc === null) { out.rc = t; continue; }
    if (t in PROC && out.process === null) { out.process = t; out.speed = PROC[t]; continue; }
    let m = /^([nm])?(\d+(?:p\d+)?)c$/.exec(t);
    if (m) { out.temp = (m[1] ? -1 : 1) * Number(m[2].replace("p", ".")); continue; }
    m = /^(\d+)[vp](\d+)v?$/.exec(t);
    if (m) { out.volt = Number(m[1] + "." + m[2]); continue; }
  }
  return out;
}

export function cornerWords(name) {
  const c = parseCorner(name);
  const parts = [];
  if (c.speed) parts.push(c.speed + " process");
  if (c.temp !== null) parts.push(c.temp + " °C");
  if (c.volt !== null) parts.push(c.volt.toFixed(2) + " V");
  if (c.rc) parts.push(RC_WORDS[c.rc]);
  return parts.join(", ");
}

// Short column / row heads: "FF −40°C", "MAX RC".
function colHead(c) {
  const p = (c.process || "?").toUpperCase();
  return p + (c.temp !== null ? " " + (c.temp < 0 ? "−" + Math.abs(c.temp) : c.temp) + "°C" : "");
}

// The layout of a corner set: { grid: bool, cols: [key], rows: [key], at(rc, col) -> name }.
export function cornerLayout(names) {
  const parsed = names.map(parseCorner);
  const ok = parsed.length > 1 && parsed.every((c) => c.speed);
  if (!ok) {
    return { grid: false, cols: names.slice(), rows: [""], cells: new Map(names.map((n) => ["|" + n, n])), parsed };
  }
  const colKey = (c) => c.process + "|" + (c.temp ?? "") + "|" + (c.volt ?? "");
  const cols = [...new Set(parsed.map(colKey))].sort((a, b) => {
    const pa = parsed.find((c) => colKey(c) === a), pb = parsed.find((c) => colKey(c) === b);
    return (SPEED_ORDER[pa.speed] - SPEED_ORDER[pb.speed]) || String(a).localeCompare(String(b));
  });
  const rows = [...new Set(parsed.map((c) => c.rc || ""))].sort((a, b) => (RC_ORDER[a] ?? 9) - (RC_ORDER[b] ?? 9));
  const cells = new Map();
  parsed.forEach((c) => cells.set((c.rc || "") + "|" + colKey(c), c.name));
  // Two corners in one cell (a naming we don't understand): fall back.
  if (cells.size !== names.length) return { grid: false, cols: names.slice(), rows: [""], cells: new Map(names.map((n) => ["|" + n, n])), parsed };
  return { grid: true, cols, rows, cells, parsed };
}

// rows: [{ corner, … }]; value(row) -> number|null; gate(row) -> true|false|undefined
// (fails the run here / only warns); fmt(v) -> text; tip(row) -> tooltip text.
export function cornerGridHtml(rows, { value, selected = "", gate = () => undefined, fmt = (v) => String(v), tip = () => "", kind = "", label = "" } = {}) {
  const names = rows.map((r) => r.corner);
  if (!names.length) return "";
  const by = new Map(rows.map((r) => [r.corner, r]));
  const L = cornerLayout(names);
  const vals = rows.map((r) => value(r)).filter((v) => typeof v === "number" && Number.isFinite(v));
  const worst = vals.length ? Math.min(...vals) : null;
  const cell = (name, ri, ci) => {
    const r = name ? by.get(name) : null;
    if (!r) return "<td class='cg-c is-none'><span>—</span></td>";
    const v = value(r);
    const num = typeof v === "number" && Number.isFinite(v);
    const g = gate(r);
    const cls = ["cg-c", num && v < 0 ? (g === false ? "is-warn" : "is-bad") : "", num && v === worst ? "is-worst" : "",
      name === selected ? "is-sel" : "", !num ? "is-none" : ""].filter(Boolean).join(" ");
    return "<td class='" + cls + "'><button type='button' class='cg-b' data-cg-corner='" + esc(name) + "' data-cg-kind='" + esc(kind) + "'" +
      " data-r='" + ri + "' data-c='" + ci + "' tabindex='" + (name === selected || (!selected && ri === 0 && ci === 0) ? "0" : "-1") + "'" +
      " aria-pressed='" + (name === selected) + "' aria-label='" + esc(name + ": " + (num ? fmt(v) : "not timed") + (g === true ? ", fails the run here" : g === false ? ", only warns here" : "")) + "'" +
      " data-an-tip='" + esc(tip(r)) + "'>" +
      "<span class='cg-v'>" + esc(num ? fmt(v) : "not timed") + "</span>" +
      (g === undefined ? "" : "<span class='cg-g" + (g ? " is-on" : "") + "' aria-hidden='true'>" + (g ? "●" : "○") + "</span>") +
      (num && v === worst && vals.length > 1 ? "<span class='cg-w' aria-hidden='true'>◆</span>" : "") +
      "</button></td>";
  };
  let head, body;
  if (L.grid) {
    const colInfo = L.cols.map((k) => L.parsed.find((c) => c.process + "|" + (c.temp ?? "") + "|" + (c.volt ?? "") === k));
    head = "<tr><th scope='col'><span class='sr-only'>Wire RC</span></th>" + colInfo.map((c) => "<th scope='col' title='" + esc(cornerWords(c.name).split(", ").slice(0, 3).join(", ")) + "'>" + esc(colHead(c)) + "</th>").join("") + "</tr>";
    body = L.rows.map((rk, ri) => "<tr><th scope='row'>" + esc(rk ? rk.toUpperCase() + " RC" : "") + "</th>" +
      L.cols.map((ck, ci) => cell(L.cells.get(rk + "|" + ck), ri, ci)).join("") + "</tr>").join("");
  } else {
    const order = [...names].sort((a, b) => {
      const pa = parseCorner(a), pb = parseCorner(b);
      return ((SPEED_ORDER[pb.speed] ?? 9) - (SPEED_ORDER[pa.speed] ?? 9)) || a.localeCompare(b);
    });
    head = "<tr>" + order.map((n) => "<th scope='col'>" + esc(n) + "</th>").join("") + "</tr>";
    body = "<tr>" + order.map((n, ci) => cell(n, 0, ci)).join("") + "</tr>";
  }
  return "<table class='cg" + (L.grid ? "" : " is-strip") + "' role='grid' aria-label='" + esc(label) + "'><thead>" + head + "</thead><tbody>" + body + "</tbody></table>";
}

// Arrow keys move between the cells of a grid (one tab stop per grid).
export function wireGridKeys(root) {
  if (!root || root._cgKeys) return;
  root._cgKeys = true;
  root.addEventListener("keydown", (e) => {
    const b = e.target.closest && e.target.closest(".cg-b");
    if (!b) return;
    const d = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowDown: [1, 0], ArrowUp: [-1, 0] }[e.key];
    if (!d) return;
    const table = b.closest("table");
    const r = Number(b.dataset.r) + d[0], c = Number(b.dataset.c) + d[1];
    const next = table.querySelector(".cg-b[data-r='" + r + "'][data-c='" + c + "']");
    if (next) {
      e.preventDefault();
      table.querySelectorAll(".cg-b").forEach((x) => x.setAttribute("tabindex", x === next ? "0" : "-1"));
      next.focus();
    }
  });
}
