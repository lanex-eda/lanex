// warnings.js — Verify's warnings: what the tools said while building the run,
// grouped by tool and code, with LanEx's plain-words reading, "new since the
// baseline", and per-project waivers.
//
// The triage pattern of ORFS's report (code ×count per source), Sentry (one
// issue per fingerprint, first seen), SonarQube (new code first) and GitHub
// code scanning (dismiss with a reason that stays on the record). A waiver
// never hides anything: a waived group stays on screen, greyed, with its
// reason, its note and who waived it; a waiver that matches nothing is listed.
// Waivers live in lanex-waivers.yaml at the project root (controller/
// runwarnings.py), a file meant to be reviewed like code.

import { api, fmt } from "./api.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));

const WORRY = {
  look: { mark: "warn", label: "Look", tip: "LanEx's reading: this usually points at a real problem." },
  info: { mark: "info", label: "Info", tip: "LanEx's reading: worth knowing; it matters only in some designs." },
  harmless: { mark: "harmless", label: "Harmless", tip: "LanEx's reading: expected on the standard flow; it doesn't change the result." },
};

const W = { tag: "", base: "", data: null, mode: null, q: "", showWaived: true, open: new Set(), token: 0, host: null };

// Pure: the groups a filter shows (tests).
export function filterGroups(groups, { mode = "all", q = "", showWaived = true } = {}) {
  const qq = (q || "").trim().toLowerCase();
  return (groups || []).filter((g) => {
    const waived = g.waived_count >= g.count;
    if (waived && !showWaived) return false;
    if (mode === "new" && !(g.new || (g.base_count != null && g.base_count !== g.count && g.base_count > 0))) return false;
    if (qq) {
      const hay = [g.code, g.tool, g.title, g.plain, ...(g.samples || [])].join(" ").toLowerCase();
      if (!hay.includes(qq)) return false;
    }
    return true;
  });
}

function stepsText(steps) {
  const ns = (steps || []).map((s) => s.n).filter((n) => n != null);
  if (!ns.length) return "";
  if (ns.length === 1) return "step " + ns[0];
  return "steps " + ns[0] + "–" + ns[ns.length - 1];
}

export async function renderWarnings(host, { tag, base }) {
  if (!host) return null;
  W.host = host;
  if (tag !== W.tag) { W.open.clear(); W.mode = null; W.q = ""; }
  W.tag = tag; W.base = base || "";
  wire(host);
  if (!host.innerHTML) host.innerHTML = headHtml(null) + "<div class='wn-body'><p class='muted'>Reading the run's warnings…</p></div>";
  const my = ++W.token;
  let d;
  try { d = await api.warnings(tag, W.base); }
  catch (ex) {
    if (my !== W.token) return null;
    host.innerHTML = headHtml(null) + "<div class='tx-empty is-fail'>" + icon("alert", { size: 18 }) + "<div><b>Couldn't read this run's warnings.</b><p>" +
      esc(ex.message || ex) + "</p><p>The run's warning.log may be missing or unreadable. Refresh to try again.</p></div></div>";
    return null;
  }
  if (my !== W.token) return null;
  W.data = d;
  if (W.mode === null) W.mode = d.base_tag && d.new_count ? "new" : "all";
  if (W.mode === "new" && !d.base_tag) W.mode = "all";
  paint();
  return d;
}

function headHtml(d) {
  return "<header class='an-head'><div><div class='an-eyebrow'>Warnings</div><h2 class='an-title'>What the tools said while building this run</h2>" +
    "<p class='an-lede'>Every warning LibreLane, OpenROAD, OpenSTA and Verilator printed, grouped by code, with LanEx's reading of how much each usually matters." +
    " Waive one you have checked: it stays listed, marked waived, with your reason.</p></div>" +
    (d ? "<div class='an-head-act'>" + toolsHtml(d) + "</div>" : "") + "</header>";
}

function toolsHtml(d) {
  const seg = d.base_tag
    ? "<div class='seg' role='group' aria-label='Which warnings'>" +
      "<button class='seg-btn" + (W.mode === "new" ? " is-active" : "") + "' data-wn-mode='new' aria-pressed='" + (W.mode === "new") + "'>New since " + esc(d.base_tag) +
        " <span class='vf-seg-n'>" + filterGroups(d.groups, { mode: "new" }).length + "</span></button>" +
      "<button class='seg-btn" + (W.mode === "all" ? " is-active" : "") + "' data-wn-mode='all' aria-pressed='" + (W.mode === "all") + "'>All <span class='vf-seg-n'>" + d.group_count + "</span></button></div>"
    : "";
  const waived = d.waived_groups ? "<label class='wn-check'><input type='checkbox' data-wn-waived" + (W.showWaived ? " checked" : "") + "/> Show waived <span class='vf-seg-n'>" + d.waived_groups + "</span></label>" : "";
  return seg + "<label class='tx-find'>" + icon("search", { size: 13 }) + "<input type='search' class='inp' id='wn-q' placeholder='Find a code or word…' aria-label='Find a warning' value='" + esc(W.q) + "'/></label>" + waived;
}

function summary(d) {
  const parts = [d.total + " message" + (d.total === 1 ? "" : "s") + " in " + d.group_count + " group" + (d.group_count === 1 ? "" : "s")];
  if (d.base_tag) parts.push((d.new_count || 0) + " new since " + d.base_tag);
  else parts.push("no baseline to compare with");
  if (d.waived_groups) parts.push(d.waived_groups + " group" + (d.waived_groups === 1 ? "" : "s") + " waived");
  if (d.look) parts.push("<b class='wn-look-n'>" + d.look + " need" + (d.look === 1 ? "s" : "") + " a look</b>");
  return parts.map((p, i) => i === parts.length - 1 && /<b/.test(p) ? p : esc(p)).join(" · ");
}

function paint() {
  const host = W.host, d = W.data;
  if (!host || !d) return;
  const shown = filterGroups(d.groups, { mode: W.mode, q: W.q, showWaived: W.showWaived });
  const wv = d.waivers || {};
  const err = wv.error ? "<p class='vf-callout is-warn'>" + icon("alert", { size: 13 }) + " " + esc(wv.error) + "</p>" : "";
  const empty = !d.total
    ? "<div class='tx-empty'>" + icon("check", { size: 18 }) + "<div><b>No warnings.</b><p>" + esc(d.has_log ? "The run's warning.log is empty, and Verilator and OpenSTA reported nothing extra." : "This run has no warning.log (it may have stopped before LibreLane wrote one).") + "</p></div></div>"
    : !shown.length ? "<div class='an-none'>" + (W.mode === "new" ? "Nothing new since " + esc(d.base_tag) + ". Every warning here was in the baseline too." : "No warning matches.") + "</div>" : "";
  const gone = d.gone && d.gone.length
    ? "<details class='wn-more'><summary>Gone since " + esc(d.base_tag) + ": " + d.gone.length + "</summary><ul class='wn-gone'>" +
      d.gone.map((g) => "<li><code>" + esc(g.code) + "</code> " + esc(g.title || g.tool) + " <span class='muted'>×" + g.count + " there</span></li>").join("") + "</ul></details>" : "";
  const unused = (wv.unused || []).length
    ? "<details class='wn-more'><summary>Waivers that matched nothing in this run: " + wv.unused.length + "</summary><ul class='wn-gone'>" +
      wv.unused.map((w) => "<li><code>" + esc(w.code) + "</code>" + (w.match ? " like <code>" + esc(w.match) + "</code>" : "") + " · " + esc(w.note) +
        " <button class='btn btn-ghost btn-xs' data-wn-unwaive='" + esc(w.id) + "'>Remove</button></li>").join("") + "</ul></details>" : "";
  host.innerHTML = headHtml(d) +
    "<p class='wn-sum'>" + summary(d) + "</p>" + err +
    (empty || "<div class='wn-list'>" + shown.map(groupHtml).join("") + "</div>") + gone + unused +
    "<p class='an-foot'>Waivers are saved in <code>" + esc(wv.file || "lanex-waivers.yaml") + "</code> in your project" + (wv.exists ? " (" + (wv.entries || []).length + " so far)" : "") +
    ". They change only how LanEx lists warnings, never the run, its checks or LibreLane's verdict. \"Look\", \"Info\" and \"Harmless\" are LanEx's reading; the tools themselves don't rank warnings.</p>";
}

function groupHtml(g) {
  const open = W.open.has(g.key);
  const waived = g.waived_count >= g.count;
  const partly = g.waived_count > 0 && !waived;
  const wz = WORRY[g.worry] || null;
  const changed = !g.new && g.base_count != null && g.base_count > 0 && g.base_count !== g.count;
  const tags = [
    g.new ? "<span class='wn-tag is-new'>New</span>" : "",
    changed ? "<span class='wn-tag is-chg' title='" + esc("Count in " + W.data.base_tag) + "'>" + g.base_count + " → " + g.count + "</span>" : "",
    waived ? "<span class='wn-tag is-waived'>Waived</span>" : partly ? "<span class='wn-tag is-waived'>" + g.waived_count + " waived</span>" : "",
    g.recheck ? "<span class='wn-tag is-chg' title='The count changed since it was waived'>check waiver</span>" : "",
  ].join("");
  const title = g.title || (g.samples && g.samples[0]) || "";
  return "<div class='wn-row" + (wz ? " is-" + g.worry : "") + (waived ? " is-waived" : "") + (open ? " is-open" : "") + "' id='wn-" + esc(g.key.replace(/[^A-Za-z0-9_-]/g, "_")) + "'>" +
    "<button class='wn-main' data-wn-toggle='" + esc(g.key) + "' aria-expanded='" + open + "'>" +
      "<span class='wn-mark is-" + esc(wz ? wz.mark : "none") + "' title='" + esc(wz ? wz.tip : "LanEx has no note for this code yet.") + "'></span>" +
      "<span class='wn-code'><code>" + esc(g.code) + "</code><small>" + esc(g.tool + (g.part ? " · " + g.part : "")) + "</small></span>" +
      "<span class='wn-title'>" + esc(title) + "</span>" +
      "<span class='wn-tags'>" + tags + "</span>" +
      "<span class='wn-n'>" + g.count + "×</span>" +
      "<span class='wn-steps'>" + esc(stepsText(g.steps)) + "</span>" +
      "<span class='vf-chev' aria-hidden='true'>" + icon("chevron", { size: 13 }) + "</span>" +
    "</button>" + (open ? "<div class='wn-detail'>" + detailHtml(g) + "</div>" : "") + "</div>";
}

function detailHtml(g) {
  const f = g.first || {};
  const wz = WORRY[g.worry];
  const explain = g.plain
    ? "<p><span class='vf-k'>What it means" + (wz ? " · LanEx: " + esc(wz.label.toLowerCase()) : "") + "</span>" + esc(g.plain) + "</p>" +
      (g.matters ? "<p><span class='vf-k'>When it matters</span>" + esc(g.matters) + "</p>" : "")
    : "<p class='muted'>LanEx has no note for this code yet. The tool's own message is below.</p>";
  const first = "<div class='wn-first'><span class='vf-k'>First seen" + (f.step_dir ? " · step " + esc(String(f.step_dir).split("-")[0]) + " · " + esc(f.step_id || f.step_dir) : "") + "</span>" +
    "<pre class='wn-msg'>" + esc([f.msg, ...(f.more || [])].join("\n")) + "</pre></div>";
  const vs = (g.variants || []);
  const variants = vs.length > 1
    ? "<div class='wn-vars'><span class='vf-k'>" + g.variants_total + " variants</span><ul>" + vs.slice(0, 8).map((v) =>
      "<li><span>" + esc(v.text) + "</span><b>" + v.count + "×</b></li>").join("") + "</ul>" + (g.variants_total > 8 ? "<p class='muted'>and " + (g.variants_total - 8) + " more in the log.</p>" : "") + "</div>"
    : "";
  const wv = (g.waivers || []).map((w) =>
    "<div class='wn-waiver'><span class='vf-k'>Waived · " + esc((W.data.waivers.reasons || {})[w.reason] || w.reason) + "</span>" +
    "<p>“" + esc(w.note) + "”</p><small>" + esc([w.by || "unnamed", w.date, w.match ? "messages like " + w.match : "every " + w.code + " message", w.step ? "from " + w.step : ""].filter(Boolean).join(" · ")) + "</small>" +
    (g.recheck && g.recheck.id === w.id ? "<p class='vf-callout is-warn'>Waived, but the count changed (" + g.recheck.was + " → " + g.recheck.now + "). Check it still applies.</p>" : "") +
    "<div class='vf-acts'><button class='btn btn-ghost btn-xs' data-wn-edit='" + esc(g.key) + "' data-wn-wid='" + esc(w.id) + "'>Edit waiver</button>" +
    (g.recheck && g.recheck.id === w.id ? "<button class='btn btn-ghost btn-xs' data-wn-keep='" + esc(g.key) + "' data-wn-wid='" + esc(w.id) + "'>Keep waiver</button>" : "") +
    "<button class='btn btn-ghost btn-xs' data-wn-unwaive='" + esc(w.id) + "'>Remove waiver</button></div></div>").join("");
  const acts = "<div class='vf-acts'>" +
    (f.step_id || f.log ? "<button class='btn btn-ghost btn-sm' data-wn-log='" + esc(g.key) + "'>" + icon("file", { size: 13 }) + " Open in the step log</button>" : "") +
    (g.waived_count < g.count ? "<button class='btn btn-ghost btn-sm' data-wn-waive='" + esc(g.key) + "'>" + icon("check", { size: 13 }) + " Waive…</button>" : "") +
    (g.doc ? "<a class='btn btn-ghost btn-sm' href='" + esc(g.doc) + "' target='_blank' rel='noopener'>" + icon("popout", { size: 13 }) + " Verilator's page for " + esc(g.code) + "</a>" : "") +
    "</div>";
  return "<div class='wn-dl'>" + explain + first + variants + "</div><div class='wn-dr'>" + wv + acts + "</div>";
}

function groupByKey(key) { return ((W.data && W.data.groups) || []).find((g) => g.key === key); }

function wire(host) {
  if (host._wnWired) return;
  host._wnWired = true;
  host.addEventListener("click", (e) => {
    const t = e.target;
    const tg = t.closest("[data-wn-toggle]");
    if (tg) {
      const k = tg.dataset.wnToggle;
      if (W.open.has(k)) W.open.delete(k); else W.open.add(k);
      paint();
      host.querySelector("[data-wn-toggle='" + CSS.escape(k) + "']")?.focus();
      return;
    }
    const m = t.closest("[data-wn-mode]");
    if (m) { W.mode = m.dataset.wnMode; paint(); return; }
    const wv = t.closest("[data-wn-waive]");
    if (wv) { waiverDialog(groupByKey(wv.dataset.wnWaive)); return; }
    const ed = t.closest("[data-wn-edit]");
    if (ed) { const g = groupByKey(ed.dataset.wnEdit); waiverDialog(g, (g.waivers || []).find((w) => w.id === ed.dataset.wnWid)); return; }
    const kp = t.closest("[data-wn-keep]");
    if (kp) { const g = groupByKey(kp.dataset.wnKeep); keepWaiver(g, (g.waivers || []).find((w) => w.id === kp.dataset.wnWid)); return; }
    const un = t.closest("[data-wn-unwaive]");
    if (un) { unwaive(un.dataset.wnUnwaive); return; }
    const lg = t.closest("[data-wn-log]");
    if (lg) { openLog(groupByKey(lg.dataset.wnLog)); }
  });
  host.addEventListener("change", (e) => {
    if (e.target.matches("[data-wn-waived]")) { W.showWaived = e.target.checked; paint(); }
  });
  host.addEventListener("input", (e) => {
    if (e.target.id !== "wn-q") return;
    clearTimeout(host._wnQ);
    host._wnQ = setTimeout(() => {
      W.q = e.target.value;
      const pos = e.target.selectionStart;
      paint();
      const q = document.getElementById("wn-q");
      if (q) { q.focus(); try { q.setSelectionRange(pos, pos); } catch (_e) {} }
    }, 200);
  });
}

async function reload() {
  const d = await renderWarnings(W.host, { tag: W.tag, base: W.base });
  return d;
}

// The step log, at the first line with the code.
async function openLog(g) {
  if (!g) return;
  const f = g.first || {};
  const { customDialog } = await import("./dialog.js");
  const { renderFileText } = await import("./fileview.js");
  const { needleLine } = await import("./provenance.js");
  const tag = W.tag;
  const needle = /^[A-Z]{2,5}-\d{4}$/.test(g.code) ? g.code : (f.msg || "").slice(0, 60);
  customDialog({
    title: (f.step_id || "Step") + " · log", wide: true,
    bodyHtml: "<div class='vf-viewer'><p class='muted'>Loading the log…</p></div>",
    onMount: async (dlg) => {
      const host = dlg.querySelector(".vf-viewer");
      try {
        let text, rel;
        if (f.log) {
          rel = f.log;
          const resp = await fetch(api.runFileUrl(tag, rel), { headers: { "X-Requested-With": "XMLHttpRequest" } });
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          text = await resp.text();
        } else {
          const r = await api.runStepLog(f.step_id, tag);
          if (!r || !r.ok) throw new Error((r && r.reason) || "This step wrote no log.");
          rel = r.dir && r.log_file ? r.dir + "/" + r.log_file : null;
          text = r.log || "";
        }
        renderFileText(host, text, { tag, path: rel, title: rel || f.step_id, line: needleLine(text, needle) });
      } catch (ex) {
        host.innerHTML = "<p class='muted'>" + esc(ex.message || ex) + "</p>";
      }
    },
  });
}

// "Waive …" for this project: which messages, why, a note for a reviewer.
export async function waiverDialog(g, existing = null) {
  if (!g) return;
  const d = W.data;
  const reasons = (d && d.waivers && d.waivers.reasons) || { harmless: "Harmless for this design" };
  let who = "";
  try { who = localStorage.getItem("ll.waiverBy") || ""; } catch (_e) {}
  const sample = (g.first && g.first.msg) || "";
  // A starting pattern: the first message with every path and every word that
  // holds a number (names, counts, corners) as a wildcard.
  const like = existing && existing.match ? existing.match : sample.replace(/\S*\/\S*/g, "*").replace(/\S*\d\S*/g, "*").replace(/(\* )+\*/g, "*");
  const step = (g.steps && g.steps.length === 1 && g.steps[0].id) || "";
  const scope = existing ? (existing.step ? "step" : existing.match ? "like" : "all") : "all";
  const body =
    "<p class='wn-dlg-lead'><code>" + esc(g.code) + "</code> · " + esc(g.title || sample) + " · <b>" + g.count + "×</b> in this run</p>" +
    "<h4>Which messages</h4>" +
    "<label class='wn-radio'><input type='radio' name='wn-scope' value='all'" + (scope === "all" ? " checked" : "") + "/> Every " + esc(g.code) + " message</label>" +
    "<label class='wn-radio'><input type='radio' name='wn-scope' value='like'" + (scope === "like" ? " checked" : "") + "/> Only messages like <input class='dlg-input wn-like' id='wn-like' spellcheck='false' value='" + esc(like) + "' aria-label='Message pattern, * matches anything'/></label>" +
    (step ? "<label class='wn-radio'><input type='radio' name='wn-scope' value='step'" + (scope === "step" ? " checked" : "") + "/> Only from step <code>" + esc(step) + "</code></label>" : "") +
    "<h4><label for='wn-reason'>Why it's fine</label></h4><select class='dlg-input' id='wn-reason'>" +
      Object.entries(reasons).map(([k, v]) => "<option value='" + esc(k) + "'" + (existing && existing.reason === k ? " selected" : "") + ">" + esc(v) + "</option>").join("") + "</select>" +
    "<h4><label for='wn-note'>Note for a reviewer</label></h4><textarea class='dlg-input wn-note' id='wn-note' rows='3' placeholder='Why is this fine? Someone reviewing this design will read it.'>" + esc(existing ? existing.note : "") + "</textarea>" +
    "<h4><label for='wn-by'>Waived by</label></h4><input class='dlg-input' id='wn-by' spellcheck='false' placeholder='Your name (optional)' value='" + esc(existing ? existing.by : who) + "'/>" +
    "<label class='wn-radio wn-ask'><input type='checkbox' id='wn-expect'" + (!existing || existing.expect_count != null ? " checked" : "") + "/> Ask again if the number of these messages changes</label>" +
    "<p class='wn-err' id='wn-err' role='alert'></p>" +
    "<p class='muted wn-dlg-foot'>Saved in lanex-waivers.yaml in your project. Waivers change only how LanEx lists warnings: the run, its checks and LibreLane's verdict stay as they are.</p>";
  const { customDialog } = await import("./dialog.js");
  const res = await customDialog({
    title: (existing ? "Edit the waiver for " : "Waive ") + g.code + " for this project", bodyHtml: body,
    buttons: [{ label: "Cancel", value: null, cls: "btn-ghost" }],
    onMount: (back, close) => {
      const acts = back.querySelector(".dlg-actions");
      const go = document.createElement("button");
      go.className = "btn btn-primary";
      go.textContent = existing ? "Save waiver" : "Waive";
      acts.appendChild(go);
      // Typing a pattern picks "Only messages like"; the reason has the focus
      // (the dialog would otherwise focus the pattern field and change nothing).
      back.querySelector("#wn-like")?.addEventListener("input", () => { const r = back.querySelector("input[value='like']"); if (r) r.checked = true; });
      requestAnimationFrame(() => requestAnimationFrame(() => back.querySelector("#wn-reason")?.focus()));
      go.addEventListener("click", async () => {
        const errEl = back.querySelector("#wn-err");
        const sc = (back.querySelector("input[name='wn-scope']:checked") || {}).value || "all";
        const note = back.querySelector("#wn-note").value.trim();
        if (note.length < 10) { errEl.textContent = "Write a note for a reviewer (at least 10 characters): why is this warning fine?"; back.querySelector("#wn-note").focus(); return; }
        const by = back.querySelector("#wn-by").value.trim();
        const entry = {
          id: existing ? existing.id : "", tool: g.tool, code: g.code,
          match: sc === "like" ? back.querySelector("#wn-like").value.trim() : null,
          step: sc === "step" ? step : null,
          reason: back.querySelector("#wn-reason").value, note, by,
          expect_count: back.querySelector("#wn-expect").checked ? "auto" : null,
        };
        if (sc === "like" && !entry.match) { errEl.textContent = "Type the pattern the messages must match (* matches anything), or pick another choice."; return; }
        go.disabled = true;
        try {
          await api.waive(entry, d.waivers.mtime, W.tag);
          try { if (by) localStorage.setItem("ll.waiverBy", by); } catch (_e) {}
          close("saved");
        } catch (ex) {
          go.disabled = false;
          errEl.textContent = ex.message || String(ex);
        }
      });
    },
  });
  if (res !== "saved") return;
  await reload();
  toast.show((existing ? "Waiver for " : "Waived ") + g.code + (existing ? " saved" : "") + ". Saved in lanex-waivers.yaml.", "success");
}

async function keepWaiver(g, w) {
  if (!g || !w) return;
  try {
    await api.waive({ ...w, expect_count: "auto" }, W.data.waivers.mtime, W.tag);
    await reload();
    toast.show("Kept the waiver for " + g.code + " at this run's count.", "success");
  } catch (ex) { toast.show("Couldn't save the waiver: " + (ex.message || ex), "error"); }
}

async function unwaive(id) {
  const d = W.data;
  const w = ((d && d.waivers && d.waivers.entries) || []).find((x) => x.id === id);
  if (!w) return;
  const { confirmDialog } = await import("./dialog.js");
  const ok = await confirmDialog({ title: "Remove the waiver for " + w.code + "?", body: "“" + w.note + "” will be taken out of lanex-waivers.yaml, and the warning listed as not waived again.", confirmText: "Remove waiver", danger: true });
  if (!ok) return;
  try {
    const before = { ...w };
    await api.unwaive(id, d.waivers.mtime);
    const nd = await reload();
    toast.show("Removed the waiver for " + w.code + ".", "success", 8000, {
      label: "Undo", run: async () => {
        try { await api.waive({ ...before, id: "" }, nd && nd.waivers ? nd.waivers.mtime : null); await reload(); }
        catch (ex) { toast.show("Couldn't put the waiver back: " + (ex.message || ex), "error"); }
      },
    });
  } catch (ex) { toast.show("Couldn't remove the waiver: " + (ex.message || ex), "error"); }
}
