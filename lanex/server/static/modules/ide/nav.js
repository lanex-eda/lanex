// nav.js — getting around a project in the RTL IDE, the way code editors do
// it: F12 / Ctrl+click goes to a definition, Shift+F12 lists the references
// (the References panel tab), hovering a name shows its declaration, Alt+←
// and Alt+→ walk back and forward through the jumps, and Ctrl+P opens any
// file (@ symbols in this file, # symbols in the project, : a line).
//
// Every answer comes from the server's project index (controller/hdlindex.py),
// with the unsaved tabs sent along so the answers match the screen.
//
//   const nav = initNav(ctx)   ctx = { editor(), activeRel(), dirty(), files(),
//     topRel(), openAt(rel, line, col, len), setPanel(name), note(msg, bad), designDir() }
import { api, fmt } from "../api.js";
import { rank, markHits } from "./fuzzy.js";
import { openPicker } from "./picker.js";

const esc = (s) => fmt.escape(String(s === undefined || s === null ? "" : s));
const KIND_WORD = {
  module: "module", interface: "interface", package: "package", program: "program", class: "class",
  entity: "entity", architecture: "architecture", component: "component", port: "port", param: "parameter",
  localparam: "localparam", net: "wire", var: "variable", signal: "signal", constant: "constant",
  genvar: "genvar", function: "function", task: "task", block: "block", typedef: "type", enum: "enum value",
  macro: "macro", instance: "instance",
};
export function kindWord(k) { return KIND_WORD[k] || k || ""; }

const ROLE_TAG = { design: "DESIGN", testbench: "TESTBENCH", header: "HEADER", vhdl: "VHDL", timing: "CONSTRAINTS",
  pins: "PINS", config: "CONFIG", memory: "MEMORY", other: "" };

let ctx = null;
const hist = { back: [], fwd: [] };
let lastRefs = null;

export function initNav(c) {
  ctx = c;
  const ed = ctx.editor();
  ed.onDefinition = (o) => goToDefinition(o);
  ed.onReferences = () => findReferences();
  ed.onBack = (dir) => walk(dir);
  wireRefsPane();
  return { goToDefinition, findReferences, quickOpen, declHover, walk, pushHere, refreshIndexStatus };
}

function here() {
  const rel = ctx.activeRel();
  if (!rel) return null;
  const p = ctx.editor().cursorPos();
  return { rel, line: p.line, col: p.col };
}

// Remember where the cursor was before a jump (Alt+← comes back here).
export function pushHere() {
  const h = here();
  if (!h) return;
  const top = hist.back[hist.back.length - 1];
  if (top && top.rel === h.rel && top.line === h.line) return;
  hist.back.push(h);
  if (hist.back.length > 100) hist.back.shift();
  hist.fwd.length = 0;
}

async function walk(dir) {
  const from = dir < 0 ? hist.back : hist.fwd;
  const to = dir < 0 ? hist.fwd : hist.back;
  if (!from.length) return false;
  const h = here();
  const go = from.pop();
  if (h) to.push(h);
  await ctx.openAt(go.rel, go.line, go.col, 0);
  return true;
}

async function jump(o) {
  pushHere();
  await ctx.openAt(o.rel, o.line, o.col, o.len || 0);
}

function placeText(o) {
  return o.rel + ":" + o.line;
}

// F12: one definition jumps, several open a short list (built files first),
// none says why. On a definition itself it lists the references instead.
export async function goToDefinition({ mouse = false } = {}) {
  const h = here();
  if (!h) return;
  let r;
  try {
    r = await api.ideDefinition({ rel: h.rel, line: h.line, col: h.col, dirty: ctx.dirty() });
  } catch (ex) {
    ctx.note("Couldn't look that up: " + (ex.message || ex), true);
    return;
  }
  if (!r.name) { if (!mouse) ctx.note(r.note || "Put the cursor on a name first."); return; }
  const stale = r.index && !r.index.ready ? " (still indexing the project, so this may miss files not read yet)" : "";
  const res = r.results || [];
  if (!res.length) { ctx.note((r.note || "No definition of " + r.name + " found.") + stale, true); return; }
  if (r.at_definition && res.length === 1) { findReferences(); return; }
  if (res.length === 1) {
    await jump(res[0]);
    ctx.note(r.name + ": " + kindWord(res[0].kind) + " in " + placeText(res[0]) + (r.via ? " (through " + r.via + ")" : "") + "." + stale);
    return;
  }
  openPicker({
    label: "Definitions of " + r.name,
    placeholder: r.name + " is defined in " + res.length + " places. Pick one.",
    hint: "<kbd>↑</kbd><kbd>↓</kbd> choose · <kbd>↵</kbd> open · <kbd>esc</kbd> close",
    items: async (q) => ({
      rows: res.filter((o) => !q || (o.rel + " " + o.text).toLowerCase().includes(q.toLowerCase())).map((o) => ({
        html: "<span class='qp-name'>" + esc(o.name) + "</span> <span class='qp-kind'>" + esc(kindWord(o.kind)) + "</span>",
        detail: esc(placeText(o)) + " · <code>" + esc(o.text.trim().slice(0, 90)) + "</code>",
        tag: o.built ? "BUILT" : o.role === "testbench" ? "TESTBENCH" : "NOT BUILT",
        tagCls: o.built ? "is-on" : "",
        o,
      })),
    }),
    onPick: (row) => jump(row.o),
  });
}

// ------------------------------------------------------------------ references

export async function findReferences() {
  const h = here();
  if (!h) return;
  const host = document.getElementById("ide-refs");
  ctx.setPanel("refs");
  if (host) host.innerHTML = "<div class='pb-empty'><p>Looking up the references…</p></div>";
  let r;
  try {
    r = await api.ideReferences({ rel: h.rel, line: h.line, col: h.col, dirty: ctx.dirty() });
  } catch (ex) {
    if (host) host.innerHTML = "<div class='pb-empty pb-failed'><p><b>Couldn't list the references.</b> " + esc(ex.message || ex) + "</p></div>";
    return;
  }
  lastRefs = r;
  renderRefs(r);
}

function chip(o) {
  if (o.tag === "declared" || o.tag === "defined") return "<span class='rf-chip is-decl'>" + (o.tag === "defined" ? "DEFINED" : "DECLARED") + "</span>";
  if (o.write) return "<span class='rf-chip is-write'>ASSIGNED</span>";
  if (o.tag) return "<span class='rf-chip'>" + esc(o.tag.toUpperCase()) + "</span>";
  return "";
}

function lineHtml(o) {
  const t = o.text || "";
  const lead = t.length - t.trimStart().length;
  const a = Math.max(0, o.col - 1 - lead), b = a + (o.len || 0);
  const s = t.trimStart();
  return esc(s.slice(0, a)) + "<mark>" + esc(s.slice(a, b)) + "</mark>" + esc(s.slice(b));
}

function renderRefs(r) {
  const host = document.getElementById("ide-refs");
  const count = document.getElementById("ide-refs-count");
  if (count) count.textContent = r && r.results ? String(r.total) : "";
  if (!host) return;
  if (!r || !r.name) {
    host.innerHTML = "<div class='pb-empty'><p><b>No references listed yet.</b> Put the cursor on a name and press " +
      "<kbd>Shift</kbd>+<kbd>F12</kbd> (or right-click it in the editor) to list every place it is used. " +
      "<kbd>F12</kbd> goes to where it is declared.</p></div>";
    return;
  }
  const res = r.results || [];
  const by = new Map();
  for (const o of res) { if (!by.has(o.rel)) by.set(o.rel, []); by.get(o.rel).push(o); }
  const defs = (r.definitions || []).map((d) => kindWord(d.kind) + " in " + d.rel + ":" + d.line);
  const head = "<div class='pb-bar rf-bar'><span class='an-eyebrow rf-eyebrow'>References to</span>" +
    "<code class='rf-name'>" + esc(r.name) + "</code>" +
    "<span class='pb-meta'>" + res.length + (r.capped ? "+" : "") + " in " + r.files + (r.files === 1 ? " file" : " files") +
    (defs.length ? " · " + esc(defs[0]) + (defs.length > 1 ? " and " + (defs.length - 1) + " more" : "") : "") + "</span>" +
    "<span class='ide-spacer'></span>" +
    "<button class='btn btn-ghost btn-sm' data-rf='again' title='Look again (Shift F12 on the name)'>Refresh</button></div>";
  if (!res.length) {
    host.innerHTML = head + "<div class='pb-list'><p class='pb-clean'>" + esc(r.note || "No uses found.") + "</p></div>";
    return;
  }
  let i = 0;
  const groups = [...by.entries()].map(([rel, list]) => {
    const cut = rel.lastIndexOf("/");
    const open = list.length < 60 || by.size === 1;
    return "<div class='pb-group'><button class='pb-fhead' aria-expanded='" + open + "'>" +
      "<svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M6 9l6 6 6-6'/></svg>" +
      "<span class='pb-fname'><span class='pb-dir'>" + esc(rel.slice(0, cut + 1)) + "</span>" + esc(rel.slice(cut + 1)) + "</span>" +
      "<span class='pb-fcount'>" + list.length + "</span></button><div class='pb-items'>" +
      list.slice(0, 500).map((o) => "<div class='rf-item' role='button' tabindex='0' data-i='" + (i++) + "' data-rel='" + esc(o.rel) +
        "' data-line='" + o.line + "' data-col='" + o.col + "' data-len='" + o.len + "'>" +
        "<span class='rf-ln'>" + o.line + "</span><span class='rf-text'>" + lineHtml(o) + "</span>" + chip(o) + "</div>").join("") +
      (list.length > 500 ? "<p class='pb-clean'>" + (list.length - 500) + " more in this file are not listed.</p>" : "") +
      "</div></div>";
  }).join("");
  host.innerHTML = head + "<div class='pb-list'>" +
    (r.target === "decl" && r.unit ? "" : "") + groups +
    "<p class='rf-foot'>Read from the project's text as written: code a macro generates isn't seen, and both sides of an <code>`ifdef</code> are listed." +
    (r.results.some((o) => o.write) ? " ASSIGNED marks a line that sets it (=, &lt;= or assign)." : "") + "</p></div>";
}

function wireRefsPane() {
  const host = document.getElementById("ide-refs");
  if (!host || host._wired) return;
  host._wired = true;
  renderRefs(null);
  const open = (it) => jump({ rel: it.dataset.rel, line: +it.dataset.line, col: +it.dataset.col, len: +it.dataset.len });
  host.addEventListener("click", (e) => {
    if (e.target.closest("[data-rf='again']") && lastRefs) {
      const first = (lastRefs.definitions || [])[0] || (lastRefs.results || [])[0];
      if (first) ctx.openAt(first.rel, first.line, first.col, first.len).then(() => findReferences());
      return;
    }
    const head = e.target.closest(".pb-fhead");
    if (head) { head.setAttribute("aria-expanded", head.getAttribute("aria-expanded") === "false" ? "true" : "false"); return; }
    const it = e.target.closest(".rf-item");
    if (it) open(it);
  });
  host.addEventListener("keydown", (e) => {
    const it = e.target.closest(".rf-item");
    if (!it) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(it); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const all = [...host.querySelectorAll(".rf-item")].filter((x) => x.getClientRects().length);
      const k = all.indexOf(it) + (e.key === "ArrowDown" ? 1 : -1);
      if (all[k]) all[k].focus();
    }
  });
}

// ------------------------------------------------------------------ hover

// The declaration of the name under the mouse: "wire [W:0] next · acc_top.v:13".
// A DOM node, or null. main.js puts the waveform's values under it.
export async function declHover(word, rel, pos) {
  if (!rel || !pos) return null;
  let r;
  try { r = await api.ideDefinition({ rel, line: pos.line, col: pos.col, dirty: ctx.dirty() }); } catch (_e) { return null; }
  const res = (r && r.results) || [];
  if (!res.length) return null;
  const o = res[0];
  const dom = document.createElement("div");
  dom.className = "cm-decltip";
  const code = document.createElement("code");
  code.className = "cm-decltip-c";
  const text = o.kind === "module" || o.kind === "entity" ? o.text.trim() : (o.detail || o.text.trim());
  code.textContent = text.length > 160 ? text.slice(0, 157) + "…" : text;
  const where = document.createElement("div");
  where.className = "cm-decltip-w";
  where.textContent = kindWord(o.kind) + (o.dir && o.dir !== "port" ? " (" + o.dir + ")" : "") +
    (o.default ? " · default " + o.default : "") + " · " + o.rel + ":" + o.line +
    (res.length > 1 ? " · and " + (res.length - 1) + " more" : "") + (r.via ? " · through " + r.via : "");
  dom.append(code, where);
  return dom;
}

// ------------------------------------------------------------------ quick open

const RECENT_KEY = "ll.ideRecent:";
export function noteRecent(designDir, rel) {
  if (!designDir || !rel) return;
  try {
    const k = RECENT_KEY + designDir;
    const list = JSON.parse(localStorage.getItem(k) || "[]").filter((x) => x !== rel);
    list.unshift(rel);
    localStorage.setItem(k, JSON.stringify(list.slice(0, 20)));
  } catch (_e) { /* storage blocked: no recent list */ }
}
function recent(designDir) {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY + designDir) || "[]"); } catch (_e) { return []; }
}

const HINT = "<button type='button' class='cmdk-pfx' data-qp-pfx=''><kbd>file</kbd></button> " +
  "<button type='button' class='cmdk-pfx' data-qp-pfx='@'><kbd>@</kbd> symbols in this file</button> " +
  "<button type='button' class='cmdk-pfx' data-qp-pfx='#'><kbd>#</kbd> symbols in the project</button> " +
  "<button type='button' class='cmdk-pfx' data-qp-pfx=':'><kbd>:</kbd> go to line</button>";

let _projSyms = null;   // { at, rows }

export function quickOpen(prefix = "") {
  const dir = ctx.designDir();
  if (!dir) { ctx.note("Open a project first (Setup tab).", true); return; }
  _projSyms = null;
  let fileSyms = null, fileSymsRel = null;
  openPicker({
    label: "Go to a file or symbol",
    placeholder: "Type a file name · @ symbols here · # symbols anywhere · :line",
    query: prefix,
    hint: HINT,
    items: async (q) => {
      if (q.startsWith(":")) return lineRows(q);
      if (q.startsWith("@")) {
        const rel = ctx.activeRel();
        if (!rel) return { rows: [], empty: "Open a file first: @ lists the modules, ports and signals in it." };
        if (fileSymsRel !== rel) {
          fileSymsRel = rel;
          try { fileSyms = (await api.ideSymbols({ rel, dirty: ctx.dirty() })).symbols || []; } catch (_e) { fileSyms = []; }
        }
        return symbolRows(q.slice(1), fileSyms, false);
      }
      if (q.startsWith("#")) {
        if (!_projSyms) {
          try { _projSyms = (await api.ideSymbols({ dirty: ctx.dirty() })).symbols || []; } catch (_e) { _projSyms = []; }
        }
        return symbolRows(q.slice(1), _projSyms, true);
      }
      return fileRows(q, dir);
    },
    onPick: async (row) => {
      if (row.line) { await jump({ rel: row.rel, line: row.line, col: row.col || 1, len: row.len || 0 }); return; }
      if (row.goLine) {
        const rel = ctx.activeRel();
        if (rel) { pushHere(); await ctx.openAt(rel, row.goLine, row.goCol || 1, 0); }
        return;
      }
      pushHere();
      await ctx.openAt(row.rel, 0, 0, 0);
    },
  });
}

function lineRows(q) {
  const m = q.match(/^:\s*(\d+)?(?::(\d+))?/);
  const rel = ctx.activeRel();
  if (!rel) return { rows: [], empty: "Open a file first, then type :42 to go to line 42." };
  if (!m || !m[1]) return { rows: [], empty: "Type a line number, like :42 or :42:9 for a column." };
  return { rows: [{ html: "Go to line <b>" + esc(m[1]) + "</b>" + (m[2] ? ", column <b>" + esc(m[2]) + "</b>" : ""),
    detail: esc(rel), goLine: +m[1], goCol: m[2] ? +m[2] : 1 }] };
}

function fileRows(q, dir) {
  const files = ctx.files().filter((f) => f.role !== "other" || /\.(md|txt|tcl|json|ya?ml|sdc|cfg|hex|mem)$/i.test(f.relpath));
  const top = ctx.topRel();
  const rec = recent(dir);
  const recRank = new Map(rec.map((r, i) => [r, 20 - i]));
  if (!q.trim()) {
    const order = rec.filter((r) => files.some((f) => f.relpath === r));
    if (top && !order.includes(top)) order.push(top);
    for (const f of files) if (!order.includes(f.relpath)) order.push(f.relpath);
    const rows = order.slice(0, 200).map((rel, i) => fileRow(files.find((f) => f.relpath === rel), [], top,
      i === 0 && rec.length ? "RECENTLY OPENED" : i === Math.min(rec.length, order.length) && rec.length ? "FILES" : ""));
    return { rows };
  }
  const ranked = rank(q, files, (f) => f.relpath, { limit: 512, recency: (f) => recRank.get(f.relpath) || 0 });
  if (!ranked.length) {
    return { rows: [], empty: "No file matches “" + esc(q) + "”. Search inside the files instead with <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd>." };
  }
  return { rows: ranked.map((r) => fileRow(r.item, r.nameHits, top, "")) };
}

function fileRow(f, nameHits, top, group) {
  const rel = f.relpath;
  const cut = rel.lastIndexOf("/");
  const isTop = rel === top;
  return {
    html: "<span class='qp-name'>" + markHits(rel.slice(cut + 1), nameHits, esc) + "</span>",
    detail: "<span class='qp-dir'>" + esc(cut >= 0 ? rel.slice(0, cut + 1) : "./") + "</span>",
    tag: isTop ? "TOP" : ROLE_TAG[f.role] || "",
    tagCls: isTop ? "is-on" : "",
    rel,
    group: group || "",
  };
}

function symbolRows(q, syms, project) {
  const cur = ctx.activeRel();
  let list = syms;
  if (!project) list = syms.filter((s) => s.kind !== "macro" || true);
  const ranked = q.trim() ? rank(q, list, (s) => s.name, { limit: 400 }) : list.slice(0, 400).map((s) => ({ item: s, nameHits: [] }));
  if (!ranked.length) return { rows: [], empty: q ? "No symbol matches “" + esc(q) + "”." : (project ? "The project has no symbols yet." : "This file declares nothing LanEx can list.") };
  return {
    rows: ranked.map(({ item: s, nameHits }) => ({
      html: "<span class='qp-name is-mono'>" + markHits(s.name, nameHits, esc) + "</span> <span class='qp-kind'>" + esc(kindWord(s.kind)) + "</span>",
      detail: esc((project || s.rel !== cur ? s.rel + ":" : "line ") + s.line) + (s.unit && s.unit !== s.name ? " · in " + esc(s.unit) : ""),
      tag: project && s.built ? "BUILT" : "",
      rel: s.rel, line: s.line, col: s.col, len: s.name.length,
    })),
  };
}

// ------------------------------------------------------------------ index status

let _statusTimer = 0;
export async function refreshIndexStatus({ refresh = false } = {}) {
  const el = document.getElementById("ide-index-status");
  if (!el || !ctx.designDir()) { if (el) el.hidden = true; return; }
  let st;
  try { st = await api.ideIndex({ refresh }); } catch (_e) { el.hidden = true; return; }
  el.hidden = false;
  const skipped = (st.skipped || []).length;
  el.classList.toggle("is-warn", !!(skipped || st.error || st.truncated));
  el.title = st.error ? "Couldn't read the project folder: " + st.error
    : skipped ? "Not indexed: " + st.skipped.slice(0, 6).map((x) => x.rel + " (" + x.why + ")").join(", ")
    : "F12 goes to a definition, Shift F12 lists the references, Ctrl P opens any file. " +
      st.files + " HDL files, " + st.lines + " lines, " + st.symbols + " names, read in " + st.ms + " ms.";
  el.textContent = st.building && !st.ready ? "Index · reading " + st.progress.done + " of " + st.progress.total + " files…"
    : st.error ? "Index · couldn't read the folder"
    : "Index · " + st.files + (st.files === 1 ? " file" : " files") + (skipped ? " · " + skipped + " skipped" : "") +
      (st.truncated ? " · first 2,000 only" : " · ready");
  clearTimeout(_statusTimer);
  if (st.building) _statusTimer = setTimeout(() => refreshIndexStatus(), 600);
}
