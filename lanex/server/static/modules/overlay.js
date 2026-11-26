// overlay.js — the one behaviour for everything that opens over the page:
// dialogs, sheets, menus, popovers and the Console drawer.
//
//   const layer = openLayer(el, { kind: "menu", owner: button, onClose });
//   layer.close();            closeLayer(el);            isLayerOpen(el)
//
// Rules:
//   - Layers stack. Esc closes only the top one: one press, one layer.
//   - A press outside closes a menu, a popover or a side sheet (`outside`
//     says otherwise: a sheet holding typed work stays). A dialog closes by
//     its buttons, by Esc, or by a press on its scrim (the dialog decides).
//   - A modal layer (every dialog; a sheet that asks for it) takes focus when
//     it opens and keeps Tab inside. Closing puts focus back on what opened it.
//   - Only one menu is open at a time.
//   - Every dialog and sheet is named: role="dialog", aria-modal where it
//     traps, aria-labelledby its title (or aria-label).
//
// The layer's element is shown and hidden by its owner module (the `hidden`
// attribute or by adding and removing it); this module only keeps the order,
// the keys, the focus and the outside press.

const _stack = [];   // bottom … top: { el, kind, owner, onClose, prev, trap, modal }

const FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex='-1'])";
const LIGHT = new Set(["menu", "popover", "sheet"]);   // an outside press closes these, by default
const ITEMS = ".menu-item:not(:disabled), [role^='menuitem']:not(:disabled)";

// A menu's items: its .menu-item / menuitem elements, else its buttons.
function menuItems(el) {
  let items = Array.from(el.querySelectorAll(ITEMS));
  if (!items.length) items = Array.from(el.querySelectorAll("button:not(:disabled)"));
  return items.filter((n) => n.getClientRects().length);
}

function focusables(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE)).filter((n) => !n.disabled && n.getClientRects().length);
}

function top() { return _stack[_stack.length - 1] || null; }

export function isLayerOpen(el) { return _stack.some((l) => l.el === el); }
export function topLayer() { const t = top(); return t ? t.el : null; }
export function layerCount() { return _stack.length; }

// Open a layer. `onClose(reason)` hides the element; reason is "esc",
// "outside", "replaced" or "api".
export function openLayer(el, { kind = "menu", owner = null, onClose = null, label = "", labelledBy = "", focus = null, modal = null, outside = null } = {}) {
  if (!el) return null;
  const again = _stack.find((l) => l.el === el);
  if (again) return again.handle;
  // One menu at a time: a menu opening replaces the menus above the last
  // dialog or sheet.
  if (kind === "menu" || kind === "popover") {
    for (let i = _stack.length - 1; i >= 0; i--) {
      const l = _stack[i];
      if (l.kind !== "menu" && l.kind !== "popover") break;
      remove(l, "replaced", { refocus: false });
    }
  }
  const prev = document.activeElement && document.activeElement !== document.body ? document.activeElement : owner;
  const isModal = modal == null ? kind === "dialog" : !!modal;
  const layer = { el, kind, owner, onClose, prev, trap: isModal, modal: isModal,
    light: outside == null ? LIGHT.has(kind) : !!outside };
  layer.handle = { close: (reason = "api") => remove(layer, reason), el };
  _stack.push(layer);

  if (kind === "dialog" || kind === "sheet") {
    if (!el.getAttribute("role")) el.setAttribute("role", "dialog");
    if (layer.modal) el.setAttribute("aria-modal", "true");
    if (labelledBy) el.setAttribute("aria-labelledby", labelledBy);
    else if (label && !el.getAttribute("aria-labelledby")) el.setAttribute("aria-label", label);
  }
  if (owner && owner.hasAttribute && owner.hasAttribute("aria-expanded")) owner.setAttribute("aria-expanded", "true");

  // Focus: what the caller names, else the first field, else the first control.
  if (focus !== false && (layer.trap || kind === "menu" || focus)) {
    const target = (typeof focus === "function" ? focus() : focus) ||
      el.querySelector("[data-autofocus]") ||
      (layer.trap ? el.querySelector("input:not([type=checkbox]):not([type=radio]), select, textarea") : null) ||
      (kind === "menu" ? menuItems(el)[0] : null) ||
      focusables(el)[0];
    if (target && target.focus) { try { target.focus({ preventScroll: true }); } catch (_e) {} }
  }
  return layer.handle;
}

function remove(layer, reason, { refocus = true } = {}) {
  const i = _stack.indexOf(layer);
  if (i < 0) return;
  // Whatever a dialog or sheet opened above itself goes with it. A menu owns
  // nothing: its item may have just opened a sheet or a dialog, which stays.
  const owns = layer.kind !== "menu" && layer.kind !== "popover";
  if (owns) for (let j = _stack.length - 1; j > i; j--) remove(_stack[j], "replaced", { refocus: false });
  _stack.splice(_stack.indexOf(layer), 1);
  if (!owns && _stack.length > i) refocus = false;   // focus belongs to what it opened
  if (layer.owner && layer.owner.hasAttribute && layer.owner.hasAttribute("aria-expanded")) layer.owner.setAttribute("aria-expanded", "false");
  try { if (layer.onClose) layer.onClose(reason); } catch (e) { console.error("overlay close", e); }
  // Focus goes back to the opener, unless the press that closed it chose a
  // new place.
  if (refocus && reason !== "outside") {
    const back = layer.prev && layer.prev.isConnected ? layer.prev : layer.owner;
    if (back && back.focus && back.isConnected) { try { back.focus({ preventScroll: true }); } catch (_e) {} }
  }
}

export function closeLayer(el, reason = "api") {
  const l = _stack.find((x) => x.el === el);
  if (l) remove(l, reason);
}

export function closeTop(reason = "api") {
  const t = top();
  if (t) remove(t, reason);
  return !!t;
}

// Close every menu and popover (a page change, a run starting).
export function closeMenus() {
  for (let i = _stack.length - 1; i >= 0; i--) {
    const l = _stack[i];
    if (l.kind === "menu" || l.kind === "popover") remove(l, "api", { refocus: false });
  }
}

// ---------------------------------------------------------------- keys and presses
function onKey(e) {
  const t = top();
  if (!t) return;
  if (e.key === "Escape") {
    // A field with its own use for Esc (the editor's find box, a search with
    // text in it) may have taken it already.
    if (e.defaultPrevented) return;
    const tg = e.target;
    if (tg && tg.matches && tg.matches("input[type=search]") && tg.value) return;   // Esc clears it first
    e.preventDefault();
    e.stopPropagation();
    remove(t, "esc");
    return;
  }
  if (e.key === "Tab" && t.trap) {
    const f = focusables(t.el);
    if (!f.length) { e.preventDefault(); return; }
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (i === f.length - 1 || i < 0)) { e.preventDefault(); f[0].focus(); }
    return;
  }
  // Menus: arrows, Home, End move between items; a letter jumps to the next
  // item that starts with it.
  if (t.kind === "menu") {
    const items = menuItems(t.el);
    if (!items.length) return;
    const i = items.indexOf(document.activeElement);
    let j = -1;
    if (e.key === "ArrowDown") j = (i + 1) % items.length;
    else if (e.key === "ArrowUp") j = (i - 1 + items.length) % items.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = items.length - 1;
    else if (e.key.length === 1 && /\S/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey &&
      !/input|textarea|select/i.test((e.target && e.target.tagName) || "")) {
      const k = e.key.toLowerCase();
      for (let n = 1; n <= items.length; n++) {
        const c = items[(Math.max(i, -1) + n) % items.length];
        if ((c.textContent || "").trim().toLowerCase().startsWith(k)) { j = items.indexOf(c); break; }
      }
      if (j < 0) return;
    } else return;
    e.preventDefault();
    items[j].focus();
  }
}

function onPress(e) {
  const tgt = e.target;
  // Walk down from the top: a press inside a layer (or on its opener) is that
  // layer's business; a press outside closes it if it is a light one, then
  // the same question goes to the layer below.
  for (let i = _stack.length - 1; i >= 0; i--) {
    const l = _stack[i];
    if (!l) continue;
    if (l.el.contains(tgt) || (l.owner && l.owner.contains && l.owner.contains(tgt))) return;
    // Toasts and tooltips float above everything and belong to no layer.
    if (tgt.closest && tgt.closest(".toast, .tip, .driver-popover")) return;
    if (!l.light) return;
    remove(l, "outside");
  }
}

let _wired = false;
export function setupOverlays() {
  if (_wired) return;
  _wired = true;
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("pointerdown", onPress, true);
}
// The stack works from the first import: nothing to remember to call.
if (typeof document !== "undefined") setupOverlays();

// ---------------------------------------------------------------- a menu on a button
// The usual case: a button that opens a .menu beside it.
//   wireMenu(button, menu, { onOpen, onPick })
// `onOpen(menu)` may fill it first (it may return a promise); `onPick(item)`
// runs after a press on an item has closed the menu. A press on an item
// closes the menu, unless the item keeps it open (data-keep, a tick box, a field).
export function wireMenu(btn, menu, { onOpen = null, onPick = null } = {}) {
  if (!btn || !menu || btn._menuWired) return;
  btn._menuWired = true;
  if (!btn.hasAttribute("aria-haspopup")) btn.setAttribute("aria-haspopup", "menu");
  btn.setAttribute("aria-expanded", "false");
  const hide = () => { menu.hidden = true; };
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (isLayerOpen(menu)) { closeLayer(menu); return; }
    menu.hidden = false;
    if (onOpen) { const r = onOpen(menu); if (r && typeof r.then === "function") await r; }
    if (menu.hidden) return;   // closed while it filled
    openLayer(menu, { kind: "menu", owner: btn, onClose: hide });
  });
  menu.addEventListener("click", (e) => {
    const item = e.target.closest(".menu-item, [role^='menuitem']");
    if (!item || item.disabled) return;
    if (item.matches("[data-keep], .menu-check") || item.closest("[data-keep]")) return;
    closeLayer(menu, "api");
    if (onPick) onPick(item);
  });
}
