// html.js — the one HTML escaper (audit 7c), and a tagged template that uses it.
//
//   import { html, raw, escapeHtml } from "./html.js";
//   el.innerHTML = html`<b>${run.tag}</b> ${raw(iconSvg)} ${rows.map((r) => html`<li>${r}</li>`)}`;
//
// Every ${value} is escaped unless it is html`` itself or raw(…) (markup this
// code made). Arrays are joined, each item by the same rule. null and
// undefined print nothing. A leaf module: no imports, safe in node tests.
// frontend_test.mjs fails on any other module that builds its own escaper.

const ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// Text for an HTML context: element content or a quoted attribute.
export function escapeHtml(s) {
  return s == null ? "" : String(s).replace(/[&<>"']/g, (c) => ENT[c]);
}

// Text for content only (no attribute), where quotes must stay quotes: an
// ECharts rich-text label, a <pre> block.
export function escapeText(s) {
  return s == null ? "" : String(s).replace(/[&<>]/g, (c) => ENT[c]);
}

class SafeHtml {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

// Markup this code built itself, to be put in as it is.
export function raw(s) { return new SafeHtml(s == null ? "" : String(s)); }

function part(v) {
  if (v == null || v === false) return "";
  if (v instanceof SafeHtml) return v.s;
  if (Array.isArray(v)) return v.map(part).join("");
  return escapeHtml(v);
}

export function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += part(vals[i]) + strings[i + 1];
  return new SafeHtml(out);
}
