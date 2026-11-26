// toolsys.js — "LanEx and this computer" on the Tools page (audit plan 5a, 5c, 5e):
// the LanEx version and its updates, where the disk went, the network LanEx's
// Linux uses, how much memory WSL gives it, and what can be cleaned up.
//
// Reusable pieces (other pages can adopt them):
//   storageBarHtml({...})   one stacked bar: parts by size, free space outlined,
//                           a legend with exact numbers in mono (never colour alone)
//   sysTileHtml({...})      a status tile in the Tools look (tl-tile)
//   cleanupRowsHtml(items)  rows of things to remove, each with its size
//   fmtBytes(n)             1000-based sizes, as Docker and the disk report them
// Every removal asks first, naming what goes, its size and what stays, with
// focus on Cancel (dialog.js danger dialogs).

import { api, fmt } from "./api.js";
import { state, safeStorage } from "./state.js";
import { toast } from "./toast.js";
import { confirmDialog, customDialog } from "./dialog.js";
import { icon } from "./icons.js";
import { prefOn } from "./prefs.js";
import { when, bytes } from "./metricfmt.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));

// ------------------------------------------------------------------ format
// The one size formatter (metricfmt.bytes), "about" for an estimate.
export function fmtBytes(n, { approx = false } = {}) {
  if (n == null || !isFinite(n)) return "";
  return (approx ? "about " : "") + bytes(n);
}

export function whenText(t) {
  if (!t) return "never";
  const ms = typeof t === "number" ? (t < 1e12 ? t * 1000 : t) : Date.parse(t);
  if (!isFinite(ms)) return String(t);
  // The one date shape (metricfmt.when), the same as Runs and the run picker.
  return when(new Date(ms));
}

// ------------------------------------------------------------------ pieces
function stateHtml(st, text) {
  const mark = st === "ready" ? icon("check", { size: 13, stroke: 2 }) : "<i></i>";
  return "<span class='tl-state is-" + st + "'>" + mark + esc(text) + "</span>";
}
function moreHtml(items) {
  const list = (items || []).filter(Boolean);
  if (!list.length) return "";
  return "<details class='tl-more'><summary class='tl-more-btn' title='More actions' aria-label='More actions'>" +
    "<svg viewBox='0 0 24 24' width='16' height='16' fill='currentColor' aria-hidden='true'><circle cx='5' cy='12' r='1.6'/><circle cx='12' cy='12' r='1.6'/><circle cx='19' cy='12' r='1.6'/></svg>" +
    "</summary><div class='tl-menu'>" + list.join("") + "</div></details>";
}
const btn = (label, attrs = "", { primary = false, title = "" } = {}) =>
  "<button class='btn btn-sm tl-act " + (primary ? "btn-primary" : "btn-ghost") + "' " + attrs +
  (title ? " title='" + esc(title) + "'" : "") + ">" + esc(label) + "</button>";
const item = (label, attrs = "", { danger = false } = {}) =>
  "<button class='tl-menu-item" + (danger ? " is-danger" : "") + "' " + attrs + ">" + esc(label) + "</button>";
function kvHtml(rows) {
  return "<dl class='tl-kv'>" + rows.filter(Boolean).map(([k, v, title]) =>
    "<div><dt>" + esc(k) + "</dt><dd" + (title ? " title='" + esc(title) + "'" : "") + ">" + v + "</dd></div>").join("") + "</dl>";
}

/** A status tile in the Tools look. `t`: {id, eyebrow, status, stateText, big,
 *  ver, rows:[[k, html, title]], actions: html, menu: [html], sub: html, name}. */
export function sysTileHtml(t) {
  const st = t.status || "off";
  return (
    "<div class='tl-tile tl-sys-tile is-" + st + "' id='" + esc(t.id) + "' data-status='" + st + "' data-name='" + esc(t.name || t.eyebrow) + "'>" +
      "<div class='tl-tile-top'><span class='tl-eyebrow'>" + esc(t.eyebrow) + "</span>" + stateHtml(st, t.stateText) + moreHtml(t.menu) + "</div>" +
      "<div class='tl-tile-big'>" + esc(t.big) + (t.ver ? " <span class='tl-tile-ver'>" + esc(t.ver) + "</span>" : "") + "</div>" +
      kvHtml(t.rows || []) +
      (t.sub ? "<div class='tl-sys-sub'>" + t.sub + "</div>" : "") +
      "<div class='tl-tile-act'>" + (t.actions || "") + "</div>" +
    "</div>"
  );
}

/** One stacked bar. `s`: {drive, total, free, parts:[{key,label,bytes,title}],
 *  measuring, note, windows}. Free under 12 GB reads amber, under 5 GB red. */
export function storageBarHtml(s) {
  const total = s.total || 0;
  const parts = (s.parts || []).filter((p) => p.bytes > 0);
  const used = parts.reduce((a, p) => a + p.bytes, 0);
  const freeGb = (s.free || 0) / 1e9;
  const level = s.free == null ? "" : freeGb < 5 ? " is-critical" : freeGb < 12 ? " is-low" : "";
  const pct = (b) => total ? Math.max(0.4, (b / total) * 100).toFixed(2) + "%" : "0%";
  const segs = parts.map((p) =>
    "<span class='tl-store-seg is-" + esc(p.key) + "' style='width:" + pct(p.bytes) + "' title='" + esc(p.label + " " + fmtBytes(p.bytes) + (p.title ? ": " + p.title : "")) + "'></span>").join("");
  const legend = parts.map((p) =>
    "<button type='button' class='tl-store-key is-" + esc(p.key) + "' data-store='" + esc(p.key) + "' title='" + esc(p.title || p.label) + "'><i></i>" + esc(p.label) + " <b>" + esc(fmtBytes(p.bytes)) + "</b></button>").join("") +
    (s.measuring ? "<span class='tl-store-key is-measuring'><span class='spinner-sm'></span> measuring…</span>" : "") +
    "<span class='tl-store-key is-free'><i></i>Free <b>" + esc(fmtBytes(s.free)) + "</b></span>";
  const aria = (s.drive || "Disk") + ": " + fmtBytes(s.free) + " free of " + fmtBytes(total) +
    (used ? "; LanEx uses " + fmtBytes(used) : "");
  return (
    "<div class='tl-store" + level + "' id='tl-sys-storage' data-status='" + (level.includes("critical") ? "broken" : level ? "update" : "ready") + "' data-name='Disk space'>" +
      "<div class='tl-store-h'><span class='tl-eyebrow'>Storage</span>" +
        "<span class='tl-store-sum'><b class='tl-store-free-v'>" + esc(fmtBytes(s.free)) + "</b> free on " + esc(s.drive || "this disk") +
        (total ? " <span class='muted'>of " + esc(fmtBytes(total)) + "</span>" : "") +
        (used ? "<span class='sep'>·</span>LanEx uses <b>" + esc(fmtBytes(used)) + "</b>" : "") + "</span></div>" +
      "<div class='tl-store-bar' role='img' aria-label='" + esc(aria) + "'>" + segs + "<span class='tl-store-free'></span></div>" +
      "<div class='tl-store-legend'>" + legend + "</div>" +
      (level ? "<p class='tl-store-warn'>" + icon("alert", { size: 13 }) + " " +
        (level.includes("critical") ? "Runs and downloads stop when the disk is full." : "Updating the tool image needs about 10 GB.") +
        " Clean up below, or free space on " + esc(s.drive || "the disk") + ".</p>" : "") +
      (s.note ? "<p class='tl-store-note'>" + esc(s.note) + "</p>" : "") +
    "</div>"
  );
}

/** Rows of things that can be removed. `items`: cleanup_items() rows. */
export function cleanupRowsHtml(items) {
  return items.map((x, i) => {
    const detail = x.kind === "image"
      ? (x.ref || "") + (x.in_use ? " · a container still uses it" : "")
      : (x.variants || []).join(", ") + (x.installed_at ? " · installed " + whenText(x.installed_at) : "");
    return "<div class='tl-clean-row' data-i='" + i + "'>" +
      "<div class='tl-clean-what'><div class='tl-clean-name'>" + esc(x.name) + "</div>" +
        "<div class='tl-clean-detail' title='" + esc(detail) + "'>" + esc(detail) + "</div></div>" +
      "<div class='tl-clean-size'>" + (x.bytes ? esc(fmtBytes(x.bytes)) + (x.kind === "image" ? "<span class='muted'> of its own</span>" : "") : "<span class='muted'>measuring…</span>") + "</div>" +
      "<div class='tl-clean-act'>" + btn("Remove…", "data-clean='" + i + "'" + (x.in_use ? " disabled title='A container still uses this image'" : "")) + "</div>" +
    "</div>";
  }).join("");
}

// ------------------------------------------------------------------ state
const _sys = { update: null, storage: null, network: null, memory: null, cleanup: null, doctor: null, busy: {} };
let _onChange = null;
let _storageTimer = null;

/** Problems the doctor found that the rest of the page doesn't show (disk,
 *  memory, the network, the LanEx folder, the computer), for the verdict band. */
export function sysAttention() {
  const rep = _sys.doctor;
  if (!rep || !Array.isArray(rep.checks)) return [];
  const show = { disk: "tl-sys-storage", memory: "tl-sys-memory", network: "tl-sys-network", files: "tools-system", system: "tools-system" };
  return rep.checks.filter((c) => (c.status === "fail" || c.status === "warn") && show[c.id]).map((c) => ({
    tone: c.status, title: c.title + ": " + c.summary, detail: (c.next && c.next.text) || "",
    show: show[c.id], fix: c.id === "disk" && (_sys.cleanup?.items || []).length ? { label: "Clean up", id: "tl-sys-cleanup", open: true } : null,
  }));
}

function isWsl(info) {
  const c = (info && info.container) || {};
  const h = (info && info.host) || {};
  return c.platform === "wsl" || h.wsl === "yes";
}

// ------------------------------------------------------------------ tiles
function lanexTile() {
  const s = _sys.update;
  const on = prefOn("updateCheck");
  const id = "tl-sys-lanex";
  if (!s) return sysTileHtml({ id, eyebrow: "LanEx", status: "busy", stateText: "Checking…", big: "LanEx" });
  const rel = s.latest || {};
  const checked = s.checked_at ? whenText(s.checked_at) : "";
  const checkBtn = btn(on ? "Check now" : "Check once", "data-sys='update-check'", { title: "Ask GitHub for the newest release now" });
  if (_sys.busy.update) {
    return sysTileHtml({ id, eyebrow: "LanEx", status: "busy", stateText: "Checking…", big: "LanEx", ver: s.current,
      rows: [["Checked", esc(checked || "never")]] });
  }
  if (s.newer && !s.skipped) {
    const how = s.how || {};
    const get = how.platform === "windows"
      ? "<a class='btn btn-sm btn-primary tl-act' href='" + esc(how.download || rel.url || s.releases_url) + "' target='_blank' rel='noopener'>Get LanEx " + esc(rel.version) + " ↗</a>"
      : btn("Copy the update command", "data-sys='update-copy'", { primary: true, title: how.command || "" });
    return sysTileHtml({ id, eyebrow: "LanEx", status: "update", stateText: "Update available", big: "LanEx", ver: rel.version,
      rows: [["You have", "<span class='tl-mono'>" + esc(s.current) + "</span>"],
        rel.published_at && ["Released", esc(whenText(rel.published_at))],
        ["How", esc(how.text || "")]],
      actions: (rel.notes ? btn("What's new", "data-sys='update-notes'") : "<a class='btn btn-sm btn-ghost tl-act' href='" + esc(rel.url) + "' target='_blank' rel='noopener'>What's new ↗</a>") + get,
      menu: [item("Skip " + rel.version, "data-sys='update-skip'"), item("Check now", "data-sys='update-check'")] });
  }
  if (s.newer && s.skipped) {
    return sysTileHtml({ id, eyebrow: "LanEx", status: "off", stateText: rel.version + " skipped", big: "LanEx", ver: s.current,
      rows: [["Newest", "<span class='tl-mono'>" + esc(rel.version) + "</span> <span class='muted'>(skipped; a newer one will show)</span>"],
        checked && ["Checked", esc(checked)]],
      actions: btn("Show it again", "data-sys='update-unskip'") + checkBtn });
  }
  if (!s.checked_at) {
    return sysTileHtml({ id, eyebrow: "LanEx", status: "off", stateText: on ? "Not checked yet" : "Update check off", big: "LanEx", ver: s.current,
      rows: [["Updates", on ? "LanEx asks GitHub once a day while this page is open." : "Turned off in Settings."]],
      actions: checkBtn });
  }
  if (!s.last_ok) {
    return sysTileHtml({ id, eyebrow: "LanEx", status: "off", stateText: "Could not check", big: "LanEx", ver: s.current,
      rows: [["Last try", esc(checked) + (s.error ? " <span class='muted'>(" + esc(s.error) + ")</span>" : "")],
        rel.version && ["Last known", "<span class='tl-mono'>" + esc(rel.version) + "</span>"]],
      actions: checkBtn });
  }
  return sysTileHtml({ id, eyebrow: "LanEx", status: "ready", stateText: "Up to date", big: "LanEx", ver: s.current,
    rows: [["Newest", "<span class='tl-mono'>" + esc(rel.version || s.current) + "</span>" + (rel.published_at ? " <span class='muted'>· " + esc(whenText(rel.published_at)) + "</span>" : "")],
      ["Checked", esc(checked) + (on ? "" : " <span class='muted'>(daily check off)</span>")]],
    actions: checkBtn });
}

function networkTile() {
  const n = _sys.network;
  const id = "tl-sys-network";
  if (!n) return sysTileHtml({ id, eyebrow: "Network", status: "busy", stateText: "Checking…", big: "Network" });
  if (_sys.busy.network) return sysTileHtml({ id, eyebrow: "Network", status: "busy", stateText: "Asking Windows…", big: n.proxy_host ? "Proxy" : "Direct",
    rows: [["Now", "Reading this PC's proxy and certificates, then applying them to LanEx's Linux."]] });
  const canApply = n.windows && n.helper;
  const again = canApply ? btn("Check again", "data-sys='net-apply'", { title: "Ask Windows for its proxy and certificates now and apply them to LanEx's Linux" }) : "";
  const certs = n.certificates_added
    ? n.certificates_added + " company certificate" + (n.certificates_added === 1 ? "" : "s") + " added"
    : "none needed";
  const rows = [];
  if (n.proxy_host) {
    rows.push(["Proxy", "<span class='tl-mono'>" + esc(n.proxy_host) + "</span>"]);
    rows.push(["From", esc(n.proxy_source === "Windows" ? "Windows" + (n.mode === "pac" ? " (automatic setup script)" : n.mode === "wpad" ? " (found automatically)" : "") : n.proxy_source || "—")]);
    if (n.no_proxy) rows.push(["Not for", esc(n.no_proxy.split(",").length + " addresses"), n.no_proxy]);
  }
  rows.push(["Certificates", esc(certs)]);
  if (n.proxy_host && n.docker_uses_proxy != null) rows.push(["Docker", n.docker_uses_proxy ? "uses it <span class='tl-ok'>" + icon("check", { size: 12, stroke: 2 }) + "</span>" : "<span class='tl-bad'>not using it yet</span>"]);
  if (n.applied_at) rows.push(["Applied", esc(whenText(n.applied_at))]);
  const problems = (n.problems || []).length;
  let status = "ready", stateText = n.proxy_host ? "Proxy" : "Direct";
  let sub = "";
  if (problems) {
    status = "broken"; stateText = "Needs attention";
    sub = (n.problems || []).map((p) => "<p class='tl-warn'>" + icon("alert", { size: 12 }) + " " + esc(p) + "</p>").join("");
  } else if (n.pending) {
    status = "update"; stateText = "Applies to Docker when the run ends";
  } else if (n.proxy_host && n.docker_uses_proxy === false) {
    status = "update"; stateText = "Docker not using it";
  }
  return sysTileHtml({ id, eyebrow: "Network", status, stateText, big: n.proxy_host ? "Company proxy" : "Direct connection",
    rows, sub, actions: again, name: "Network" });
}

function memoryTile() {
  const m = _sys.memory;
  const id = "tl-sys-memory";
  if (!m) return sysTileHtml({ id, eyebrow: "Memory for Linux", status: "busy", stateText: "Checking…", big: "Memory" });
  const gb = (x) => (x == null ? "?" : (Math.round(x * 10) / 10) + " GB");
  let status = "ready", stateText = m.value ? "Set to " + m.value : "Windows default";
  const rows = [
    ["Set in", m.value ? ".wslconfig: <span class='tl-mono'>memory=" + esc(m.value) + "</span>" : "Windows decides (half of " + esc(gb(m.windows_total_gb)) + ")", m.path],
    m.linux_available_gb != null && ["Free now", esc(gb(m.linux_available_gb))],
  ];
  if (!m.readable) { status = "update"; stateText = "Can't read .wslconfig"; }
  else if (m.value && !m.valid) { status = "broken"; stateText = "WSL can't read this value"; rows.push(["Fix", "Use whole GB in capitals, for example 12GB."]); }
  else if (m.value && m.applied === false) { status = "update"; stateText = "Applies after WSL restarts"; }
  return sysTileHtml({ id, eyebrow: "Memory for Linux", status, stateText, big: gb(m.linux_total_gb), ver: m.windows_total_gb ? "of " + gb(m.windows_total_gb) : "",
    rows, actions: btn("Change…", "data-sys='mem-change'", { title: "How much of this PC's memory WSL gives Linux" }), name: "Memory for Linux" });
}

function storageHtml() {
  const s = _sys.storage;
  if (!s) return "<div class='tl-store is-loading' id='tl-sys-storage'><div class='tl-store-h'><span class='tl-eyebrow'>Storage</span><span class='tl-store-sum'><span class='spinner-sm'></span> Reading the disk…</span></div><div class='tl-store-bar'></div></div>";
  const p = s.parts || {};
  const parts = [
    { key: "image", label: "Tool image", bytes: p.image, title: "LibreLane container images" },
    { key: "pdks", label: "PDKs", bytes: p.pdks, title: "Process design kits (ciel's store)" },
    { key: "runs", label: "Runs", bytes: p.runs, title: "Run folders of the projects LanEx knows" },
    { key: "other", label: "Other", bytes: p.other, title: "LanEx's own folder: settings, caches, logs" },
  ];
  return storageBarHtml({ drive: s.drive, total: s.total_bytes, free: s.free_bytes, parts, measuring: s.measuring,
    note: s.windows && (_sys.cleanup?.items || []).length ? s.note : "" });
}

function cleanupHtml() {
  const c = _sys.cleanup;
  if (!c) return "";
  const items = c.items || [];
  if (!items.length) return "";
  return "<div class='tl-clean' id='tl-sys-cleanup' data-name='Clean up'>" +
    "<div class='tl-clean-h'><span class='tl-eyebrow'>Clean up</span>" +
      (c.total_bytes ? "<span class='tl-clean-tot'>can free <b>" + esc(fmtBytes(c.total_bytes, { approx: true })) + "</b></span>" : "") + "</div>" +
    cleanupRowsHtml(items) + "</div>";
}

function render() {
  const host = typeof document !== "undefined" ? document.getElementById("tl-sys") : null;
  if (!host) return;
  const wsl = _sys.wsl;
  host.innerHTML =
    storageHtml() +
    "<div class='tl-sys-tiles'>" + lanexTile() + networkTile() + (wsl ? memoryTile() : "") + "</div>" +
    cleanupHtml();
  wire(host);
}

// ------------------------------------------------------------------ actions
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_e) { /* fall back */ }
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_e) { ok = false; }
  ta.remove();
  return ok;
}

function wire(host) {
  host.querySelectorAll("[data-sys]").forEach((b) => b.addEventListener("click", () => onAction(b.dataset.sys, b)));
  host.querySelectorAll("[data-clean]").forEach((b) => b.addEventListener("click", () => onClean(+b.dataset.clean)));
  host.querySelectorAll("[data-store]").forEach((b) => b.addEventListener("click", () => {
    const target = { image: "tl-image", pdks: "tools-pdk", runs: null, other: null }[b.dataset.store];
    const el = target && document.getElementById(target);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    else document.getElementById("tl-sys-cleanup")?.scrollIntoView({ block: "center", behavior: "smooth" });
  }));
}

async function onAction(what, el) {
  el?.closest("details")?.removeAttribute("open");
  if (what === "update-check") {
    _sys.busy.update = true; render();
    try {
      const mod = await import("./updatebanner.js");
      _sys.update = (await mod.checkForUpdates({ force: true })) || _sys.update;
    } catch (_e) { /* the tile says "Could not check" */ }
    _sys.busy.update = false; render();
  } else if (what === "update-skip" || what === "update-unskip") {
    const v = what === "update-skip" ? (_sys.update?.latest?.version || "") : "";
    try { _sys.update = await api.updateSkip(v); } catch (ex) { toast.show("Could not save that: " + ex.message, "error"); }
    try {
      const ub = await import("./updatebanner.js");
      // The banner's own "don't show again" follows the tile.
      safeStorage.set(ub.DISMISS_KEY, v);
      ub.checkForUpdates();
    } catch (_e) {}
    render();
  } else if (what === "update-copy") {
    const cmd = _sys.update?.how?.command || "";
    toast.show((await copyText(cmd)) ? "Command copied. Paste it into a terminal, then start LanEx again." : "Copy the command from the tile's tooltip.", "success");
  } else if (what === "update-notes") {
    const rel = _sys.update?.latest || {};
    await customDialog({ title: "What's new in LanEx " + rel.version, wide: true,
      bodyHtml: "<pre class='tl-notes'>" + esc(rel.notes || "") + "</pre>" +
        "<p class='hint'><a href='" + esc(rel.url) + "' target='_blank' rel='noopener'>Full notes on GitHub ↗</a></p>" });
  } else if (what === "net-apply") {
    _sys.busy.network = true; render();
    try {
      const r = await api.networkApply();
      _sys.network = r.status || _sys.network;
      const a = r.applied || {};
      toast.show(a.mode && a.mode !== "none"
        ? "LanEx's Linux now uses the proxy Windows uses" + (a.certificates ? " and " + a.certificates + " company certificate" + (a.certificates === 1 ? "" : "s") : "") + "."
        : "Direct connection: no proxy needed." + (a.pending ? " Docker picks it up when the run ends." : ""), "success");
    } catch (ex) {
      toast.show("Could not apply the network settings: " + ex.message + " Log: the Logs panel and launcher.log.", "error");
    }
    _sys.busy.network = false; render();
  } else if (what === "mem-change") {
    await memoryDialog();
  }
}

async function memoryDialog() {
  const m = _sys.memory || {};
  const total = m.windows_total_gb || 0;
  const max = m.max_gb || Math.max(2, Math.floor(total) - 4);
  const min = m.min_gb || 2;
  const cur = m.gb ? Math.round(m.gb) : 0;
  const deflt = total ? Math.round(total / 2) : 0;
  const v = await customDialog({
    title: "Memory for LanEx's Linux",
    bodyHtml:
      "<p>WSL gives Linux half of this PC's memory unless you choose a size. This setting is shared by every WSL distribution on this PC (Docker Desktop too, if you use it).</p>" +
      "<div class='tl-mem-choice' role='radiogroup' aria-label='Memory for Linux'>" +
        "<label class='tl-mem-opt'><input type='radio' name='tl-mem' value='default'" + (cur ? "" : " checked") + "> Windows default" +
          (deflt ? ": <b class='tl-mono'>" + deflt + " GB</b> of " + Math.round(total) + " GB" : "") + "</label>" +
        "<label class='tl-mem-opt'><input type='radio' name='tl-mem' value='size'" + (cur ? " checked" : "") + "> Choose a size: " +
          "<input type='number' id='tl-mem-gb' class='tl-mem-gb' min='" + min + "' max='" + max + "' step='1' value='" + (cur || Math.min(max, Math.max(min, deflt + 4))) + "' aria-label='GB for Linux'> GB" +
          "<span class='muted'> " + min + " to " + max + " GB</span></label>" +
      "</div>" +
      "<p class='tl-mem-err' id='tl-mem-err' role='alert' hidden></p>" +
      "<p>It takes effect the next time WSL starts: quit LanEx, close any other Linux windows, wait a minute, then open LanEx again. LanEx does not restart WSL for you, because that would also close your other Linux programs.</p>" +
      (m.exists ? "<p class='hint'>LanEx saves a copy of your current file first, next to it in your user folder.</p>" : ""),
    buttons: [{ label: "Cancel", value: false, cls: "btn-ghost" }, { label: "Save", value: "save", cls: "btn-primary" }],
    onMount: (back) => {
      const n = back.querySelector("#tl-mem-gb");
      // Typing a size chooses "Choose a size" (focus alone does not: the
      // dialog may focus the field when it opens).
      n?.addEventListener("input", () => {
        n.value = n.value.replace(/[^0-9]/g, "");
        const r = back.querySelector("input[value='size']"); if (r) r.checked = true;
      });
      // Open on the current choice, not on the number field.
      back.querySelector("input[name='tl-mem']:checked")?.focus();
      // Read the choice on the way out (the dialog removes its markup).
      back.addEventListener("click", () => {
        const pick = back.querySelector("input[name='tl-mem']:checked");
        _memPick = { kind: pick ? pick.value : "default", gb: parseInt(n?.value || "0", 10) };
      }, true);
      back.addEventListener("keydown", () => {
        const pick = back.querySelector("input[name='tl-mem']:checked");
        _memPick = { kind: pick ? pick.value : "default", gb: parseInt(n?.value || "0", 10) };
      }, true);
    },
  });
  if (v !== "save" || !_memPick) return;
  let gb = null;
  if (_memPick.kind === "size") {
    gb = _memPick.gb;
    if (!(gb >= min && gb <= max)) { toast.show("Choose between " + min + " and " + max + " GB.", "warn"); return; }
  }
  try {
    const r = await api.setWslMemory(gb);
    _sys.memory = r.status || _sys.memory;
    toast.show((gb ? "Memory for Linux set to " + gb + " GB." : "Memory for Linux is back to Windows' default.") +
      " It applies the next time WSL starts." + (r.backup ? " Your previous file is saved as " + r.backup.split("/").pop() + "." : ""), "success");
  } catch (ex) {
    toast.show("Memory for Linux was not changed: " + ex.message, "error");
  }
  render();
}
let _memPick = null;

async function onClean(i) {
  const x = ((_sys.cleanup && _sys.cleanup.items) || [])[i];
  if (!x) return;
  const size = x.bytes ? fmtBytes(x.bytes, { approx: true }) : "";
  if (x.kind === "image") {
    const cur = (_sys.cleanup.current_image || "").split("@")[0];
    const ver = x.version ? "LibreLane " + x.version : "the untagged LibreLane";
    const ok = await confirmDialog({
      title: "Remove the " + ver + " image?", danger: true, confirmText: "Remove " + (x.version ? x.version + " " : "") + "image",
      body: "This deletes " + x.ref + " (image " + x.short + ") from LanEx's container engine" + (size ? " and frees " + size : "") +
        ". Finished runs keep their results; runs started with " + ver + " can't be resumed after this. You can download it again later. " +
        "Kept: " + (cur || "the image LanEx uses") + " (in use), your other container images, and all projects." +
        (state.tools?.container?.platform === "wsl" ? "" : " Another LibreLane on this computer (outside LanEx) may still use it."),
    });
    if (!ok) return;
    try {
      const r = await api.removeImage(x.id);
      toast.show("Removed the " + ver + " image." + (r.freed_bytes ? " Freed " + fmtBytes(r.freed_bytes) + "." : ""), "success");
    } catch (ex) {
      toast.show("The image was not removed: " + ex.message, "error");
    }
  } else if (x.kind === "pdk-version") {
    const ok = await confirmDialog({
      title: "Remove the old " + x.family + " version?", danger: true, confirmText: "Remove version " + x.version.slice(0, 7),
      body: "This deletes " + x.family + " version " + x.version.slice(0, 7) + " (" + (x.variants || []).join(" and ") + ")" +
        (size ? " and frees " + size : "") + ". LibreLane uses a different version, which stays. Runs made with the old version keep " +
        "their results, but can't be resumed or re-run exactly after this. Kept: your projects and run results.",
    });
    if (!ok) return;
    try {
      const r = await api.removePdkVersion(x.family, x.version);
      toast.show("Removed " + x.family + " version " + x.version.slice(0, 7) + "." + (r.freed_bytes ? " Freed " + fmtBytes(r.freed_bytes) + "." : ""), "success");
    } catch (ex) {
      toast.show("The PDK version was not removed: " + ex.message, "error");
    }
  }
  await refresh({ storage: true, cleanup: true });
}

// ------------------------------------------------------------------ load
async function refresh(which) {
  const jobs = [];
  if (which.update) jobs.push(api.updateStatus().then((s) => { _sys.update = s; }).catch(() => {}));
  if (which.storage) jobs.push(api.storage(true).then((s) => { _sys.storage = s; pollStorage(); }).catch(() => {}));
  if (which.cleanup) jobs.push(api.cleanupList().then((c) => { _sys.cleanup = c; }).catch(() => { _sys.cleanup = { items: [] }; }));
  await Promise.all(jobs);
  render();
  if (_onChange) _onChange();
}

function pollStorage() {
  if (_storageTimer) clearTimeout(_storageTimer);
  if (!_sys.storage || !_sys.storage.measuring) return;
  _storageTimer = setTimeout(async () => {
    try { _sys.storage = await api.storage(); } catch (_e) { return; }
    render();
    pollStorage();
  }, 3000);
}

/** Paint the section; `onChange` repaints the verdict band when the doctor's
 *  answer arrives. Every part loads on its own, so a slow one never blocks. */
export async function paintSystem(info, { onChange = null, fresh = false } = {}) {
  _onChange = onChange;
  _sys.wsl = isWsl(info);
  render();
  const parts = [
    api.updateStatus().then((s) => { _sys.update = s; render(); }).catch(() => { _sys.update = { current: "", checked_at: 0 }; render(); }),
    api.storage(fresh).then((s) => { _sys.storage = s; render(); pollStorage(); }).catch(() => {}),
    api.networkStatus().then((n) => { _sys.network = n; render(); }).catch(() => {}),
    api.cleanupList().then((c) => { _sys.cleanup = c; render(); }).catch(() => { _sys.cleanup = { items: [] }; }),
    api.doctor("offline=1" + (fresh ? "&fresh=1" : "")).then((d) => { _sys.doctor = d; if (_onChange) _onChange(); }).catch(() => {}),
  ];
  if (_sys.wsl) parts.push(api.wslMemory().then((m) => { _sys.memory = m; render(); }).catch(() => {}));
  await Promise.all(parts);
}

/** "Copy diagnostics": the doctor's share form (no user name, no proxy
 *  address), this browser and window, and the page's own facts. */
export async function diagnosticsBundle(pageText = "") {
  let doc = "";
  try {
    const r = await api.doctor("share=1&text=1");
    doc = r.text || "";
  } catch (ex) {
    doc = "lanex doctor could not run: " + ex.message + "\n";
  }
  const zoom = (() => { try { return getComputedStyle(document.documentElement).zoom || "1"; } catch (_e) { return "?"; } })();
  const browser = "== Browser ==\n" + navigator.userAgent + "\nWindow " + window.innerWidth + " x " + window.innerHeight +
    " CSS px, device pixel ratio " + (window.devicePixelRatio || 1) + ", zoom " + zoom + "\n";
  return doc + "\n" + browser + (pageText ? "\n== Tools page ==\n" + pageText + "\n" : "");
}
