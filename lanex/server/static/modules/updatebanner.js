// updatebanner.js — "a new LanEx is out" (audit plan 5a).
//
// While the "Check for updates" setting is on (the default; Settings says what
// it does), the page asks the server once after it loads. The server goes to
// GitHub at most once a day and sends nothing about the person
// (controller/updatecheck.py). When a newer release exists, a small card under
// the top bar says which version, links to what's new, says how to update on
// this platform (Windows: the new Setup; Linux and macOS: the one-line
// installer) and can be dismissed for that version.

import { api, fmt } from "./api.js";
import { safeStorage } from "./state.js";
import { prefOn } from "./prefs.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));
export const DISMISS_KEY = "ll.updateDismissed";

let _last = null;          // the last status the server sent
let _closed = false;       // closed for this page load (the x), not dismissed

export function lastStatus() { return _last; }

// Show the card for this status? Not when the person dismissed this version.
export function shouldShow(s, dismissed) {
  // `skipped`: "Skip this version" on Tools (kept on the server, so every
  // window and the Tools tile agree).
  return !!(s && s.newer && !s.skipped && s.latest && s.latest.version && s.latest.version !== dismissed);
}

export function bannerHtml(s) {
  const rel = s.latest || {};
  const how = s.how || {};
  const step = how.platform === "windows"
    ? "<div class='ub-acts'><a class='btn btn-sm btn-primary' href='" + esc(how.download || rel.url) +
        "' target='_blank' rel='noopener'>Download LanEx Setup " + esc(rel.version) + "</a>" +
        "<a class='btn btn-sm btn-ghost' href='" + esc(rel.url) + "' target='_blank' rel='noopener'>What's new ↗</a></div>"
    : "<pre class='code ub-cmd' id='ub-cmd'>" + esc(how.command || "") + "</pre>" +
      "<div class='ub-acts'><button class='btn btn-sm btn-primary' data-ub='copy'>Copy command</button>" +
        "<a class='btn btn-sm btn-ghost' href='" + esc(rel.url) + "' target='_blank' rel='noopener'>What's new ↗</a></div>";
  return (
    "<div class='ub-top'><span class='an-eyebrow'>Update available</span>" +
      "<button class='iconbtn iconbtn-sm ub-x' data-ub='close' aria-label='Close until LanEx is opened again' title='Close until LanEx is opened again'>" +
      "<svg viewBox='0 0 24 24' width='15' height='15' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M6 6l12 12M18 6L6 18'/></svg></button></div>" +
    "<div class='ub-title'>LanEx <span class='ub-ver'>" + esc(rel.version) + "</span></div>" +
    "<p class='ub-text'>You have " + esc(s.current) + ". " + esc(how.text || "") + "</p>" +
    step +
    "<div class='ub-foot'><button class='linklike' data-ub='dismiss'>Don't show " + esc(rel.version) + " again</button>" +
      "<span class='ub-note'>Checked once a day. <button class='linklike' data-ub='settings'>Settings</button></span></div>"
  );
}

function paint(s) {
  const el = typeof document !== "undefined" ? document.getElementById("update-banner") : null;
  if (!el) return;
  const show = !_closed && prefOn("updateCheck") && shouldShow(s, safeStorage.get(DISMISS_KEY, ""));
  if (!show) { el.hidden = true; el.innerHTML = ""; return; }
  el.innerHTML = bannerHtml(s);
  el.hidden = false;
  if (!el._wired) {
    el._wired = true;
    el.addEventListener("click", onClick);
  }
}

async function onClick(e) {
  const b = e.target.closest("[data-ub]");
  if (!b) return;
  const what = b.dataset.ub;
  if (what === "close") { _closed = true; paint(_last); }
  else if (what === "dismiss") {
    if (_last && _last.latest) {
      safeStorage.set(DISMISS_KEY, _last.latest.version);
      // The same "skip this version" the Tools tile shows and can undo.
      try { _last = await api.updateSkip(_last.latest.version); } catch (_e) { /* the local dismiss holds */ }
    }
    paint(_last);
    document.dispatchEvent(new CustomEvent("g:update_status", { detail: _last }));
  } else if (what === "settings") {
    (await import("./nav.js")).go("settings");
  } else if (what === "copy") {
    const cmd = (_last && _last.how && _last.how.command) || "";
    const { toast } = await import("./toast.js");
    try { await navigator.clipboard.writeText(cmd); toast.show("Command copied. Paste it into a terminal.", "success"); }
    catch (_e) {
      const pre = document.getElementById("ub-cmd");
      if (pre && window.getSelection) { const r = document.createRange(); r.selectNodeContents(pre); const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); }
      toast.show("Select the command and copy it with Ctrl+C.", "info");
    }
  }
}

// Ask the server (it goes online at most once a day; force asks now). Never
// throws; offline or turned off it quietly does nothing.
export async function checkForUpdates({ force = false } = {}) {
  if (!force && !prefOn("updateCheck")) { paint(null); return null; }
  let s = null;
  try { s = await api.updateCheck(force); } catch (_e) { s = null; }
  if (s) _last = s;
  paint(_last);
  try { document.dispatchEvent(new CustomEvent("g:update_status", { detail: _last })); } catch (_e) {}
  return _last;
}

// What is known without going online (the Settings page's line).
export async function updateStatus() {
  try { _last = await api.updateStatus(); } catch (_e) {}
  return _last;
}

// The setting changed: off hides the card, on checks now (once a day still).
export function onPrefChanged(on) {
  if (on) { _closed = false; checkForUpdates(); }
  else paint(null);
}

// After boot, once the page has settled: one quiet check.
export function setupUpdateBanner() {
  setTimeout(() => { checkForUpdates(); }, 3000);
}
