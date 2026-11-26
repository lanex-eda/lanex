// search.js — the Search panel tab: find in every project file (Ctrl+Shift+F)
// and replace with a preview (Ctrl+Shift+H). Match case, whole word and
// regular expression toggles, files to include and to leave out, results
// grouped by file, a tick per match. Replace asks once, writes each file
// through the same hash-checked save as the editor (so a file that changed
// on disk since the search is left alone and named), and Undo puts the old
// texts back. The search itself runs on the server (controller/hdlsearch.py).
//
//   initSearch(ctx)  ctx = { dirty(), dirtyRels(), note(msg, bad), openAt(rel, line, col, len),
//                            setPanel(name), selection(), afterWrite(rels) }
import { api, fmt } from "../api.js";

const esc = (s) => fmt.escape(String(s === undefined || s === null ? "" : s));
const CARET = "<svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M6 9l6 6 6-6'/></svg>";
const OPTS_KEY = "ll.ideSearch";

let ctx = null;
const st = { res: null, off: new Set(), seq: 0, busy: false };

function opts() {
  const q = (id) => document.getElementById(id);
  return {
    query: q("sx-q") ? q("sx-q").value : "",
    replace: q("sx-r") && q("sx-r").value !== "" ? q("sx-r").value : null,
    case: q("sx-case")?.getAttribute("aria-pressed") === "true",
    word: q("sx-word")?.getAttribute("aria-pressed") === "true",
    regex: q("sx-regex")?.getAttribute("aria-pressed") === "true",
    include: q("sx-inc") ? q("sx-inc").value : "",
    exclude: q("sx-exc") ? q("sx-exc").value : "",
  };
}

function remember() {
  const o = opts();
  try {
    localStorage.setItem(OPTS_KEY, JSON.stringify({ case: o.case, word: o.word, regex: o.regex, include: o.include,
      exclude: o.exclude, files: !!document.getElementById("sx-files")?.open }));
  } catch (_e) { /* not kept */ }
}

export function searchPaneHtml() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(OPTS_KEY) || "{}") || {}; } catch (_e) { saved = {}; }
  const tog = (id, label, tip, on) => "<button type='button' class='sx-tog' id='" + id + "' aria-pressed='" + !!on + "' title='" + tip + "' aria-label='" + tip + "'>" + label + "</button>";
  return "<div class='sx-form'>" +
    "<div class='sx-row'><label class='sx-field'><span class='sr-only'>Search the project</span>" +
      "<input id='sx-q' class='inp sx-input' type='text' spellcheck='false' autocomplete='off' placeholder='Search every file in the project'/></label>" +
      "<span class='sx-togs'>" + tog("sx-case", "Aa", "Match case", saved.case) + tog("sx-word", "ab|", "Whole word", saved.word) +
      tog("sx-regex", ".*", "Regular expression ($1 in the replacement is the first group)", saved.regex) + "</span>" +
      "<span class='sx-meta' id='sx-meta' aria-live='polite'></span></div>" +
    "<div class='sx-row'><label class='sx-field'><span class='sr-only'>Replace with</span>" +
      "<input id='sx-r' class='inp sx-input' type='text' spellcheck='false' autocomplete='off' placeholder='Replace with (each match shows old → new; nothing changes until you press Replace)'/></label>" +
      "<button class='btn btn-sm' id='sx-apply' disabled>Replace</button></div>" +
    "<details class='sx-files' id='sx-files'" + (saved.files ? " open" : "") + "><summary>Files to search</summary>" +
      "<div class='sx-row'><label class='ide-field sx-glob'>Files to include <input id='sx-inc' class='inp' type='text' spellcheck='false' placeholder='src/**, *.sv' value='" + esc(saved.include || "") + "'/></label>" +
      "<label class='ide-field sx-glob'>Files to leave out <input id='sx-exc' class='inp' type='text' spellcheck='false' placeholder='verify, *_old.v' value='" + esc(saved.exclude || "") + "'/></label></div>" +
      "<p class='sx-note'>Comma-separated. <code>src/**</code> is everything under src, <code>*.sv</code> any SystemVerilog file, a bare name a folder or a file. " +
      "runs/, sim_build/ and hidden folders are never searched.</p></details>" +
    "</div><div class='pb-list sx-list' id='sx-list'></div>";
}

export function initSearch(c) {
  ctx = c;
  const pane = document.getElementById("ide-search");
  if (!pane || pane._wired) return { focusSearch };
  pane._wired = true;
  const q = (id) => document.getElementById(id);
  let t = 0;
  const soon = () => { clearTimeout(t); t = setTimeout(run, 250); };
  ["sx-q", "sx-r", "sx-inc", "sx-exc"].forEach((id) => q(id)?.addEventListener("input", () => { if (id !== "sx-q" && id !== "sx-r") remember(); soon(); }));
  ["sx-case", "sx-word", "sx-regex"].forEach((id) => q(id)?.addEventListener("click", (e) => {
    const b = e.currentTarget;
    b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true");
    remember();
    run();
  }));
  q("sx-files")?.addEventListener("toggle", remember);
  q("sx-q")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  q("sx-r")?.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); applyReplace(); } });
  q("sx-apply")?.addEventListener("click", applyReplace);
  const list = q("sx-list");
  list.addEventListener("click", (e) => {
    const head = e.target.closest(".pb-fhead");
    if (head && !e.target.closest("input")) { head.setAttribute("aria-expanded", head.getAttribute("aria-expanded") === "false" ? "true" : "false"); return; }
    const gt = e.target.closest("input.sx-gtick");
    if (gt) { toggleGroup(gt.dataset.rel, gt.checked); return; }
    const tk = e.target.closest("input.sx-tick");
    if (tk) { if (tk.checked) st.off.delete(tk.dataset.k); else st.off.add(tk.dataset.k); paintCounts(); return; }
    const it = e.target.closest(".sx-item");
    if (it) ctx.openAt(it.dataset.rel, +it.dataset.line, +it.dataset.col, +it.dataset.len);
  });
  list.addEventListener("keydown", (e) => {
    const it = e.target.closest(".sx-item");
    if (!it) return;
    if (e.key === "Enter") { e.preventDefault(); ctx.openAt(it.dataset.rel, +it.dataset.line, +it.dataset.col, +it.dataset.len); }
    else if (e.key === " ") { const tk = it.querySelector("input.sx-tick"); if (tk && !tk.disabled) { e.preventDefault(); tk.click(); } }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const all = [...list.querySelectorAll(".sx-item")].filter((x) => x.getClientRects().length);
      const k = all.indexOf(it) + (e.key === "ArrowDown" ? 1 : -1);
      if (all[k]) all[k].focus();
    }
  });
  paintEmpty();
  return { focusSearch };
}

// Ctrl+Shift+F / Ctrl+Shift+H: the field, filled with the editor's selection.
export function focusSearch({ replace = false } = {}) {
  ctx.setPanel("search");
  const q = document.getElementById("sx-q");
  const sel = ctx.selection();
  if (q && sel && !sel.includes("\n") && sel.length < 200) { q.value = sel; run(); }
  const f = replace ? document.getElementById("sx-r") : q;
  if (f) { f.focus(); f.select(); }
}

function paintEmpty(msg) {
  const list = document.getElementById("sx-list");
  if (list) list.innerHTML = "<p class='pb-clean'>" + (msg || "Type to search every Verilog, VHDL, constraint and config file in the project. " +
    "<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd> opens this from anywhere in the IDE.") + "</p>";
  const meta = document.getElementById("sx-meta");
  if (meta && !msg) meta.textContent = "";
  paintCounts();
}

export async function run() {
  const o = opts();
  const meta = document.getElementById("sx-meta");
  if (!o.query) { st.res = null; st.off.clear(); paintEmpty(); return; }
  const seq = ++st.seq;
  if (meta) meta.textContent = "Searching…";
  let r;
  try {
    r = await api.ideSearch({ ...o, dirty: ctx.dirty() });
  } catch (ex) {
    if (seq !== st.seq) return;
    paintEmpty("<b>The search didn't run.</b> " + esc(ex.message || ex));
    return;
  }
  if (seq !== st.seq) return;
  if (r.ok === false) { st.res = null; paintEmpty("<b>" + esc(r.error) + "</b>"); if (meta) meta.textContent = ""; return; }
  st.res = r;
  st.off.clear();
  render();
}

function key(rel, off) { return rel + "@" + off; }

function render() {
  const r = st.res;
  const list = document.getElementById("sx-list");
  const meta = document.getElementById("sx-meta");
  if (!list || !r) return;
  const o = opts();
  const dirty = new Set(ctx.dirtyRels());
  if (meta) {
    meta.textContent = (r.total ? r.total + (r.capped ? "+" : "") + (r.total === 1 ? " match" : " matches") + " in " + r.files + (r.files === 1 ? " file" : " files")
      : "No match") + " · " + r.searched + " searched · " + (r.ms < 1000 ? Math.round(r.ms) + " ms" : (r.ms / 1000).toFixed(1) + " s");
  }
  if (!r.groups.length) {
    const skipped = (r.skipped || []).length;
    list.innerHTML = "<p class='pb-clean'>No match in " + r.searched + " files. Searched everything except runs/, sim_build/ and hidden folders" +
      (skipped ? ", and " + skipped + " files LanEx doesn't search (" + esc(r.skipped.slice(0, 3).map((x) => x.rel + ": " + x.why).join("; ")) + ")" : "") + ".</p>";
    paintCounts();
    return;
  }
  const replacing = o.replace !== null;
  const rows = r.groups.map((g) => {
    const cut = g.rel.lastIndexOf("/");
    const isDirty = replacing && dirty.has(g.rel);
    const open = g.matches.length < 10 || r.groups.length === 1;
    return "<div class='pb-group sx-group" + (isDirty ? " is-dirty" : "") + "'><div class='pb-fhead sx-fhead' role='button' tabindex='0' aria-expanded='" + open + "'>" + CARET +
      (replacing ? "<input type='checkbox' class='sx-gtick' data-rel='" + esc(g.rel) + "'" + (isDirty ? " disabled" : " checked") +
        " aria-label='Replace in " + esc(g.rel) + "'/>" : "") +
      "<span class='pb-fname'><span class='pb-dir'>" + esc(g.rel.slice(0, cut + 1)) + "</span>" + esc(g.rel.slice(cut + 1)) + "</span>" +
      (isDirty ? "<span class='rf-chip is-warn' title='This file has edits in the editor that are not saved. Save it, then search again to replace in it.'>UNSAVED EDITS · SAVE FIRST</span>" : "") +
      "<span class='pb-fcount'>" + g.matches.length + "</span></div><div class='pb-items'>" +
      g.matches.map((m) => {
        const k = key(g.rel, m.off);
        const a = m.at, b = m.at + m.len;
        const textHtml = esc(m.text.slice(0, a)) + "<mark" + (replacing ? " class='is-old'" : "") + ">" + esc(m.text.slice(a, b)) + "</mark>" +
          (replacing ? "<ins>" + esc(m.new === undefined ? "" : m.new) + "</ins>" : "") + esc(m.text.slice(b));
        return "<div class='sx-item' role='button' tabindex='0' data-rel='" + esc(g.rel) + "' data-line='" + m.line + "' data-col='" + m.col + "' data-len='" + m.len + "'>" +
          (replacing ? "<input type='checkbox' class='sx-tick' data-k='" + esc(k) + "'" + (isDirty ? " disabled" : st.off.has(k) ? "" : " checked") +
            " aria-label='Replace this match on line " + m.line + "'/>" : "") +
          "<span class='rf-ln'>" + m.line + "</span><span class='rf-text'>" + textHtml + "</span></div>";
      }).join("") + "</div></div>";
  }).join("");
  list.innerHTML = rows + (r.capped ? "<p class='pb-clean'>Showing the first " + r.total + " matches. Narrow the search (a folder in Files to include, or Match case) to see the rest.</p>" : "");
  paintCounts();
}

function toggleGroup(rel, on) {
  const g = st.res && st.res.groups.find((x) => x.rel === rel);
  if (!g) return;
  for (const m of g.matches) { const k = key(rel, m.off); if (on) st.off.delete(k); else st.off.add(k); }
  document.querySelectorAll("#sx-list input.sx-tick").forEach((tk) => {
    if (tk.dataset.k.startsWith(rel + "@")) tk.checked = on;
  });
  paintCounts();
}

function ticked() {
  const r = st.res;
  if (!r) return [];
  const dirty = new Set(ctx.dirtyRels());
  return r.groups.filter((g) => !dirty.has(g.rel)).map((g) => ({
    rel: g.rel, hash: g.hash, offs: g.matches.filter((m) => !st.off.has(key(g.rel, m.off))).map((m) => m.off),
  })).filter((e) => e.offs.length);
}

function paintCounts() {
  const b = document.getElementById("sx-apply");
  if (!b) return;
  const o = opts();
  const edits = o.replace !== null ? ticked() : [];
  const n = edits.reduce((a, e) => a + e.offs.length, 0);
  b.disabled = !n || st.busy;
  b.textContent = n ? "Replace " + n + " in " + edits.length + (edits.length === 1 ? " file" : " files") : "Replace";
  document.querySelectorAll("#sx-list input.sx-gtick").forEach((gt) => {
    const all = [...document.querySelectorAll("#sx-list input.sx-tick")].filter((tk) => tk.dataset.k.startsWith(gt.dataset.rel + "@"));
    const on = all.filter((tk) => tk.checked).length;
    gt.checked = on > 0;
    gt.indeterminate = on > 0 && on < all.length;
  });
}

async function applyReplace() {
  const o = opts();
  const edits = ticked();
  if (o.replace === null || !edits.length || st.busy) return;
  const n = edits.reduce((a, e) => a + e.offs.length, 0);
  const { confirmDialog } = await import("../dialog.js");
  const ok = await confirmDialog({
    title: "Replace in " + edits.length + (edits.length === 1 ? " file?" : " files?"),
    body: n + (n === 1 ? " match" : " matches") + " of “" + o.query + "” become “" + o.replace + "”. Files open in the editor reload. You can undo this right after.",
    confirmText: "Replace", danger: true,
  });
  if (!ok) return;
  st.busy = true;
  paintCounts();
  let r;
  try {
    r = await api.ideReplace({ query: o.query, regex: o.regex, case: o.case, word: o.word, replace: o.replace, edits });
  } catch (ex) {
    st.busy = false;
    ctx.note("Nothing was replaced: " + (ex.message || ex), true);
    paintCounts();
    return;
  }
  st.busy = false;
  const done = (r.results || []).filter((x) => x.ok);
  const skipped = (r.results || []).filter((x) => !x.ok);
  const names = (list) => list.map((x) => x.rel.split("/").pop()).join(", ");
  const msg = (done.length ? "Replaced " + r.count + (r.count === 1 ? " match" : " matches") + " in " + names(done) + "." : "Nothing was replaced.") +
    (skipped.length ? " " + skipped.map((x) => x.rel.split("/").pop() + " wasn't changed: it " + x.why + ".").join(" ") +
      (skipped.some((x) => x.changed) ? " Search again to include it." : "") : "");
  if (done.length) await ctx.afterWrite(done.map((x) => x.rel));
  ctx.note(msg, !!skipped.length);
  if (done.length) {
    const { toast } = await import("../toast.js");
    toast.show(msg, skipped.length ? "warn" : "info", 12000, { label: "Undo", run: () => undo(done) });
  }
  run();
}

async function undo(done) {
  const back = [], failed = [];
  for (const x of done) {
    try {
      const w = await api.fileWrite(x.rel, x.before, { base_hash: x.hash });
      if (w && w.ok === false) throw new Error(w.error || "refused");
      back.push(x.rel);
    } catch (ex) {
      failed.push(x.rel.split("/").pop() + (ex && ex.status === 409 ? " (changed since)" : ""));
    }
  }
  await ctx.afterWrite(back);
  ctx.note((back.length ? "Undid the replace in " + back.map((r) => r.split("/").pop()).join(", ") + "." : "") +
    (failed.length ? " Couldn't undo " + failed.join(", ") + ": it changed after the replace, so it was left as it is." : ""), !!failed.length);
  run();
}
