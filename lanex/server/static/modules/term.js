// term.js — the inline glossary: plain definitions on the labels that carry
// jargon, shown on hover and on keyboard focus, with a link to Help.
//
// One registry, two ways in:
//   data-term="utilization"      on any element (a page that writes its own
//                                 HTML can mark a label this way), and
//   ATTACH below                 a list of places, by selector and label
//                                 text, for pages this module doesn't own.
//                                 When a page's markup moves, only its entry
//                                 here changes.
// The words come from static/glossary.json (version 2: {terms: {id: {term,
// full, short, lanex, aliases, see}}}), the same file Help's glossary shows.
//
// Behaviour (WCAG 2.2 SC 1.4.13, content on hover or focus): the definition
// opens after a short hover or at once on focus; the pointer can move onto it;
// Esc closes it without moving focus; it stays until hover and focus leave.
// Enter or a click on a term pins it, so its links (More in Help, See also)
// can be reached. Only the first use of a term on each page is underlined;
// later ones keep the definition without the dotted line.

import { escapeHtml } from "./html.js";

const esc = escapeHtml;

// ---------------------------------------------------------------- the registry
// Each entry: `sel` (inside `in`, the page section) and either `term` (every
// match gets it) or `text` (the match's own label text, case and a trailing
// colon ignored, picks the term). Pages: Setup, Flow, Verify, Layout.
export const ATTACH = [
  // Setup: the project card, the process card, the Settings card.
  { in: "#sec-setup", sel: ".pd-field label", text: { "top module": "top-module", "clock port": "clock-port", "clock period": "clock-period" } },
  { in: "#sec-setup", sel: ".field label", text: { "process (pdk)": "pdk", "standard-cell library": "scl" } },
  { in: "#sec-setup", sel: ".ms-field .ms-top label", text: {
    "die size from": "die", "core utilization": "utilization", "die area": "die", "placement density": "placement-density",
    "synthesis goal": "synth-strategy", "defines": "define", "systemverilog frontend": "slang",
    "hold margin, placement": "hold", "hold margin, routing": "hold", "max fanout": "fanout",
    "top routing layer": "metal-layer", "antenna diodes on ports": "diode" } },
  // Flow: the stage tiles and the step groups.
  { in: "#sec-pipeline", sel: ".fl-tile-h > b, .fl-grp-h > b, .fl-grp > div > b", text: STAGE_TEXT() },
  // Verify: each check row by its id, and the corner table.
  ...Object.entries({
    antenna: "antenna", lvs: "lvs", xor: "xor", magic_drc: "drc", klayout_drc: "drc", route_drc: "detailed-routing",
    setup: "setup", hold: "hold", slew: "slew", cap: "max-cap", fanout: "fanout", latch: "latch", eqy: "eqy",
    pdn: "pdn", disc_pin: "disconnected-pin", overlap: "illegal-overlap", lint_errors: "lint", lint_warnings: "lint",
    lint_timing_constructs: "lint", wire_length: "wirelength", unmapped: "standard-cell", assign: "netlist",
    klayout_antenna: "antenna",
  }).map(([id, term]) => ({ in: "#sec-verify", sel: "#vf-row-" + id + " .vf-row-name > b", term })),
  { in: "#sec-verify", sel: "th", text: { corner: "corner" } },
  // Layout: the stage strip and the side panel's tabs.
  { in: "#sec-layout", sel: ".lx-stage-txt > b", text: { ...STAGE_TEXT(), "final gds": "gds", "gds": "gds" } },
  { in: "#sec-layout", sel: ".lx-side-tabs > button", text: { layers: "metal-layer", cells: "standard-cell", checks: "drc" }, own: true },
];

function STAGE_TEXT() {
  return { synthesis: "synthesis", floorplan: "floorplan", "power grid": "pdn", placement: "placement",
    "clock tree": "cts", routing: "routing", "sign-off": "signoff", signoff: "signoff" };
}

// ---------------------------------------------------------------- the words
let _terms = null;          // id -> entry
let _loading = null;

// The glossary in either shape: version 2, or the flat {term: text} map of
// version 1 (kept so an old file still reads).
export function normGlossary(doc) {
  if (doc && typeof doc === "object" && doc.terms && typeof doc.terms === "object") return doc.terms;
  const out = {};
  for (const [k, v] of Object.entries(doc || {})) {
    if (typeof v === "string") out[k.toLowerCase().replace(/[^a-z0-9]+/g, "-")] = { term: k, short: v };
  }
  return out;
}

// {display name: text} for the palette and anything that wants a flat list.
export function flatGlossary(doc) {
  const out = {};
  for (const e of Object.values(normGlossary(doc))) out[e.term] = e.short + (e.lanex ? " " + e.lanex : "");
  return out;
}

export function loadTerms() {
  if (_terms) return Promise.resolve(_terms);
  if (!_loading) {
    _loading = fetch("/static/glossary.json", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then((r) => r.json()).then((d) => (_terms = normGlossary(d))).catch(() => (_terms = {}));
  }
  return _loading;
}

// A term by its id or any of its names ("utilisation", "WNS").
export function findTerm(terms, key) {
  const k = String(key || "").trim().toLowerCase();
  if (!k || !terms) return null;
  if (terms[k]) return { id: k, ...terms[k] };
  for (const [id, e] of Object.entries(terms)) {
    if (String(e.term).toLowerCase() === k || (e.aliases || []).some((a) => String(a).toLowerCase() === k)) return { id, ...e };
  }
  return null;
}

const normText = (s) => String(s || "").replace(/\s+/g, " ").trim().replace(/:$/, "").toLowerCase();

// The term an element gets from one registry entry, or null. Pure (tested).
export function termFor(entry, labelText) {
  if (entry.term) return entry.term;
  return (entry.text || {})[normText(labelText)] || null;
}

// The popover's inner HTML for a term entry. Pure (tested).
export function termHtml(e, { pinned = false } = {}) {
  if (!e) return "";
  const see = (e.see || []).filter((x) => _terms && _terms[x]).slice(0, 4);
  return "<div class='term-eyebrow'>" + esc(e.term) + (e.full ? "<span class='term-full'>" + esc(e.full) + "</span>" : "") + "</div>" +
    "<p class='term-short'>" + esc(e.short) + "</p>" +
    (e.lanex ? "<p class='term-lanex'>" + esc(e.lanex) + "</p>" : "") +
    "<div class='term-foot'>" +
      (see.length ? "<span class='term-see'><span class='term-see-k'>See also</span>" +
        see.map((x) => "<button type='button' class='term-chip' data-term-see='" + esc(x) + "'>" + esc(_terms[x].term) + "</button>").join("") + "</span>" : "") +
      "<button type='button' class='term-more' data-term-more='" + esc(e.id || "") + "'" + (pinned ? "" : " tabindex='-1'") + ">More in Help ›</button>" +
    "</div>";
}

// ---------------------------------------------------------------- attaching
const FOCUSABLE = "a[href],button,input,select,textarea,[tabindex]";
// A control a term can sit inside (a Verify row's button): it takes the focus
// for the term. A page container with tabindex=-1 (for focus on navigation)
// is not one, nor a page section (a tabpanel with tabindex=0).
const CONTROL = "a[href],button,input,select,textarea,summary,[role=button],[role=tab],[tabindex]:not([tabindex='-1']):not([role=tabpanel]):not(section)";

function mark(el, id) {
  if (el.dataset.term && el.dataset.termAuto !== "1") return;     // a page's own data-term wins
  el.dataset.term = id;
  el.dataset.termAuto = "1";
  el.classList.add("has-term");
  // Reachable by keyboard: the element itself when nothing around it takes
  // focus; a term inside a button or link is read when that gets focus.
  if (!el.matches(FOCUSABLE) && !el.closest(CONTROL) && !el.hasAttribute("tabindex")) {
    el.tabIndex = 0;
    el.dataset.termTab = "1";
  }
}

// One pass over the registry inside `root` (a page section, or the document).
export function attachTerms(root = document) {
  if (!_terms || !root || !root.querySelectorAll) return 0;
  let n = 0;
  for (const a of ATTACH) {
    const scope = a.in ? (root.matches && root.matches(a.in) ? root : root.querySelector ? root.querySelector(a.in) : null) || (root === document ? document.querySelector(a.in) : null) : root;
    if (!scope) continue;
    for (const el of scope.querySelectorAll(a.sel)) {
      const id = termFor(a, a.own ? ownText(el) : el.textContent);
      if (id && _terms[id]) { mark(el, id); n++; }
    }
  }
  // Every [data-term] gets the class, wherever it came from.
  for (const el of (root.querySelectorAll ? root.querySelectorAll("[data-term]:not(.has-term)") : [])) {
    if (!_terms[el.dataset.term]) continue;
    el.classList.add("has-term");
    // Its own control (a term chip): reachable, and Enter pins it.
    if (!el.parentElement?.closest(CONTROL) && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      el.tabIndex = 0;
      el.dataset.termTab = "1";
    }
  }
  underlineFirst(root);
  return n;
}

// A button's own words, without the counters inside it ("Checks 3").
function ownText(el) {
  return Array.from(el.childNodes).filter((x) => x.nodeType === 3).map((x) => x.textContent).join(" ");
}

// The first use of each term in each page is underlined, the rest aren't.
function underlineFirst(root) {
  const pages = root === document ? document.querySelectorAll("section.section") : [root.closest ? (root.closest("section.section") || root) : root];
  for (const page of pages) {
    const seen = new Set();
    for (const el of page.querySelectorAll(".has-term")) {
      const first = !seen.has(el.dataset.term) && el.getClientRects().length > 0;
      if (first) seen.add(el.dataset.term);
      el.classList.toggle("term-u", first && underlineOn());
    }
  }
}

function underlineOn() {
  try { return localStorage.getItem("ll.termUnderline") !== "off"; } catch (_e) { return true; }
}

// ---------------------------------------------------------------- the popover
// `quiet`: the term Esc just closed; focus coming back to it doesn't reopen it.
const P = { el: null, target: null, timer: 0, hideTimer: 0, pinned: false, quiet: null };

function pop() {
  if (P.el) return P.el;
  const el = document.createElement("div");
  el.className = "term-pop";
  el.id = "term-pop";
  el.setAttribute("role", "tooltip");
  el.hidden = true;
  document.body.appendChild(el);
  el.addEventListener("pointerenter", () => clearTimeout(P.hideTimer));
  el.addEventListener("pointerleave", () => { if (!P.pinned) hideSoon(); });
  el.addEventListener("click", (e) => {
    const see = e.target.closest("[data-term-see]");
    if (see) { show(P.target, see.dataset.termSee, { pinned: true }); return; }
    const more = e.target.closest("[data-term-more]");
    if (more) { hide(); openInHelp(more.dataset.termMore); }
  });
  el.addEventListener("focusout", (e) => {
    if (P.pinned && !el.contains(e.relatedTarget) && e.relatedTarget !== P.target) hide();
  });
  P.el = el;
  return el;
}

function zoomFactor() {
  const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ll-zoom"));
  return v > 0 ? v : 1;
}

function place(target) {
  const el = P.el;
  const z = zoomFactor();
  const r = target.getBoundingClientRect();
  const pr = el.getBoundingClientRect();
  const rl = r.left / z, rt = r.top / z, rb = r.bottom / z;
  const w = pr.width / z, h = pr.height / z;
  const vw = window.innerWidth / z, vh = window.innerHeight / z;
  // Above the label, so it never covers the field being typed in; below when
  // there's no room above.
  let top = rt - h - 8;
  let below = false;
  if (top < 6) { top = rb + 8; below = true; }
  if (top + h > vh - 6) top = Math.max(6, vh - h - 6);
  const left = Math.max(8, Math.min(rl - 6, vw - w - 8));
  el.style.top = Math.round(top) + "px";
  el.style.left = Math.round(left) + "px";
  el.classList.toggle("is-below", below);
}

function show(target, id, { pinned = false } = {}) {
  const e = findTerm(_terms, id);
  if (!e || !target || !target.isConnected) return;
  const el = pop();
  clearTimeout(P.hideTimer);
  if (P.target && P.target !== target) P.target.removeAttribute("aria-describedby");
  P.target = target;
  P.pinned = pinned;
  el.innerHTML = termHtml(e, { pinned });
  el.classList.toggle("is-pinned", pinned);
  el.hidden = false;
  target.setAttribute("aria-describedby", "term-pop");
  place(target);
  if (pinned) requestAnimationFrame(() => el.querySelector(".term-chip, .term-more")?.focus({ preventScroll: true }));
}

function hide() {
  clearTimeout(P.timer);
  clearTimeout(P.hideTimer);
  if (!P.el || P.el.hidden) return;
  P.el.hidden = true;
  if (P.target) P.target.removeAttribute("aria-describedby");
  P.pinned = false;
}

function hideSoon() {
  clearTimeout(P.hideTimer);
  // A grace period, so the pointer can cross the gap onto the popover.
  P.hideTimer = setTimeout(hide, 160);
}

async function openInHelp(id) {
  const [{ openHelp }, learn] = await Promise.all([import("./shell.js"), import("./learn.js")]);
  const body = openHelp("learn", { render: false });
  await learn.renderGlossary(body, { term: id });
}

function termTarget(node) {
  return node && node.closest ? node.closest(".has-term") : null;
}

let _wired = false;
export function setupTerms() {
  if (_wired || typeof document === "undefined") return;
  _wired = true;
  loadTerms().then(() => attachTerms(document));
  document.addEventListener("pointerover", (e) => {
    const t = termTarget(e.target);
    if (!t || (P.pinned && P.target === t)) return;
    clearTimeout(P.timer);
    clearTimeout(P.hideTimer);
    P.timer = setTimeout(() => { if (!P.pinned) show(t, t.dataset.term); }, 320);
  });
  document.addEventListener("pointerout", (e) => {
    const t = termTarget(e.target);
    if (!t) return;
    clearTimeout(P.timer);
    if (!P.pinned && !(P.el && P.el.contains(e.relatedTarget))) hideSoon();
  });
  document.addEventListener("focusin", (e) => {
    if (P.el && P.el.contains(e.target)) return;
    // The term itself, or a button whose label is a term (a Verify row).
    const t = termTarget(e.target) || (e.target.querySelector && e.target.matches(CONTROL) ? e.target.querySelector(":scope .has-term") : null);
    if (t && t === P.quiet) return;
    P.quiet = null;
    if (t) show(t, t.dataset.term);
    else if (!P.pinned) hide();
  });
  document.addEventListener("focusout", (e) => {
    if (!P.pinned && termTarget(e.target)) hideSoon();
  });
  document.addEventListener("click", (e) => {
    const t = termTarget(e.target);
    // A term that is its own control (made focusable here) pins on a click;
    // inside a button or a label the click does what it always did.
    if (t && t.dataset.termTab === "1" && !e.target.closest("[data-term-see],[data-term-more]")) {
      if (t.tagName !== "LABEL") { e.preventDefault(); show(t, t.dataset.term, { pinned: true }); }
    } else if (P.pinned && P.el && !P.el.contains(e.target)) hide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && P.el && !P.el.hidden) {
      const back = P.pinned ? P.target : null;
      P.quiet = P.target;
      hide();
      back?.focus?.({ preventScroll: true });
      e.stopPropagation();
      return;
    }
    const t = termTarget(e.target);
    if (t && t.dataset.termTab === "1" && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      show(t, t.dataset.term, { pinned: true });
    }
  }, true);
  // A scroll moves the label: the definition follows it while the label is
  // focused, hovered or pinned (focusing a label scrolls it into view), and
  // goes away otherwise.
  window.addEventListener("scroll", () => {
    if (!P.el || P.el.hidden || !P.target) return;
    const a = document.activeElement;
    const keep = P.pinned || a === P.target || (a && a !== document.body && a.contains(P.target)) || P.target.matches(":hover");
    if (keep && P.target.isConnected && P.target.getClientRects().length) place(P.target); else hide();
  }, true);
  // Pages draw their HTML when shown and when data arrives: attach after the
  // changes settle (200 ms), inside the page that changed. Streams that never
  // carry labels (a live log, a canvas, a drawing) are ignored, so a running
  // flow doesn't re-scan its page on every log line.
  const work = document.querySelector("main.work") || document.body;
  const QUIET = ".fl-insp, .lv-lines, pre, svg, canvas, .lx-canvas, .cm-editor";
  let queued = new Set();
  let timer = 0;
  const flush = () => {
    timer = 0;
    const roots = queued;
    queued = new Set();
    loadTerms().then(() => { for (const r of roots) if (r.isConnected) attachTerms(r); });
  };
  new MutationObserver((muts) => {
    for (const m of muts) {
      const t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (!t || t.closest(QUIET)) continue;
      const page = t.closest("section.section");
      if (page) queued.add(page);
    }
    if (queued.size && !timer) timer = setTimeout(flush, 200);
  }).observe(work, { childList: true, subtree: true });
  document.addEventListener("g:tab_activated", () => loadTerms().then(() => attachTerms(document)));
  document.addEventListener("g:pref", (e) => { if (!e.detail || e.detail.id === "termUnderline" || e.detail.id == null) attachTerms(document); });
}
