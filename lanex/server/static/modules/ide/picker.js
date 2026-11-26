// picker.js — a quick pick: a text field over a filtered list, like VS
// Code's Ctrl+P or the definitions menu of F12. Drawn with the command
// palette's classes (.cmdk-*), so it looks like the palette, and opened on
// the overlay stack (focus kept inside, Esc closes, focus goes back).
//
//   openPicker({
//     label: "Go to file",                 accessible name
//     placeholder: "Type a file name…",
//     query: "",                           text the field starts with
//     items: async (q) => ({ rows, note }), rows: [{ html, detail, tag, tagCls, key }]
//     onPick: (row, q, { keep }) => …,     Enter / click
//     hint: "html for the foot",
//     onQuery: (q) => …,                   every keystroke (e.g. a prefix changed)
//   }) -> { close, refresh, input }
import { openLayer, closeLayer } from "../overlay.js";

let _el = null;
let _cur = null;

function build() {
  _el = document.createElement("div");
  _el.className = "cmdk qp";
  _el.hidden = true;
  _el.innerHTML =
    "<div class='cmdk-backdrop'></div>" +
    "<div class='cmdk-panel qp-panel' role='dialog' aria-modal='true'>" +
    "<input class='cmdk-input qp-input' type='text' role='combobox' aria-expanded='true' aria-controls='qp-list' " +
      "aria-autocomplete='list' autocomplete='off' spellcheck='false'/>" +
    "<div class='qp-note' aria-live='polite' hidden></div>" +
    "<div class='cmdk-list qp-list' id='qp-list' role='listbox'></div>" +
    "<div class='cmdk-foot qp-foot'></div></div>";
  document.body.appendChild(_el);
  const input = _el.querySelector(".qp-input");
  _el.querySelector(".cmdk-backdrop").addEventListener("click", () => close());
  input.addEventListener("input", () => { if (_cur) { _cur.sel = 0; if (_cur.opts.onQuery) _cur.opts.onQuery(input.value); refreshSoon(); } });
  input.addEventListener("keydown", onKey);
  _el.querySelector(".qp-list").addEventListener("click", (e) => {
    const it = e.target.closest(".cmdk-item");
    if (it && _cur) { _cur.sel = +it.dataset.i; pick(); }
  });
  _el.querySelector(".qp-list").addEventListener("mousemove", (e) => {
    const it = e.target.closest(".cmdk-item");
    if (it && _cur && _cur.sel !== +it.dataset.i) { _cur.sel = +it.dataset.i; paintSel(); }
  });
  _el.querySelector(".qp-foot").addEventListener("click", (e) => {
    const b = e.target.closest("[data-qp-pfx]");
    if (!b || !_cur) return;
    const input2 = _el.querySelector(".qp-input");
    input2.value = b.dataset.qpPfx + input2.value.replace(/^[@#:]/, "");
    input2.dispatchEvent(new Event("input"));
    input2.focus();
  });
}

let _t = 0;
function refreshSoon() {
  clearTimeout(_t);
  _t = setTimeout(refresh, 30);
}

async function refresh() {
  if (!_cur) return;
  const me = _cur;
  const q = _el.querySelector(".qp-input").value;
  const seq = ++me.seq;
  let res;
  try { res = await me.opts.items(q); } catch (ex) { res = { rows: [], note: "Couldn't list: " + (ex.message || ex) }; }
  if (_cur !== me || seq !== me.seq) return;   // a newer keystroke won
  me.rows = (res && res.rows) || [];
  if (me.sel >= me.rows.length) me.sel = Math.max(0, me.rows.length - 1);
  const list = _el.querySelector(".qp-list");
  const note = _el.querySelector(".qp-note");
  note.hidden = !(res && res.note);
  note.innerHTML = (res && res.note) || "";
  list.innerHTML = me.rows.length ? me.rows.map((r, i) =>
    "<div class='cmdk-item qp-item" + (i === me.sel ? " cmdk-item-sel" : "") + (r.group ? " qp-first" : "") + "' role='option' id='qp-o" + i +
      "' aria-selected='" + (i === me.sel) + "' data-i='" + i + "'>" +
      (r.group ? "<span class='qp-group'>" + r.group + "</span>" : "") +
      "<span class='qp-main'><span class='qp-label'>" + r.html + "</span>" +
      (r.detail ? "<span class='qp-detail'>" + r.detail + "</span>" : "") + "</span>" +
      (r.tag ? "<span class='qp-tag" + (r.tagCls ? " " + r.tagCls : "") + "'>" + r.tag + "</span>" : "") + "</div>").join("")
    : (res && res.empty ? "<div class='cmdk-empty qp-empty'>" + res.empty + "</div>" : "");
  paintSel();
}

function paintSel() {
  if (!_cur) return;
  const items = _el.querySelectorAll(".qp-item");
  items.forEach((it, i) => {
    const on = i === _cur.sel;
    it.classList.toggle("cmdk-item-sel", on);
    it.setAttribute("aria-selected", on ? "true" : "false");
  });
  const input = _el.querySelector(".qp-input");
  const on = items[_cur.sel];
  if (on) { input.setAttribute("aria-activedescendant", on.id); on.scrollIntoView({ block: "nearest" }); }
  else input.removeAttribute("aria-activedescendant");
}

function onKey(e) {
  if (!_cur) return;
  const n = _cur.rows.length;
  if (e.key === "ArrowDown") { e.preventDefault(); if (n) { _cur.sel = (_cur.sel + 1) % n; paintSel(); } }
  else if (e.key === "ArrowUp") { e.preventDefault(); if (n) { _cur.sel = (_cur.sel - 1 + n) % n; paintSel(); } }
  else if (e.key === "PageDown") { e.preventDefault(); if (n) { _cur.sel = Math.min(n - 1, _cur.sel + 10); paintSel(); } }
  else if (e.key === "PageUp") { e.preventDefault(); if (n) { _cur.sel = Math.max(0, _cur.sel - 10); paintSel(); } }
  else if (e.key === "Enter") { e.preventDefault(); pick(); }
  else if (e.key === "Backspace" && e.target.value.length === 1 && /^[@#:]$/.test(e.target.value)) {
    // Backspace on a bare prefix goes back to files
  }
}

async function pick() {
  if (!_cur) return;
  const row = _cur.rows[_cur.sel];
  const q = _el.querySelector(".qp-input").value;
  const opts = _cur.opts;
  if (!row && !opts.onEnterEmpty) return;
  close();
  if (row) await opts.onPick(row, q);
  else await opts.onEnterEmpty(q);
}

export function close() {
  if (_el && !_el.hidden) closeLayer(_el, "api");
}

export function isPickerOpen() { return !!(_el && !_el.hidden); }

export function openPicker(opts) {
  if (!_el) build();
  if (!_el.hidden) closeLayer(_el, "replaced");
  _cur = { opts, rows: [], sel: 0, seq: 0 };
  const input = _el.querySelector(".qp-input");
  const panel = _el.querySelector(".qp-panel");
  panel.setAttribute("aria-label", opts.label || "Pick");
  input.setAttribute("aria-label", opts.label || "Pick");
  input.placeholder = opts.placeholder || "";
  input.value = opts.query || "";
  _el.querySelector(".qp-foot").innerHTML = opts.hint || "";
  _el.hidden = false;
  openLayer(_el, { kind: "dialog", label: opts.label || "Pick", onClose: () => { _el.hidden = true; _cur = null; }, focus: () => input });
  // the caret after the prefix, the text selected for a quick retype
  if (input.value && opts.selectText !== false) input.setSelectionRange(/^[@#:]/.test(input.value) ? 1 : 0, input.value.length);
  refresh();
  return { close, refresh, input };
}
