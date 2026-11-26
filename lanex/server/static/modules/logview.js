// logview.js — a log viewer that stays fast on a 200 000-line run log.
//
// The lines live in one store: the text in an array, the level and line
// number in typed arrays (no object per line). Only the rows in view are in
// the DOM. Wrap off: fixed 20 px rows. Wrap on: each line's row count is
// worked out from a measured character width (prefix sums, binary search),
// so the scroll stays exact without measuring 200 000 rows.
//
//   - Level chips (errors, warnings, the rest) filter the lines, DevTools
//     style; the next-error / next-warning buttons jump.
//   - Find highlights and steps through matches ("3 of 128", Enter /
//     Shift+Enter, F3), and "Only matching lines" turns it into a filter.
//     Typing more letters searches only the previous matches; a new search
//     runs in slices of the frame budget, so a keystroke never freezes the page.
//   - A live log keeps the last 200 000 lines (the oldest fifth goes at once);
//     "Load the full log" and Download read the file the server keeps.
//   - Follow sticks to the newest line; scrolling up stops it, a
//     "new lines" pill brings it back.
// Rows are text only (textContent), never HTML from the log; the page may add
// heading rows of its own (where each step starts).
//
// Reusable: `new LogView(host, opts)` with `setText`, `setRows`, `append`,
// `reveal`, `next`, `firstOf`, `end`, `setRaw`, `setSource({download, loadFull})`;
// `LogStore` and `lineKind` work without a DOM.

import { icon } from "./icons.js";
import { localeNum } from "./intlcache.js";

const ROW = 20;          // CSS px per row (logview.css .lv-row)
const OVER = 30;         // extra rows rendered above and below the view
export const MAX_LINES = 200000;
const SLICE_MS = 8;

const K_INFO = 0, K_WARN = 1, K_ERR = 2, K_HEAD = 3;
const KIND_NAME = ["", "warn", "err", "head"];
const KIND_OF = { "": K_INFO, warn: K_WARN, err: K_ERR, head: K_HEAD };

function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }

// The level of one line, the way the tools print it.
export function lineKind(s) {
  // LibreLane's console rules a line with the step's name as each one starts.
  if (/^\s*─{4,}.*\S.*─{4,}\s*$/.test(s)) return "head";
  // "[ERROR GPL-0301] …" (OpenROAD), "[15:43:11] ERROR    …" (LibreLane's console), "Error: …" (Tcl),
  // "%Error: …" (Verilator), a Python traceback.
  if (/\[ERROR\b|^ERROR\b|\]\s+ERROR\s|\bERROR:|^Error:|^%Error\b|\[CRITICAL\]|\]\s+CRITICAL\s|Traceback \(most recent/.test(s)) return "err";
  if (/\[WARNING\b|^WARNING\b|\]\s+WARNING\s|\bWARNING:|^Warning:|^%Warning\b|\[WARN\]/.test(s)) return "warn";
  return "";
}

// ---------------------------------------------------------------- the store

export class LogStore {
  constructor(cap = MAX_LINES) {
    this.cap = cap;
    this.clear();
  }

  clear() {
    this.text = [];
    this.kind = new Uint8Array(1024);
    this.num = new Int32Array(1024);
    this.n = 0;
    this.lastNum = 0;
    this.dropped = 0;
    this.ne = 0;
    this.nw = 0;
  }

  get length() { return this.n; }

  _grow(need) {
    if (need <= this.kind.length) return;
    let cap = this.kind.length;
    while (cap < need) cap *= 2;
    const k = new Uint8Array(cap); k.set(this.kind.subarray(0, this.n)); this.kind = k;
    const m = new Int32Array(cap); m.set(this.num.subarray(0, this.n)); this.num = m;
  }

  // One line; `k` is "head" for a heading row the page adds, else found here.
  push(t, k) {
    let kind = KIND_OF[k == null ? lineKind(t) : k] || K_INFO;
    // Rich wraps a console record at 80 columns: a line indented 20 spaces
    // belongs to the record above and takes its level.
    if (kind === K_INFO && this.n && /^ {20}\S/.test(t)) {
      const prev = this.kind[this.n - 1];
      if (prev === K_WARN || prev === K_ERR) kind = prev;
    }
    this._grow(this.n + 1);
    this.text.push(t);
    this.kind[this.n] = kind;
    this.num[this.n] = kind === K_HEAD ? 0 : ++this.lastNum;
    if (kind === K_ERR) this.ne++; else if (kind === K_WARN) this.nw++;
    this.n++;
  }

  // Keep the newest lines: the oldest fifth goes at once (not one per line).
  trim() {
    if (this.n <= this.cap) return 0;
    const drop = this.n - Math.floor(this.cap * 0.8);
    for (let i = 0; i < drop; i++) { const k = this.kind[i]; if (k === K_ERR) this.ne--; else if (k === K_WARN) this.nw--; }
    this.text.splice(0, drop);
    this.kind.copyWithin(0, drop, this.n);
    this.num.copyWithin(0, drop, this.n);
    this.n -= drop;
    this.dropped += drop;
    return drop;
  }

  kindOf(i) { return KIND_NAME[this.kind[i]]; }
}

// ---------------------------------------------------------------- the view

export class LogView {
  // host: an element to fill. opts: { follow, rawHref, emptyText, cap }
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;
    this.store = new LogStore(opts.cap || MAX_LINES);
    this.view = null;          // Uint32Array of line indexes, or null = every line
    this.levels = { err: true, warn: true, info: true };
    this.q = "";               // find text
    this.qCase = false;
    this.onlyMatches = false;
    this.matches = null;       // Uint32Array of line indexes that match
    this.mi = -1;              // the match shown
    this.follow = !!opts.follow;
    this.wrap = false;
    this.hit = -1;
    this._raf = 0;
    this._job = 0;
    this._newLines = 0;
    this._source = {};
    host.classList.add("lv");
    host.innerHTML =
      "<div class='lv-bar' role='toolbar' aria-label='Log tools'>" +
        "<label class='lv-q'>" + icon("search", { size: 13 }) +
          "<input type='search' placeholder='Find in log' aria-label='Find in log' spellcheck='false'/>" +
          "<span class='lv-mc' aria-live='polite'></span>" +
          "<button class='lv-qb' data-lv='prev' title='Previous match (Shift+Enter)' aria-label='Previous match'>˄</button>" +
          "<button class='lv-qb' data-lv='nextm' title='Next match (Enter)' aria-label='Next match'>˅</button>" +
          "<button class='lv-qb lv-aa' data-lv='case' aria-pressed='false' title='Match case'>Aa</button></label>" +
        "<span class='lv-levels' role='group' aria-label='Show lines by level'>" +
          "<button class='lv-chip is-err' data-lv='lvl-err' aria-pressed='true' title='Show error lines. Alt+click: next error'></button>" +
          "<button class='lv-chip is-warn' data-lv='lvl-warn' aria-pressed='true' title='Show warning lines. Alt+click: next warning'></button>" +
          "<button class='lv-chip is-info' data-lv='lvl-info' aria-pressed='true' title='Show every other line'>Info</button>" +
        "</span>" +
        "<span class='lv-jump'>" +
          "<button class='btn btn-ghost btn-xs' data-lv='err' hidden title='Next error (F8)'>Next error</button>" +
          "<button class='btn btn-ghost btn-xs' data-lv='warn' hidden title='Next warning'>Next warning</button></span>" +
        "<label class='lv-tog' title='Keep only the lines that match the search'><input type='checkbox' data-lv='only'/> Only matches</label>" +
        "<label class='lv-tog' title='Wrap long lines'><input type='checkbox' data-lv='wrap'/> Wrap</label>" +
        "<label class='lv-tog lv-follow' title='Stay at the newest line'><input type='checkbox' data-lv='follow'/> Follow</label>" +
        "<span class='lv-count'></span>" +
        "<span class='lv-acts'>" +
          "<button class='btn btn-ghost btn-xs' data-lv='copy' title='Copy the lines shown'>" + icon("file", { size: 12 }) + " Copy</button>" +
          "<a class='btn btn-ghost btn-xs' data-lv='dl' hidden download>" + icon("download", { size: 12 }) + " Download</a>" +
          "<a class='btn btn-ghost btn-xs' data-lv='raw' target='_blank' rel='noopener' hidden>" + icon("popout", { size: 12 }) + " Raw log</a>" +
        "</span>" +
      "</div>" +
      "<div class='lv-scroll' tabindex='0' role='log' aria-live='off' aria-label='Log'><div class='lv-spacer'><div class='lv-rows'></div></div>" +
        "<div class='lv-empty' hidden></div></div>" +
      "<button class='lv-newpill' hidden>↓ New lines</button>" +
      "<div class='lv-foot' hidden><span class='lv-foot-t'></span>" +
        "<button class='btn btn-ghost btn-xs' data-lv='full' hidden>Load the full log</button></div>";
    this.$ = (s) => host.querySelector(s);
    this.scroll = this.$(".lv-scroll");
    this.rows = this.$(".lv-rows");
    this.spacer = this.$(".lv-spacer");
    this.$("[data-lv=follow]").checked = this.follow;
    const qi = this.$("input[type=search]");
    qi.addEventListener("input", (e) => { this._find(e.target.value); });
    qi.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.step(e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape" && qi.value) { e.stopPropagation(); qi.value = ""; this._find(""); }
    });
    this.$("[data-lv=prev]").addEventListener("click", () => this.step(-1));
    this.$("[data-lv=nextm]").addEventListener("click", () => this.step(1));
    this.$("[data-lv=case]").addEventListener("click", (e) => {
      this.qCase = !this.qCase; e.currentTarget.setAttribute("aria-pressed", String(this.qCase));
      this._find(this.q, { force: true });
    });
    for (const lv of ["err", "warn", "info"]) {
      this.$("[data-lv=lvl-" + lv + "]").addEventListener("click", (e) => {
        if (e.altKey || e.ctrlKey || e.metaKey) { if (lv !== "info") this.next(lv); return; }
        this.levels[lv] = !this.levels[lv];
        e.currentTarget.setAttribute("aria-pressed", String(this.levels[lv]));
        this._refilter();
      });
    }
    this.$("[data-lv=only]").addEventListener("change", (e) => { this.onlyMatches = e.target.checked; this._refilter(); });
    this.$("[data-lv=wrap]").addEventListener("change", (e) => { this.wrap = e.target.checked; host.classList.toggle("is-wrap", this.wrap); this._layout(); });
    this.$("[data-lv=follow]").addEventListener("change", (e) => { this.follow = e.target.checked; if (this.follow) this._toEnd(); });
    this.$("[data-lv=err]").addEventListener("click", () => this.next("err"));
    this.$("[data-lv=warn]").addEventListener("click", () => this.next("warn"));
    this.$("[data-lv=copy]").addEventListener("click", (e) => this._copy(e.currentTarget));
    this.$("[data-lv=full]").addEventListener("click", () => this._loadFull());
    this.$(".lv-newpill").addEventListener("click", () => this.setFollow(true));
    host.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "f" || e.key === "F") && host.contains(document.activeElement)) {
        // The browser's find can't see rows that aren't drawn: find in the log instead.
        e.preventDefault(); qi.focus(); qi.select();
      } else if (e.key === "F3") { e.preventDefault(); this.step(e.shiftKey ? -1 : 1); }
      else if (e.key === "F8") { e.preventDefault(); this.next(e.shiftKey ? "warn" : "err"); }
      else if (e.key === "End" && e.target === this.scroll) { this.setFollow(true); }
    });
    this.scroll.addEventListener("scroll", () => {
      // Scrolling up by hand stops Follow, as in every terminal. (Not while new
      // lines wait for the next frame to be scrolled to: the gap is ours.)
      if (this.follow && !this._wantEnd && this.scroll.scrollTop + this.scroll.clientHeight < this.scroll.scrollHeight - ROW * 2) {
        this.follow = false; this.$("[data-lv=follow]").checked = false;
      }
      if (this.follow) this._pill(0);
      this._paint();
    }, { passive: true });
    if (typeof ResizeObserver !== "undefined") {
      let w = 0, t = 0;
      new ResizeObserver(() => {
        const cw = this.scroll.clientWidth;
        if (this.wrap && cw !== w) { w = cw; clearTimeout(t); t = setTimeout(() => this._layout(), 100); }
        else this._paint();
      }).observe(this.scroll);
    }
    this.setRaw(opts.rawHref || "");
  }

  // ---------------------------------------------------------------- API (flow.js, console.js)
  setRaw(href) {
    const a = this.$("[data-lv=raw]");
    a.hidden = !href;
    if (href) a.href = href; else a.removeAttribute("href");
  }

  // Where the whole log is: {download: url, loadFull: async () => text}.
  setSource(src = {}) {
    this._source = src || {};
    const dl = this.$("[data-lv=dl]");
    dl.hidden = !this._source.download;
    if (this._source.download) dl.href = this._source.download; else dl.removeAttribute("href");
    this._counts();
  }

  setFollowable(on) { this.$(".lv-follow").hidden = !on; if (!on) this.setFollow(false); }

  setFollow(on) {
    this.follow = !!on;
    this.$("[data-lv=follow]").checked = this.follow;
    if (this.follow) { this._pill(0); this._toEnd(); }
  }

  // Replace everything (a finished step's log).
  setText(text, opts = {}) {
    const rows = String(text || "").replace(/\r/g, "").split("\n");
    if (rows.length && rows[rows.length - 1] === "") rows.pop();
    this.setRows(rows, opts);
  }

  // Replace everything with rows: a line of text, or { t, k: "head" } for a
  // heading the page adds (where a step starts in the run's output).
  setRows(rows, { empty = "", dropped = 0 } = {}) {
    this.store.clear();
    for (const r of rows) {
      if (typeof r === "string") this.store.push(r);
      else this.store.push(r.t, r.k || null);
    }
    this.store.trim();
    this.store.dropped += dropped;
    this.hit = -1;
    this._empty = empty;
    this._pill(0);
    this._refilter({ keepTop: false });
    if (this.q) this._find(this.q, { force: true, quiet: true });
  }

  // Add lines while a run writes them.
  append(texts) {
    if (!texts || !texts.length) return;
    const s = this.store;
    const start = s.n;
    for (const t of texts) {
      if (typeof t === "string" && t.indexOf("\n") >= 0) { for (const part of t.replace(/\r/g, "").split("\n")) s.push(part); }
      else s.push(t);
    }
    const dropped = s.trim();
    if (dropped) {
      // The oldest fifth went: indexes moved, so the view and matches are rebuilt.
      this.hit = Math.max(-1, this.hit - dropped);
      this._refilter();
      if (this.q) this._find(this.q, { force: true, quiet: true });
      return;
    }
    const from = start;
    if (this.view) {
      const add = [];
      for (let i = from; i < s.n; i++) if (this._keep(i)) add.push(i);
      if (add.length) {
        const v = new Uint32Array(this.view.length + add.length);
        v.set(this.view); v.set(add, this.view.length);
        this.view = v;
      }
    }
    if (this.q && this.matches) {
      const add = [];
      for (let i = from; i < s.n; i++) if (this._match(s.text[i])) add.push(i);
      if (add.length) {
        const m = new Uint32Array(this.matches.length + add.length);
        m.set(this.matches); m.set(add, this.matches.length);
        this.matches = m;
      }
    }
    this._counts();
    this._layout({ keep: true });
    if (this.follow) this._toEnd(); else { this._pill(this._newLines + (s.n - from)); this._paint(); }
  }

  // Scroll so line `i` (0-based, into all lines) sits a third down, and mark it.
  reveal(i) {
    if (i == null || i < 0 || i >= this.store.n) return;
    if (this._pos(i) < 0) {
      // Filtered out: show every line again.
      this.levels = { err: true, warn: true, info: true };
      for (const lv of ["err", "warn", "info"]) this.$("[data-lv=lvl-" + lv + "]").setAttribute("aria-pressed", "true");
      this.onlyMatches = false; this.$("[data-lv=only]").checked = false;
      this._refilter();
    }
    this.hit = i;
    this._ver = (this._ver || 0) + 1;
    this.follow = false; this.$("[data-lv=follow]").checked = false;
    const pos = this._pos(i);
    let tries = 0;
    const place = () => {
      const h = this.scroll.clientHeight;
      if (!h && tries++ < 30) { requestAnimationFrame(place); return; }
      this.scroll.scrollTop = Math.max(0, this._top(pos) - (h || 300) / 3);
      this._paint();
    };
    place();
  }

  firstOf(kind) {
    const k = KIND_OF[kind];
    const s = this.store;
    for (let i = 0; i < s.n; i++) if (s.kind[i] === k) return i;
    return -1;
  }

  // Show the newest lines (a finished run's output ends with how it went).
  end() { this.scroll.scrollTop = this.scroll.scrollHeight; this._paint(); }

  // The next line of a level after the one marked (wrapping round).
  next(kind) {
    const k = KIND_OF[kind];
    const s = this.store;
    if (!s.n) return;
    for (let d = 1; d <= s.n; d++) {
      const i = (Math.max(-1, this.hit) + d) % s.n;
      if (s.kind[i] === k) { this.reveal(i); return; }
    }
  }

  // The next / previous match of the search.
  step(dir) {
    const m = this.matches;
    if (!m || !m.length) return;
    if (this.mi < 0) {
      // The first match at or after the line in view.
      const at = this.hit >= 0 ? this.hit : this._lineAt(this.scroll.scrollTop);
      let k = lowerBound(m, at);
      if (dir < 0) k -= 1;
      this.mi = ((k % m.length) + m.length) % m.length;
    } else this.mi = (this.mi + dir + m.length) % m.length;
    this.reveal(m[this.mi]);
    this._counts();
  }

  // ---------------------------------------------------------------- filter + find
  _match(t) {
    if (!this.q) return true;
    return this.qCase ? t.includes(this.q) : t.toLowerCase().includes(this._ql);
  }

  _keep(i) {
    const k = this.store.kind[i];
    if (k === K_ERR && !this.levels.err) return false;
    if (k === K_WARN && !this.levels.warn) return false;
    if ((k === K_INFO) && !this.levels.info) return false;
    if (this.onlyMatches && this.q && k !== K_HEAD && !this._match(this.store.text[i])) return false;
    return true;
  }

  _filtering() {
    return !this.levels.err || !this.levels.warn || !this.levels.info || (this.onlyMatches && !!this.q);
  }

  // Rebuild the view in frame-sized slices (a 200 000-line log never freezes the page).
  _refilter({ keepTop = true } = {}) {
    const job = ++this._job;
    this._ver = (this._ver || 0) + 1;
    if (!this._filtering()) {
      this.view = null;
      this._after(keepTop);
      return;
    }
    const n = this.store.n;
    const out = new Uint32Array(n);
    let len = 0, i = 0;
    const slice = () => {
      if (job !== this._job) return;
      const t0 = performance.now();
      while (i < n) {
        if (this._keep(i)) out[len++] = i;
        i++;
        if ((i & 2047) === 0 && performance.now() - t0 > SLICE_MS) break;
      }
      if (i < n) { this._counts(i / n); setTimeout(slice, 0); return; }
      this.view = out.slice(0, len);
      this._after(keepTop);
    };
    slice();
  }

  _after(keepTop) {
    this._counts();
    this._layout();
    if (!keepTop) this.scroll.scrollTop = 0;
    if (this.follow) this._toEnd(); else this._paint();
  }

  // Find: the lines that match, in slices; a longer query scans only the last matches.
  _find(q, { force = false, quiet = false } = {}) {
    const prev = this.q, prevCase = this._qCaseUsed;
    this.q = q || "";
    this._ql = this.q.toLowerCase();
    this._qCaseUsed = this.qCase;
    this.mi = -1;
    const job = (this._fjob = (this._fjob || 0) + 1);
    if (!this.q) {
      this.matches = null;
      this._counts();
      if (this.onlyMatches) this._refilter(); else { this._ver = (this._ver || 0) + 1; this._paint(); }
      return;
    }
    const narrow = !force && this.matches && prev && this.q.startsWith(prev) && prevCase === this.qCase;
    const src = narrow ? this.matches : null;
    const n = src ? src.length : this.store.n;
    const out = new Uint32Array(n);
    let len = 0, k = 0;
    const text = this.store.text;
    const slice = () => {
      if (job !== this._fjob) return;
      const t0 = performance.now();
      while (k < n) {
        const i = src ? src[k] : k;
        if (this._match(text[i])) out[len++] = i;
        k++;
        if ((k & 4095) === 0 && performance.now() - t0 > SLICE_MS) break;
      }
      if (k < n) { setTimeout(slice, 0); return; }
      this.matches = out.slice(0, len);
      this._ver = (this._ver || 0) + 1;
      this._counts();
      if (this.onlyMatches) this._refilter();
      else if (!quiet && this.matches.length) this.step(1);
      else this._paint();
    };
    slice();
  }

  // ---------------------------------------------------------------- geometry
  _len() { return this.view ? this.view.length : this.store.n; }
  _at(p) { return this.view ? this.view[p] : p; }

  // Position in the view of line i, or -1.
  _pos(i) {
    if (!this.view) return i < this.store.n ? i : -1;
    const k = lowerBound(this.view, i);
    return k < this.view.length && this.view[k] === i ? k : -1;
  }

  // Rows each line takes when wrapping (prefix sums), from a measured character width.
  _layout({ keep = false } = {}) {
    const n = this._len();
    if (!this.wrap) {
      this._pre = null;
      this.spacer.style.height = n * ROW + "px";
      if (!keep) this._ver = (this._ver || 0) + 1;
      return;
    }
    if (!this._chW) {
      const probe = document.createElement("span");
      probe.className = "lv-t";
      probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre";
      probe.textContent = "0".repeat(100);
      this.rows.appendChild(probe);
      this._chW = probe.getBoundingClientRect().width / 100 || 7.5;
      probe.remove();
    }
    const width = Math.max(80, this.scroll.clientWidth - 90);
    const cols = Math.max(20, Math.floor(width / this._chW));
    const pre = new Uint32Array(n + 1);
    const text = this.store.text;
    for (let p = 0; p < n; p++) {
      const L = text[this._at(p)].length;
      pre[p + 1] = pre[p] + Math.max(1, Math.ceil(L / cols));
    }
    this._pre = pre;
    this.spacer.style.height = pre[n] * ROW + "px";
    this._ver = (this._ver || 0) + 1;
    this._paint();
  }

  _top(p) { return this._pre ? this._pre[p] * ROW : p * ROW; }

  _posAtY(y) {
    if (!this._pre) return Math.floor(y / ROW);
    return Math.max(0, upperBound(this._pre, Math.floor(y / ROW)) - 1);
  }

  _lineAt(y) { const p = this._posAtY(y); return this._len() ? this._at(Math.min(p, this._len() - 1)) : 0; }

  // ---------------------------------------------------------------- painting
  _counts(progress) {
    const s = this.store;
    const e = s.ne, w = s.nw;
    const eb = this.$("[data-lv=lvl-err]"), wb = this.$("[data-lv=lvl-warn]");
    setText(eb, "✗ " + localeNum(e));
    setText(wb, "▲ " + localeNum(w));
    eb.setAttribute("aria-label", e + (e === 1 ? " error line" : " error lines") + ", shown: " + this.levels.err);
    wb.setAttribute("aria-label", w + (w === 1 ? " warning line" : " warning lines") + ", shown: " + this.levels.warn);
    // Lines of this log marked WARNING (a tool can print one warning on several
    // lines, or repeat it); the run's warning count elsewhere is LibreLane's
    // warning.log, one entry per warning, so the words differ.
    wb.title = "Show warning lines (" + w + " in this log; the run's warning count lists each warning once). Alt+click: next warning";
    this.$("[data-lv=err]").hidden = !e;
    this.$("[data-lv=warn]").hidden = !w;
    const n = s.n, shown = this._len();
    const lines = (k) => localeNum(k) + (k === 1 ? " line" : " lines");
    const busy = progress != null ? " · filtering " + Math.round(progress * 100) + " %" : "";
    setText(this.$(".lv-count"), (shown === n ? lines(n) : localeNum(shown) + " of " + lines(n)) + busy);
    const mc = this.$(".lv-mc");
    const m = this.matches;
    setText(mc, !this.q ? "" : !m ? "…" : !m.length ? "no match" : (this.mi >= 0 ? (this.mi + 1) + " of " : "") + localeNum(m.length));
    this.$(".lv-q").classList.toggle("has-q", !!this.q);
    const em = this.$(".lv-empty");
    em.hidden = shown > 0;
    setText(em, n ? (this.q && this.onlyMatches ? "No line matches “" + this.q + "”." : "No line at the levels shown.") : (this._empty || "Nothing here yet."));
    const foot = this.$(".lv-foot");
    const capped = s.dropped > 0;
    foot.hidden = !capped;
    if (capped) setText(this.$(".lv-foot-t"), "Showing the last " + localeNum(n) + " of " + localeNum(n + s.dropped) + " lines.");
    this.$("[data-lv=full]").hidden = !(capped && this._source.loadFull);
  }

  _pill(n) {
    this._newLines = n;
    const p = this.$(".lv-newpill");
    p.hidden = !n;
    if (n) setText(p, "↓ " + localeNum(n) + " new " + (n === 1 ? "line" : "lines"));
  }

  _toEnd() {
    this._wantEnd = true;
    this._paint();
  }

  _row(p) {
    const i = this._at(p);
    const s = this.store;
    const k = s.kind[i];
    const row = document.createElement("div");
    const mark = this.q && this.matches && this.matches.length && i === (this.mi >= 0 ? this.matches[this.mi] : -1);
    row.className = "lv-row" + (k ? " is-" + KIND_NAME[k] : "") + (i === this.hit ? " is-hit" : "") + (mark ? " is-match" : "");
    if (this._pre) { row.style.top = this._top(p) + "px"; row.style.height = (this._pre[p + 1] - this._pre[p]) * ROW + "px"; }
    const n = document.createElement("span");
    n.className = "lv-n"; n.textContent = s.num[i] ? String(s.num[i]) : "";
    if (k === K_ERR) n.dataset.g = "✗"; else if (k === K_WARN) n.dataset.g = "▲";
    const t = document.createElement("span");
    t.className = "lv-t";
    const text = s.text[i] || " ";
    if (this.q && this._match(text) && this.q.length) {
      // The matched words marked (text nodes only, never HTML from the log).
      const hay = this.qCase ? text : text.toLowerCase();
      const needle = this.qCase ? this.q : this._ql;
      let a = 0, b;
      while ((b = hay.indexOf(needle, a)) >= 0 && a < text.length) {
        if (b > a) t.appendChild(document.createTextNode(text.slice(a, b)));
        const m = document.createElement("mark"); m.textContent = text.slice(b, b + needle.length); t.appendChild(m);
        a = b + needle.length;
        if (!needle.length) break;
      }
      if (a < text.length) t.appendChild(document.createTextNode(text.slice(a)));
    } else t.textContent = text;
    row.append(n, t);
    return row;
  }

  // The rows in view (and OVER either side). Rows already drawn for the same
  // positions stay: a live run's flush adds its new lines at the bottom and
  // drops as many from the top, instead of rebuilding ~90 rows each time.
  _paint() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      if (this._wantEnd) { this._wantEnd = false; this.scroll.scrollTop = this.scroll.scrollHeight; }
      const top = this.scroll.scrollTop, h = this.scroll.clientHeight || 400;
      const len = this._len();
      const a = Math.max(0, this._posAtY(top) - OVER);
      const b = Math.min(len, this._posAtY(top + h) + 1 + OVER);
      const ver = this._ver || 0;
      const ra = this._ra, rb = this._rb;
      if (ver === this._rver && a === ra && b === rb) return;
      if (this._pre) {
        // Wrapped rows sit at their own offsets: redraw the window.
        this.rows.style.transform = "";
        const frag = document.createDocumentFragment();
        for (let p = a; p < b; p++) frag.appendChild(this._row(p));
        this.rows.replaceChildren(frag);
      } else {
        this.rows.style.transform = "translateY(" + a * ROW + "px)";
        if (ver !== this._rver || ra == null || b <= ra || a >= rb) {
          const frag = document.createDocumentFragment();
          for (let p = a; p < b; p++) frag.appendChild(this._row(p));
          this.rows.replaceChildren(frag);
        } else {
          const kids = this.rows.children;
          for (let k = ra; k < Math.min(a, rb); k++) kids[0].remove();
          for (let k = rb; k > Math.max(b, a); k--) this.rows.lastElementChild.remove();
          if (a < ra) { const f = document.createDocumentFragment(); for (let p = a; p < ra; p++) f.appendChild(this._row(p)); this.rows.prepend(f); }
          if (b > rb) { const f = document.createDocumentFragment(); for (let p = rb; p < b; p++) f.appendChild(this._row(p)); this.rows.append(f); }
        }
      }
      this._ra = a; this._rb = b; this._rver = ver;
    });
  }

  async _loadFull() {
    const btn = this.$("[data-lv=full]");
    if (!this._source.loadFull) return;
    btn.disabled = true;
    setText(this.$(".lv-foot-t"), "Reading the full log…");
    try {
      const text = await this._source.loadFull();
      const follow = this.follow;
      const big = new LogStore(Math.max(this.store.cap, 2000000));
      this.store = big;
      this.setText(text);
      if (follow) this.setFollow(true);
    } catch (e) {
      setText(this.$(".lv-foot-t"), "Couldn't read the full log: " + (e.message || e) + ".");
    } finally { btn.disabled = false; }
  }

  async _copy(btn) {
    const n = this._len();
    const out = new Array(n);
    for (let p = 0; p < n; p++) out[p] = this.store.text[this._at(p)];
    try { await navigator.clipboard.writeText(out.join("\n")); btn.lastChild.textContent = " Copied"; }
    catch (_e) { btn.lastChild.textContent = " Copy failed"; }
    setTimeout(() => { btn.lastChild.textContent = " Copy"; }, 1400);
  }
}

// First index in sorted array `a` whose value is >= x.
export function lowerBound(a, x) {
  let lo = 0, hi = a.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (a[mid] < x) lo = mid + 1; else hi = mid; }
  return lo;
}
// First index whose value is > x.
export function upperBound(a, x) {
  let lo = 0, hi = a.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (a[mid] <= x) lo = mid + 1; else hi = mid; }
  return lo;
}
