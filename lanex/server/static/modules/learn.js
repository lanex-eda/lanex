// learn.js — the help sheet's Learn tab: the page's own guide, the glossary
// (static/glossary.json, the same words the inline definitions show, term.js)
// and the error codes LanEx explains (the knowledge base, /api/kb/errors),
// each searchable.
//
// renderGlossary(body, {pageKey, term, code}): `term` opens the glossary at
// one term (from an inline definition's "More in Help"), `code` opens the
// error index at one code (from a failure card's code chip).
import { api, fmt } from "./api.js";
import { loadTerms, findTerm } from "./term.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));
let _kb = null;

async function kbIndex() {
  if (_kb) return _kb;
  try { _kb = await api.kbErrors(); } catch (_e) { _kb = null; }
  return _kb || { entries: [], seen: {} };
}

// One glossary row. Pure (tested).
export function glossRowHtml(id, e) {
  return "<div class='gloss-row' id='gloss-" + esc(id) + "' data-term-id='" + esc(id) + "'>" +
    "<span class='gloss-term'>" + esc(e.term) + (e.full ? "<small>" + esc(e.full) + "</small>" : "") + "</span>" +
    "<span class='gloss-def'>" + esc(e.short) + (e.lanex ? "<span class='gloss-lanex'>" + esc(e.lanex) + "</span>" : "") + "</span></div>";
}

// Does a glossary entry match a search? By its name, other names and words.
export function glossMatch(e, needle) {
  const n = String(needle || "").trim().toLowerCase();
  if (!n) return true;
  return [e.term, e.full, e.short, e.lanex, ...(e.aliases || [])].some((x) => String(x || "").toLowerCase().includes(n));
}

// One error-index row: the code, the title words, the stage, and how many of
// this project's runs hit it. Pure (tested).
const STAGE_WORD = { config: "Before the first step", synthesis: "Synthesis", floorplan: "Floorplan", pdn: "Power grid",
  placement: "Placement", cts: "Clock tree", routing: "Routing", signoff: "Sign-off", engine: "Container engine", any: "Any step" };
export function codeRowHtml(e, seen) {
  const codes = [e.code].concat(e.also || []).filter(Boolean);
  const runs = (seen || {})[e.id] || [];
  return "<div class='kb-row' data-kb-id='" + esc(e.id) + "' id='kb-" + esc(e.id) + "'>" +
    "<span class='kb-code'>" + (codes.length ? codes.map(esc).join("<br>") : "<span class='kb-words'>words</span>") + "</span>" +
    "<span class='kb-main'><b>" + esc(e.about) + "</b>" +
      "<small>" + esc(STAGE_WORD[e.stage] || e.stage || "") + (e.forced ? " · checked on a real run" : " · from the tool's source") +
      (runs.length ? " · in " + runs.length + " of your runs (" + runs.slice(0, 3).map(esc).join(", ") + (runs.length > 3 ? " …" : "") + ")" : "") + "</small></span></div>";
}

export function codeMatch(e, needle) {
  const n = String(needle || "").trim().toLowerCase();
  if (!n) return true;
  return [e.code, ...(e.also || []), e.about, e.id, e.stage, STAGE_WORD[e.stage]].some((x) => String(x || "").toLowerCase().includes(n));
}

export async function renderGlossary(body, { pageKey, term, code } = {}) {
  const terms = await loadTerms();
  const ids = Object.keys(terms).sort((a, b) => terms[a].term.localeCompare(terms[b].term));
  const key = pageKey || document.querySelector(".side-tab.side-tab-active")?.dataset.tab;
  const { guideHtml } = await import("./help.js");
  const guide = key && !term && !code ? guideHtml(key) : "";
  body.innerHTML = guide +
    "<div class='sheet-lane'>Glossary</div>" +
    "<div class='sheet-search'><input type='search' id='gloss-q' placeholder='Search " + ids.length +
    " terms: DRC, slack, utilization…' autocomplete='off' aria-label='Search the glossary'/></div>" +
    "<div class='gloss' id='gloss-list'></div>" +
    "<div class='sheet-lane' id='kb-lane'>Error codes LanEx explains</div>" +
    "<p class='kb-lede'>The tools' message codes, and the words with no code, that get a failure card with what happened and " +
    "what to change. Anything else gets the general card, with the tool's own words and Copy for help.</p>" +
    "<div class='sheet-search'><input type='search' id='kb-q' placeholder='Search codes and words: GPL, PDN, latch…' autocomplete='off' aria-label='Search the error codes'/></div>" +
    "<div class='kb-list' id='kb-list'><p class='muted'>Loading…</p></div>";
  const list = body.querySelector("#gloss-list");
  const paint = (q) => {
    const hits = ids.filter((id) => glossMatch(terms[id], q));
    list.innerHTML = hits.length ? hits.map((id) => glossRowHtml(id, terms[id])).join("")
      : "<p class='gloss-empty'>No term matches “" + esc(q) + "”.</p>";
  };
  const q = body.querySelector("#gloss-q");
  q.addEventListener("input", (e) => paint(e.target.value));
  const hit = term ? findTerm(terms, term) : null;
  if (hit) {
    q.value = hit.term;
    paint(hit.term);
    requestAnimationFrame(() => list.querySelector("#gloss-" + CSS.escape(hit.id))?.classList.add("is-hit"));
  } else paint("");

  // The error index: loaded after the glossary, which is what most opens want.
  const kb = await kbIndex();
  if (!body.isConnected) return;
  const kl = body.querySelector("#kb-list");
  const entries = (kb.entries || []).slice().sort((a, b) => String(a.code || "~" + a.id).localeCompare(String(b.code || "~" + b.id)));
  const paintKb = (needle) => {
    const hits = entries.filter((e) => codeMatch(e, needle));
    kl.innerHTML = hits.length ? hits.map((e) => codeRowHtml(e, kb.seen)).join("")
      : "<p class='gloss-empty'>No code matches “" + esc(needle) + "”. LanEx explains the codes listed here; anything else " +
        "gets the general card with Copy for help.</p>";
  };
  const kq = body.querySelector("#kb-q");
  kq.addEventListener("input", (e) => paintKb(e.target.value));
  if (code) {
    kq.value = code;
    paintKb(code);
    requestAnimationFrame(() => {
      body.querySelector("#kb-lane")?.scrollIntoView({ block: "start" });
      kl.querySelector(".kb-row")?.classList.add("is-hit");
      kq.focus({ preventScroll: true });
    });
  } else paintKb("");
}
