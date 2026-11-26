// settings.js — the Settings page: every preference in one place.
//
// The page is drawn from the registry (prefs.js). Each control writes the
// same key the owning module reads, then tells that module (a few of them
// keep a control of their own in the Run menu or the IDE toolbar, so both
// stay in step).

import { api, fmt } from "./api.js";
import { safeStorage } from "./state.js";
import { PREFS, getPref, setPref, resetPrefs } from "./prefs.js";
import { setTheme, themeChoice } from "./theme.js";
import { currentZoom, isAutoZoom, resetZoom, zoomBy } from "./zoom.js";
import { SHORTCUTS, IDE_SHORTCUTS } from "./hotkeys.js";
import { toast } from "./toast.js";
import { ago } from "./metricfmt.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));

// What changing a preference does beyond storing it.
const APPLY = {
  theme: (v) => setTheme(v),
  density: (v) => document.body.classList.toggle("density-compact", v === "compact"),
  engine: async (v) => {
    (await import("./runmode.js")).setRunMode(v);
    (await import("./shell.js")).paintRunSettings();
  },
  notify: async (v) => {
    const r = await (await import("./runend.js")).enableNotifications(v === "1");
    if (!r.ok) { setPref("notify", "0"); toast.show(r.why, "warn"); }
    syncMirror("run-pref-notify", getPref("notify") === "1");
  },
  showFlow: () => syncMirror("run-pref-show", getPref("showFlow") === "1"),
  chime: () => syncMirror("run-pref-chime", getPref("chime") === "1"),
  lintOnSave: () => syncMirror("ide-autolint", getPref("lintOnSave") === "1"),
  updateCheck: async (v) => {
    (await import("./updatebanner.js")).onPrefChanged(v === "1");
    paintUpdateLine();
  },
};

// The Updates card's status line: this version, and what the last check said.
// The one "how long ago" (metricfmt.ago).
const agoText = (t) => ago(t);

export function updateLineHtml(s, on) {
  if (!s) return "<span class='muted'>No check yet.</span>";
  const cur = "LanEx " + esc(s.current);
  const off = on ? "" : " <span class='muted'>The daily check is off.</span>";
  if (!s.checked_at) return cur + "." + (on ? " <span class='muted'>Not checked yet.</span>" : off);
  const when = " <span class='muted'>Checked " + esc(agoText(s.checked_at)) + (s.last_ok ? "" : ", GitHub could not be reached") + ".</span>";
  if (s.newer && s.latest)
    return cur + ". <b>" + esc(s.latest.version) + " is out</b> (<a href='" + esc(s.latest.url) + "' target='_blank' rel='noopener'>what's new</a>)." + when + off;
  if (s.latest) return cur + ", the newest release." + when + off;
  return cur + "." + when + off;
}

async function paintUpdateLine(status) {
  const el = typeof document !== "undefined" ? document.getElementById("st-update-line") : null;
  if (!el) return;
  let s = status;
  if (!s) {
    const m = await import("./updatebanner.js");
    s = m.lastStatus() || await m.updateStatus();
  }
  el.innerHTML = updateLineHtml(s, getPref("updateCheck") === "1");
}

function syncMirror(id, on) {
  const el = document.getElementById(id);
  if (el) el.checked = on;
}

const MINUS = "<svg viewBox='0 0 24 24' width='15' height='15' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M5 12h14'/></svg>";
const PLUS = "<svg viewBox='0 0 24 24' width='15' height='15' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M12 5v14M5 12h14'/></svg>";

function control(p) {
  const v = getPref(p.id);
  if (p.type === "bool") {
    return "<button class='st-switch' role='switch' data-pref='" + p.id + "' aria-checked='" + (v === p.on) +
      "' aria-labelledby='st-l-" + p.id + "'><span aria-hidden='true'></span></button>";
  }
  if (p.type === "choice") {
    return "<div class='seg st-seg' role='group' aria-labelledby='st-l-" + p.id + "'>" +
      p.options.map(([val, label]) =>
        "<button class='seg-btn" + (v === val ? " is-active" : "") + "' data-pref='" + p.id + "' data-val='" + esc(val) +
        "' aria-pressed='" + (v === val) + "'>" + esc(label) + "</button>").join("") + "</div>";
  }
  if (p.id === "zoom") {
    return "<span class='st-zoom'>" +
      "<button class='btn btn-sm" + (isAutoZoom() ? " is-on" : "") + "' data-zoom='auto' aria-pressed='" + isAutoZoom() + "'>Automatic</button>" +
      "<button class='iconbtn iconbtn-sm' data-zoom='-' aria-label='Smaller'>" + MINUS + "</button>" +
      "<output id='st-zoom-pct'>" + Math.round(currentZoom() * 100) + "%</output>" +
      "<button class='iconbtn iconbtn-sm' data-zoom='+' aria-label='Larger'>" + PLUS + "</button></span>";
  }
  return "";
}

function row(title, hint, ctl, id = "") {
  return "<div class='st-row'><div class='st-row-l'><span class='st-row-t'" + (id ? " id='st-l-" + id + "'" : "") + ">" + esc(title) + "</span>" +
    (hint ? "<span class='st-row-h'>" + esc(hint) + "</span>" : "") + "</div><div class='st-row-c'>" + ctl + "</div></div>";
}

const prefRows = (group) => PREFS.filter((p) => p.group === group).map((p) => row(p.label, p.hint, control(p), p.id)).join("");

function keysHtml() {
  const line = (k, d) => "<div class='keys-row'><span>" + k.split(" / ").map((x) => "<kbd>" + esc(x) + "</kbd>").join(" ") +
    "</span><span class='keys-desc'>" + esc(d) + "</span></div>";
  return "<details class='st-keys'><summary>Every shortcut</summary>" +
    "<div class='keys'>" + SHORTCUTS.map((s) => line(s.keys, s.desc)).join("") + "</div>" +
    "<h4 class='st-keys-h'>RTL IDE</h4><div class='keys'>" + IDE_SHORTCUTS.map((s) => line(s.keys, s.desc)).join("") + "</div></details>";
}

async function aboutHtml() {
  let about = null;
  try {
    about = await api.about();
  } catch (_e) {}
  const ver = about && about.version, ll = about && about.librelane;
  return "<dl class='about-facts'>" +
    (ver ? "<dt>LanEx</dt><dd>" + esc(ver) + "</dd>" : "") +
    (ll ? "<dt>LibreLane</dt><dd>" + esc(ll) + "</dd>" : "") +
    "<dt>License</dt><dd>Apache License 2.0, provided AS IS, without warranty</dd>" +
    "<dt>Built on</dt><dd>LibreLane, OpenROAD, Yosys, Magic, KLayout and Netgen, run as separate programs (not bundled)</dd>" +
    "<dt>Affiliation</dt><dd>Independent project, not affiliated with or endorsed by LibreLane</dd></dl>" +
    "<div class='about-warn'><b>Not a sign-off tool.</b> Every number LanEx shows comes from the EDA tools. " +
    "Before you fabricate, check your design against your foundry's official sign-off decks.</div>" +
    "<div class='st-acts'><button class='btn btn-sm' data-st='notices'>License and notices</button>" +
    // Updates, what's new, disk, network and memory live on Tools.
    "<button class='btn btn-sm btn-ghost' data-st='system'>Updates, disk and network</button>" +
    "<a class='btn btn-sm btn-ghost' href='https://librelane.org/' target='_blank' rel='noopener'>LibreLane docs</a>" +
    "<a class='btn btn-sm btn-ghost' href='https://github.com/lanex-eda/lanex' target='_blank' rel='noopener'>LanEx on GitHub</a></div>";
}

function card(title, body, cls = "") {
  return "<section class='an-card st-card " + cls + "'><h3 class='st-card-h'>" + esc(title) + "</h3>" + body + "</section>";
}

export async function renderSettings() {
  const host = document.getElementById("settings-body");
  if (!host) return;
  wireOnce(host);
  const btn = (act, label) => "<button class='btn btn-sm' data-st='" + act + "'>" + label + "</button>";
  host.innerHTML =
    "<div class='st-col'>" +
    card("Appearance", prefRows("Appearance")) +
    card("Runs", prefRows("Runs")) +
    card("Editor", prefRows("Editor")) +
    card("Layout", prefRows("Layout")) +
    card("Keyboard", prefRows("Keyboard") + keysHtml()) +
    "</div><div class='st-col'>" +
    card("Getting started",
      row("First-chip guide", "Build the Blinky example step by step: simulate it, run the flow, see the layout.", btn("first-chip", "Start")) +
      row("Tour", "Five stops: where everything is.", btn("tour", "Start")) +
      row("First-launch card", "Show the welcome choice again the next time LanEx starts.", btn("reset-start", "Show again"))) +
    card("Data",
      row("Preferences", "Back to the defaults: everything on this page, plus column choices, sort orders and panel sizes. Projects and runs are not touched.", btn("reset", "Reset…")) +
      row("Recent projects", "Clear the list this window remembers. The folders stay where they are.", btn("clear-recent", "Clear"))) +
    card("Updates", prefRows("Updates") +
      "<div class='st-row'><div class='st-row-l'><span class='st-row-t'>This version</span>" +
      "<span class='st-row-h' id='st-update-line'><span class='muted'>Loading…</span></span></div>" +
      "<div class='st-row-c'>" + btn("update-now", "Check now") + "</div></div>") +
    card("About", "<div id='st-about'><span class='muted'>Loading…</span></div>") +
    "</div>";
  aboutHtml().then((h) => { const el = document.getElementById("st-about"); if (el) el.innerHTML = h; });
  paintUpdateLine().catch(() => {});
}

function repaintZoom(host) {
  const o = host.querySelector("#st-zoom-pct");
  if (o) o.textContent = Math.round(currentZoom() * 100) + "%";
  const a = host.querySelector("[data-zoom='auto']");
  if (a) { a.setAttribute("aria-pressed", isAutoZoom() ? "true" : "false"); a.classList.toggle("is-on", isAutoZoom()); }
}

function wireOnce(host) {
  if (host._wired) return;
  host._wired = true;
  host.addEventListener("click", async (e) => {
    const sw = e.target.closest(".st-switch[data-pref]");
    if (sw) {
      const p = PREFS.find((x) => x.id === sw.dataset.pref);
      const on = sw.getAttribute("aria-checked") !== "true";
      sw.setAttribute("aria-checked", on ? "true" : "false");
      setPref(p.id, on ? p.on : p.off);
      if (APPLY[p.id]) await APPLY[p.id](on ? p.on : p.off);
      if (p.id === "notify") sw.setAttribute("aria-checked", getPref("notify") === "1" ? "true" : "false");
      return;
    }
    const ch = e.target.closest("[data-pref][data-val]");
    if (ch) {
      const id = ch.dataset.pref, val = ch.dataset.val;
      setPref(id, val);
      ch.parentElement.querySelectorAll("[data-pref]").forEach((b) => {
        b.setAttribute("aria-pressed", b === ch ? "true" : "false");
        b.classList.toggle("is-active", b === ch);
      });
      if (APPLY[id]) await APPLY[id](val);
      return;
    }
    const z = e.target.closest("[data-zoom]");
    if (z) {
      if (z.dataset.zoom === "auto") resetZoom(); else zoomBy(z.dataset.zoom === "+" ? 1 : -1);
      repaintZoom(host);
      return;
    }
    const a = e.target.closest("[data-st]");
    if (!a) return;
    const act = a.dataset.st;
    if (act === "first-chip" || act === "tour") document.dispatchEvent(new CustomEvent("g:guide", { detail: { action: act } }));
    else if (act === "reset-start") document.dispatchEvent(new CustomEvent("g:guide", { detail: { action: "reset" } }));
    else if (act === "notices") (await import("./shell.js")).openHelp("about");
    else if (act === "system") (await import("./nav.js")).go("tools", { item: "tl-sys-lanex" });
    else if (act === "update-now") {
      a.disabled = true;
      const line = document.getElementById("st-update-line");
      if (line) line.innerHTML = "<span class='spinner-sm'></span> Asking GitHub…";
      // An explicit click asks even with the daily check off: it is the
      // person asking, once.
      const s = await (await import("./updatebanner.js")).checkForUpdates({ force: true });
      a.disabled = false;
      paintUpdateLine(s || undefined);
      if (s && !s.last_ok) toast.show("GitHub could not be reached. LanEx will try again later.", "warn");
      else if (s && !s.newer) toast.show("LanEx " + s.current + " is the newest release.", "success");
    }
    else if (act === "clear-recent") {
      safeStorage.remove("ll.recentDesigns");
      toast.show("Recent projects cleared in this window.", "success");
      document.dispatchEvent(new CustomEvent("g:projects_changed"));
    } else if (act === "reset") {
      const { confirmDialog } = await import("./dialog.js");
      const ok = await confirmDialog({ title: "Reset every preference?",
        body: "Theme, size, run preferences, column choices and panel sizes go back to their defaults. Projects, runs and files are not touched.",
        confirmText: "Reset", danger: true });
      if (!ok) return;
      resetPrefs();
      setTheme(themeChoice());
      resetZoom();
      document.body.classList.remove("density-compact");
      await APPLY.engine("container");
      ["showFlow", "notify", "chime", "lintOnSave"].forEach((id) => { if (id !== "notify") APPLY[id](); });
      syncMirror("run-pref-notify", false);
      (await import("./updatebanner.js")).onPrefChanged(getPref("updateCheck") === "1");
      renderSettings();
      toast.show("Preferences are back to their defaults.", "success");
    }
  });
  window.addEventListener("g:zoom", () => repaintZoom(host));
  document.addEventListener("g:update_status", (e) => paintUpdateLine(e.detail || undefined).catch(() => {}));
  // The same preference changed somewhere else (the theme button, the t key,
  // the Run menu): show it.
  document.addEventListener("g:theme_changed", () => {
    const c = themeChoice();
    host.querySelectorAll("[data-pref='theme']").forEach((b) => {
      b.setAttribute("aria-pressed", b.dataset.val === c ? "true" : "false");
      b.classList.toggle("is-active", b.dataset.val === c);
    });
  });
}
