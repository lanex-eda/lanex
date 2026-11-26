// coach.js — a short step-through lesson that changes the view as it goes
// (the Layout tab's "Reading a layout"). Reusable by any page:
//
//   const c = startCoach({
//     host: element,            // the card floats in this element (bottom left)
//     eyebrow: "Reading a layout",
//     steps: [{ title, body, enter: async () => {}, done: "Done" }, ...],
//     onEnd: (how) => {},       // "done" | "leave"; the page puts its view back
//   });
//   c.leave();
//
// Offered, never pushed: the page starts it from a link. One idea per step,
// each step does what it says on screen (enter()), Back and Next move, Esc
// and Leave end it, and ← / → work while the card has focus. It sits on the
// overlay stack as a non-modal sheet, so Esc order and focus return are the
// app's; a press on the canvas does not close it (a step may ask for one).

import { openLayer, closeLayer } from "./overlay.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function startCoach({ host, eyebrow = "", steps = [], onEnd = null, label = "" } = {}) {
  if (!host || !steps.length) return null;
  const card = document.createElement("div");
  card.className = "coach";
  card.tabIndex = -1;
  card.setAttribute("aria-live", "polite");
  host.appendChild(card);
  let i = 0;
  let ended = false;
  let busy = false;
  const titleId = "coach-t-" + Math.random().toString(36).slice(2, 8);

  async function show(n) {
    if (busy || ended) return;
    i = Math.max(0, Math.min(steps.length - 1, n));
    const s = steps[i];
    busy = true;
    card.classList.add("is-busy");
    try { if (s.enter) await s.enter(); } catch (e) { console.error("coach step", e); }
    busy = false;
    card.classList.remove("is-busy");
    if (ended) return;
    const last = i === steps.length - 1;
    card.innerHTML =
      "<div class='coach-top'><span class='coach-eyebrow'>" + esc(eyebrow) + " · " + (i + 1) + "/" + steps.length + "</span>" +
      "<button class='coach-x' data-c='leave' aria-label='Leave the lesson'>×</button></div>" +
      "<h4 class='coach-title' id='" + titleId + "'>" + esc(s.title) + "</h4>" +
      "<p class='coach-body'>" + esc(s.body) + "</p>" +
      (s.hint ? "<p class='coach-hint'>" + esc(s.hint) + "</p>" : "") +
      "<div class='coach-dots' aria-hidden='true'>" + steps.map((_x, k) => "<i" + (k === i ? " class='is-on'" : "") + "></i>").join("") + "</div>" +
      "<div class='coach-act'>" +
      (i > 0 ? "<button class='btn btn-ghost btn-sm' data-c='back'>Back</button>" : "<span></span>") +
      "<span class='coach-act-r'>" +
      (last && s.extra ? "<button class='btn btn-ghost btn-sm' data-c='extra'>" + esc(s.extra.label) + "</button>" : "") +
      "<button class='btn btn-primary btn-sm' data-c='" + (last ? "done" : "next") + "' data-autofocus>" + esc(last ? (s.done || "Done") : "Next") + "</button>" +
      "</span></div>";
    card.setAttribute("aria-labelledby", titleId);
    const btn = card.querySelector("[data-autofocus]");
    if (btn) btn.focus({ preventScroll: true });
  }

  function end(how) {
    if (ended) return;
    ended = true;
    closeLayer(card);
    card.remove();
    if (onEnd) onEnd(how);
  }

  card.addEventListener("click", (e) => {
    const b = e.target.closest("[data-c]");
    if (!b) return;
    const c = b.dataset.c;
    if (c === "next") show(i + 1);
    else if (c === "back") show(i - 1);
    else if (c === "done") end("done");
    else if (c === "leave") end("leave");
    else if (c === "extra") { const s = steps[i]; end("done"); s.extra && s.extra.run && s.extra.run(); }
  });
  card.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" && i < steps.length - 1) { e.preventDefault(); show(i + 1); }
    else if (e.key === "ArrowLeft" && i > 0) { e.preventDefault(); show(i - 1); }
  });
  openLayer(card, { kind: "sheet", modal: false, outside: false, label: label || eyebrow, onClose: () => end("leave"), focus: false });
  show(0);
  return { leave: () => end("leave"), next: () => show(i + 1), back: () => show(i - 1), get step() { return i; }, el: card };
}
