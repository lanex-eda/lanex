// connection.js — one connection-lost state for the whole window (audit 4.12).
//
// When the LanEx server stops answering, every page used to show its own
// "Failed to fetch". Now a failed request or a dropped live stream only starts
// a probe of /api/health; the probe decides. Nothing shows for a blip (under
// 2 s). Then a quiet chip in the status line ("RECONNECTING…"), and after 10 s
// a card under the top bar that says what is happening and what to do, with
// the log's path. It never covers the page, never takes focus, and goes away
// by itself when LanEx answers again: same boot = a blip (the stream picks up
// where it left), a new boot = LanEx restarted (the project is opened again).
//
// The page can't tell "the server died" from "the WSL distro stopped": both
// refuse at once on 127.0.0.1. So the words follow what the page knows: how
// long, refused or not answering, after sleep, and whether LanEx.exe started
// this server (it restarts it by itself). Research: research/LANE-Z1-research.md.
//
// Leaf module: no imports (api.js imports it). Pure parts are exported and
// tested in frontend_test.mjs.

export const T = {
  chip: 2000,              // nothing before: a blip
  card: 10000,             // the card after this long
  gone: 100000,            // "has not come back" (LanEx.exe waits up to 90 s for a restart)
  goneNoLauncher: 30000,   // the same without LanEx.exe (Linux, macOS, a dev server)
  woke: 20000,             // "waking up" for this long after a sleep was seen
};
export const LOG_PATH = "%LOCALAPPDATA%\\LanEx\\logs\\launcher.log";

// The probe schedule while not connected: 0.5, 1, 2, 2, 3 s, then every 3 s
// to 2 minutes, every 10 s to 10 minutes, then every 30 s. It never gives up:
// LanEx.exe can bring the server back minutes later. Pure (tested).
export function nextDelay(tries, downFor) {
  const first = [500, 1000, 2000, 2000, 3000];
  if (tries < first.length) return first[tries];
  if (downFor < 120000) return 3000;
  if (downFor < 600000) return 10000;
  return 30000;
}

const secs = (ms) => Math.max(0, Math.round(ms / 1000));
export function agoWords(ms) {
  const s = secs(ms);
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60);
  return m + " min" + (s % 60 && m < 5 ? " " + (s % 60) + " s" : "");
}
// "over 20 s", "over 2 minutes": the card's words change every 10 s at most
// (the exact count is in its corner). Pure (tested).
export function overWords(ms) {
  const s = secs(ms);
  if (s < 60) return "over " + Math.max(10, Math.floor(s / 10) * 10) + " s";
  const m = Math.floor(s / 60);
  return "over " + m + " minute" + (m === 1 ? "" : "s");
}

// What to show, from what the page knows. Pure (tested).
//   down: not connected; since: first failure (ms); now; kind: "refused" |
//   "timeout" | "foreign"; launcher: LanEx.exe started this server (null =
//   unknown, said as Windows-or-not by `windows`); wokeAt: when a sleep was
//   seen; key: the key-lost message. Returns {phase, chip, card}.
export function connView({ down, since = 0, now = 0, kind = "refused", launcher = null, windows = false,
  wokeAt = 0, key = "", every = 3000 } = {}) {
  if (key) {
    return { phase: "key", chip: { tone: "fail", text: "NOT SIGNED IN" },
      card: { tone: "fail", eyebrow: "NOT SIGNED IN", title: "Open LanEx again", text: key, retry: false, foot: null } };
  }
  if (!down) return { phase: "ok", chip: null, card: null };
  const t = now - since;
  if (t < T.chip) return { phase: "blip", chip: null, card: null };
  const woke = wokeAt && now - wokeAt < T.woke;
  const checking = "Checking every " + agoWords(every);
  const lx = launcher === true || (launcher == null && windows);
  if (kind === "foreign") {
    return { phase: "foreign", chip: { tone: "fail", text: "WRONG ADDRESS" },
      card: { tone: "fail", eyebrow: "WRONG ADDRESS", title: "Something else is answering here",
        text: "Another program answered at " + (typeof location !== "undefined" ? location.host : "this address") +
          ", not LanEx. Quit LanEx from its tray icon and open it again: LanEx then picks a free port.",
        retry: true, every: checking, foot: lx ? "log" : null } };
  }
  if (t < T.card) {
    return { phase: woke ? "waking" : "reconnecting",
      chip: woke ? { tone: "warn", text: "WAKING UP…", tip: "The PC was asleep. LanEx is reconnecting." }
        : { tone: "warn", text: "RECONNECTING…", tip: "LanEx didn't answer. Trying again every few seconds." },
      card: null };
  }
  if (kind === "timeout") {
    return { phase: "not-answering", chip: { tone: "warn", text: "NOT RESPONDING" },
      card: { tone: "warn", eyebrow: "NOT RESPONDING", title: "LanEx is not answering",
        text: "The LanEx server is still there but hasn't replied for " + overWords(t) + ". It may be busy with a long job" +
          (lx ? ", or Windows may have lost its link to LanEx's environment (WSL) after sleep. LanEx checks this by itself and reconnects."
            : ". This window reconnects by itself when it answers."),
        retry: true, every: checking, foot: lx ? "log" : "terminal" } };
  }
  const gone = t >= (lx ? T.gone : T.goneNoLauncher);
  if (!gone) {
    return { phase: lx ? "restarting" : "lost", chip: { tone: "warn", text: "NOT CONNECTED" },
      card: { tone: "warn", eyebrow: "NOT CONNECTED", title: lx ? "LanEx is restarting" : "LanEx is not answering",
        text: lx
          ? "The LanEx server stopped answering. LanEx starts it again by itself, which usually takes under a minute. Your open files and unsaved edits stay in this window."
          : "The LanEx server stopped answering. If it was stopped on purpose, start it again: this window reconnects by itself. Your open files and unsaved edits stay here.",
        retry: true, every: checking, foot: lx ? "log" : "terminal" } };
  }
  return { phase: "gone", chip: { tone: "fail", text: "NOT CONNECTED" },
    card: lx
      ? { tone: "fail", eyebrow: "NOT CONNECTED", title: "LanEx has not come back",
          text: "It hasn't answered for " + overWords(t) + ". Look for the LanEx icon in the taskbar's tray. If it's gone, " +
            "open LanEx from the Start menu: it starts LanEx's environment (WSL) and the server, and this window reconnects " +
            "by itself. If LanEx won't start, run Repair LanEx from the Start menu; it keeps your projects and runs. " +
            "A run that was going may have stopped with the server: once LanEx is back, the queue chip says what happened.",
          retry: true, every: checking, foot: "log" }
      : { tone: "fail", eyebrow: "NOT CONNECTED", title: "LanEx has stopped",
          text: "It hasn't answered for " + overWords(t) + ". Start it again: run lanex in a terminal, or open the LanEx app. " +
            "This window reconnects by itself. The terminal that started LanEx shows why it stopped.",
          retry: true, every: checking, foot: "terminal" } };
}

// A probe's answer as one word. Pure (tested): res {ok, status, body} or an
// error thrown by fetch.
export function probeKind(res, err) {
  if (err) return err.name === "TimeoutError" || err.name === "AbortError" ? "timeout" : "refused";
  if (res && res.body && res.body.service === "lanex") return "ok";
  return "foreign";
}

// ------------------------------------------------------------------ the controller
const S = {
  down: false, since: 0, kind: "refused", foreignN: 0, timeouts: 0, tries: 0, timer: 0, tick: 0,
  boot: null, launcher: null, wokeAt: 0, lastTick: 0, key: "", shownCard: false, onBack: null, flash: 0,
};
const hasDom = typeof document !== "undefined";
const isWindows = () => typeof navigator !== "undefined" && /Windows/i.test(navigator.userAgent || "");

// api.js hands its reconnect: onBack({restarted, wasShown}).
export function initConnection({ onBack } = {}) {
  S.onBack = onBack || null;
  if (!hasDom) return;
  probe({ quiet: true });          // learn boot and launcher while connected
  window.addEventListener("keydown", onKey, true);
  const wake = () => { if (S.down) probeNow(); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  window.addEventListener("focus", wake);
  window.addEventListener("online", wake);
}

// Something failed in a way that may mean LanEx is gone: a fetch refused
// (TypeError), a request timed out, the live stream dropped. Starts probing;
// shows nothing yet.
export function noteTrouble(kind = "refused") {
  if (!hasDom || S.key) return;
  if (S.down) return;
  S.down = true;
  S.since = Date.now();
  S.kind = kind === "timeout" ? "refused" : kind;   // one slow request isn't "not answering": the probe says
  S.tries = 0;
  S.timeouts = 0;
  S.foreignN = 0;
  S.lastTick = Date.now();
  schedule();
  S.tick = setInterval(onTick, 1000);
  render();
}

// Any good answer from LanEx (a request, a stream message): check at once.
export function noteOk() { if (S.down) probeNow(); }

export function isDown() { return S.down || !!S.key; }

export function keyLost(message) {
  if (!hasDom) return;
  S.key = message || "This window isn't signed in to LanEx any more (its key was replaced). Close this window and open LanEx again.";
  stop();
  render();
}

export function probeNow() {
  if (!S.down) return;
  clearTimeout(S.timer);
  S.timer = 0;
  probe({});
}

function schedule() {
  clearTimeout(S.timer);
  const d = nextDelay(S.tries, Date.now() - S.since);
  S.timer = setTimeout(() => { S.timer = 0; probe({}); }, Math.round(d * (0.9 + Math.random() * 0.2)));
}

async function probe({ quiet = false }) {
  let res = null, err = null;
  try {
    const sig = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? AbortSignal.timeout(2500) : undefined;
    const r = await fetch("/api/health", { cache: "no-store", signal: sig, headers: { "X-Requested-With": "XMLHttpRequest" } });
    let body = null;
    try { body = await r.json(); } catch (_e) { body = null; }
    res = { ok: r.ok, status: r.status, body: body && body.data !== undefined && body.data && body.data.service ? body.data : body };
  } catch (e) { err = e; }
  const k = probeKind(res, err);
  if (k === "ok") {
    const b = res.body;
    if (S.launcher == null) S.launcher = !!b.instanceId;
    if (quiet) { S.boot = S.boot || b.boot || null; return; }
    back(b.boot || null);
    return;
  }
  if (quiet) return;
  S.tries += 1;
  if (k === "timeout") S.timeouts += 1; else S.timeouts = 0;
  if (k === "foreign") S.foreignN += 1; else S.foreignN = 0;
  S.kind = S.foreignN >= 2 ? "foreign" : S.timeouts >= 3 ? "timeout" : "refused";
  render();
  if (S.down) schedule();
}

function back(boot) {
  const restarted = !!(S.boot && boot && boot !== S.boot);
  if (boot) S.boot = boot;
  const wasShown = S.shownCard;
  const wasDown = S.down;
  stop();
  render();
  if (wasDown && S.onBack) {
    try { S.onBack({ restarted, wasShown }); } catch (_e) { /* the page carries on */ }
  }
}

function stop() {
  S.down = false;
  clearTimeout(S.timer);
  S.timer = 0;
  clearInterval(S.tick);
  S.tick = 0;
}

function onTick() {
  const now = Date.now();
  // A timer that fired late by far: the PC slept (or the window was frozen).
  if (now - S.lastTick > 5000) { S.wokeAt = now; probeNow(); }
  S.lastTick = now;
  render();
}

// Reloading while LanEx is away lands on the browser's own error page, which
// doesn't come back by itself. Say so instead (the card flashes).
function onKey(e) {
  if (!S.down) return;
  const reload = e.key === "F5" || ((e.ctrlKey || e.metaKey) && (e.key === "r" || e.key === "R"));
  if (!reload) return;
  e.preventDefault();
  S.flash = Date.now();
  render();
}

// ------------------------------------------------------------------ painting
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function view() {
  return connView({ down: S.down, since: S.since, now: Date.now(), kind: S.kind, launcher: S.launcher,
    windows: isWindows(), wokeAt: S.wokeAt, key: S.key, every: nextDelay(S.tries, Date.now() - S.since) });
}

export function cardHtml(c, { elapsed = "", flash = false } = {}) {
  const foot = c.foot === "log"
    ? "<div class='ub-foot conn-foot'><span class='an-eyebrow'>Log</span><code class='conn-log'>" + esc(LOG_PATH) + "</code>" +
      "<button class='linklike' data-conn='copy-log' title='Paste it into File Explorer&#39;s address bar. It holds what LanEx printed before it stopped.'>Copy path</button></div>"
    : c.foot === "terminal"
      ? "<div class='ub-foot conn-foot'><span>The terminal that started LanEx has its log.</span></div>" : "";
  return "<div class='ub-top'><span class='an-eyebrow'>" + esc(c.eyebrow) + "</span>" +
      (elapsed ? "<span class='conn-age' title='Since LanEx stopped answering'>" + esc(elapsed) + "</span>" : "") + "</div>" +
    "<div class='ub-title'>" + esc(c.title) + "</div>" +
    "<p class='ub-text'>" + esc(c.text) + "</p>" +
    (flash ? "<p class='ub-text conn-flash'>Reloading now would show a browser error page. This window reconnects to LanEx by itself.</p>" : "") +
    (c.retry ? "<div class='ub-acts'><button class='btn btn-sm btn-primary' data-conn='retry'>Retry now</button>" +
      "<span class='conn-every'>" + esc(c.every || "") + "</span></div>" : "") +
    foot;
}

function render() {
  if (!hasDom || !document.body) return;
  const v = view();
  // lost: the card is up; weak: the chip; probing: a failure is being checked.
  const conn = v.card ? "lost" : v.chip ? "weak" : S.down ? "probing" : "";
  if (conn) document.body.dataset.conn = conn; else delete document.body.dataset.conn;
  paintChip(v.chip);
  paintCard(v.card, v.phase);
}

function paintChip(chip) {
  let el = document.getElementById("conn-chip");
  if (!chip) { if (el) el.hidden = true; return; }
  if (!el) {
    const bar = document.querySelector("footer.status");
    if (!bar) return;
    el = document.createElement("button");
    el.id = "conn-chip";
    el.type = "button";
    el.setAttribute("role", "status");
    el.innerHTML = "<span class='vf-mark' aria-hidden='true'></span><span class='st-conn-t'></span>";
    el.addEventListener("click", () => {
      const card = document.getElementById("conn-card");
      if (card && !card.hidden) card.querySelector("[data-conn=retry]")?.focus();
      probeNow();
    });
    bar.insertBefore(el, bar.firstChild);
  }
  el.hidden = false;
  el.className = "st-item st-conn is-" + chip.tone;
  el.querySelector(".vf-mark").className = "vf-mark is-" + chip.tone;
  el.querySelector(".st-conn-t").textContent = chip.text;
  el.title = chip.tip || "LanEx isn't connected. Click to check again now.";
}

function paintCard(card, phase) {
  let el = document.getElementById("conn-card");
  if (!card) {
    if (el && !el.hidden) el.hidden = true;
    S.shownCard = S.down ? S.shownCard : false;
    return;
  }
  if (!el) {
    el = document.createElement("aside");
    el.id = "conn-card";
    el.className = "ub conn-card";
    el.setAttribute("aria-label", "LanEx connection");
    el.addEventListener("click", onCardClick);
    document.body.appendChild(el);
  }
  const loud = card.tone === "fail";
  // A polite status while LanEx may come back; an alert once, when it hasn't.
  el.setAttribute("role", loud ? "alert" : "status");
  el.classList.toggle("is-fail", loud);
  el.classList.toggle("is-warn", !loud);
  el.dataset.phase = phase;
  const age = S.down ? agoWords(Date.now() - S.since) : "";
  const flash = !!(S.flash && Date.now() - S.flash < 6000);
  // Rebuilt only when the words change (not for the age), so a button being
  // pressed or focused stays put.
  const html = cardHtml(card, { elapsed: S.down ? " " : "", flash });
  if (el._html !== html) {
    const had = el.contains(document.activeElement) && document.activeElement.dataset.conn;
    el.innerHTML = html;
    el._html = html;
    if (had) el.querySelector("[data-conn='" + had + "']")?.focus();
  }
  const ageEl = el.querySelector(".conn-age");
  if (ageEl && ageEl.textContent !== age) ageEl.textContent = age;
  el.hidden = false;
  S.shownCard = true;
}

async function onCardClick(e) {
  const b = e.target.closest("[data-conn]");
  if (!b) return;
  if (b.dataset.conn === "retry") {
    b.disabled = true;
    b.textContent = "Checking…";
    setTimeout(() => { probeNow(); }, 0);
    setTimeout(() => { if (b.isConnected) { b.disabled = false; b.textContent = "Retry now"; } }, 600);
  } else if (b.dataset.conn === "copy-log") {
    try { await navigator.clipboard.writeText(LOG_PATH); b.textContent = "Copied"; }
    catch (_e) { b.textContent = "Select it and copy"; }
    setTimeout(() => { if (b.isConnected) b.textContent = "Copy path"; }, 1800);
  }
}
