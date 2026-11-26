// toast.js — bottom-right notifications. Class-styled (no inline cssText),
// icon per type, dismissable, error toasts are sticky + assertive.

import { icon } from "./icons.js";

const ICON = { success: "check", warn: "alert", error: "x", info: "dot" };
const MAX = 4;

export const toast = {
  container: null,

  init() {
    const c = document.createElement("div");
    c.className = "toast-host";
    c.setAttribute("role", "status");
    c.setAttribute("aria-live", "polite");
    document.body.appendChild(c);
    this.container = c;
  },

  // ``action`` adds a button (e.g. {label: "Undo", run}), or several as an
  // array; each runs and dismisses the toast. Hovering keeps it up.
  // ``opts.keep``: the toast is outside the cap and survives clear() (an Undo
  // that a burst of newer toasts must not push out). ``opts.onClose(why)``:
  // called once when it goes, why = "timeout" | "close" | "action".
  show(message, type = "info", duration, action, opts = {}) {
    if (!this.container) this.init();
    // Cap the stack — drop the oldest so a burst can't wallpaper the screen.
    // Kept toasts don't count and are never dropped.
    const loose = () => [...this.container.children].filter((c) => !c.classList.contains("toast-keep"));
    for (let l = loose(); l.length >= MAX; l = loose()) l[0].remove();
    const t = type in ICON ? type : "info";
    const el = document.createElement("div");
    el.className = "toast toast-" + t + (opts && opts.keep ? " toast-keep" : "");
    // The element itself carries role=alert for errors — that is what actually
    // makes a screen reader interrupt (the container's aria-live is polite).
    if (t === "error") el.setAttribute("role", "alert");

    const ic = document.createElement("span");
    ic.className = "toast-ico";
    ic.innerHTML = icon(ICON[t], { size: 15 });
    const msg = document.createElement("span");
    msg.className = "toast-msg";
    msg.textContent = message;
    const close = document.createElement("button");
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.innerHTML = icon("x", { size: 13 });
    el.append(ic, msg);
    const acts = (Array.isArray(action) ? action : [action])
      .filter((a) => a && a.label && typeof a.run === "function")
      .map((a) => {
        const b = document.createElement("button");
        b.className = "toast-act";
        b.textContent = a.label;
        b._run = a.run;
        el.append(b);
        return b;
      });
    el.append(close);
    this.container.appendChild(el);
    requestAnimationFrame(() => el.classList.add("is-in"));

    // Errors stick until dismissed; everything else auto-dismisses. Hover pauses.
    const ttl = duration != null ? duration : (t === "error" ? 0 : 4000);
    let timer = null, gone = false;
    const dismiss = (why = "close") => {
      if (timer) clearTimeout(timer);
      el.classList.remove("is-in");
      setTimeout(() => el.remove(), 200);
      if (gone) return;
      gone = true;
      if (opts && typeof opts.onClose === "function") { try { opts.onClose(why); } catch (_e) { /* a caller's own */ } }
    };
    const arm = () => { if (ttl > 0) timer = setTimeout(() => dismiss("timeout"), ttl); };
    close.addEventListener("click", () => dismiss("close"));
    for (const b of acts) b.addEventListener("click", () => { dismiss("action"); b._run(); });
    el.addEventListener("mouseenter", () => { if (timer) clearTimeout(timer); });
    el.addEventListener("mouseleave", arm);
    arm();
    return dismiss;
  },

  // Remove every toast at once. A sheet opened from a failure clears them:
  // on a 768-pixel-high screen the sticky failure toast sat on the sheet's
  // own action button.
  clear() {
    if (!this.container) return;
    for (const c of [...this.container.children]) if (!c.classList.contains("toast-keep")) c.remove();
  },
};
