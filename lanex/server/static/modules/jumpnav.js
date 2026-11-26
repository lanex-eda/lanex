// jumpnav.js — "jump to section" links for long tabs, with a scroll-spy that
// underlines the section in view. Pure DOM, zero dependency.
//
//   container.insertAdjacentHTML("afterbegin", jumpBarHtml([{target,label}, …]));
//   wireJump(container);   // the container's .section-jump
//   wireJumpNav(nav);      // any nav whose buttons carry data-jump="<id>"

import { escapeHtml } from "./html.js";

export function jumpBarHtml(items, { label = "Jump to section" } = {}) {
  const esc = escapeHtml;
  return "<nav class='section-jump' aria-label='" + esc(label) + "'>" +
    (items || []).map((i) =>
      "<button class='chip chip-clickable' data-jump='" + esc(i.target) + "'>" + esc(i.label) + "</button>").join("") +
    "</nav>";
}

export function wireJump(container, opts = {}) {
  const nav = (container || document).querySelector(".section-jump");
  if (nav) wireJumpNav(nav, opts);
}

// At the bottom of a page, which of the visible sections is current. "last"
// (the default): the last one on screen, so a short final section can light
// up. "most-visible": the one taking the most of the screen, for a page whose
// last section is a folded bar under a tall one (Setup: "Ready to run?" over
// the All settings bar). `boxes`: [{id, top, bottom}] in screen pixels.
export function bottomPick(boxes, viewTop, viewBottom, mode = "last") {
  const seen = (boxes || []).filter((b) => b.bottom > viewTop && b.top < viewBottom);
  if (!seen.length) return null;
  if (mode !== "most-visible") return seen[seen.length - 1].id;
  let best = seen[0], most = -1;
  for (const b of seen) {
    const h = Math.min(b.bottom, viewBottom) - Math.max(b.top, viewTop);
    if (h > most) { most = h; best = b; }
  }
  return best.id;
}

// The nearest scrolling ancestor (the work area for page headers, the same for
// the sticky category tabs inside a card).
function scrollRootOf(el) {
  return el.closest("main.work") || document.querySelector("main.work") || null;
}

// The app's UI zoom (CSS zoom on <html>): client rects are this many times the
// CSS px that scrollTop and scrollTo work in.
function zoomOf(root) {
  if (!root || !root.offsetHeight) return 1;
  return root.getBoundingClientRect().height / root.offsetHeight || 1;
}

// Click-to-jump + scroll-spy for one nav. Chips are read live, so navs whose
// buttons are re-rendered (the Constraints categories) keep working. Idempotent.
export function wireJumpNav(nav, { clicks = true, atBottom = "last" } = {}) {
  if (!nav || nav._jumpWired) return;
  nav._jumpWired = true;
  const root = scrollRootOf(nav);
  const chips = () => Array.from(nav.querySelectorAll("[data-jump]"));
  const setCurrent = (id) =>
    chips().forEach((c) => c.classList.toggle("is-current", c.dataset.jump === id));

  // A clicked link stays current while its section is on screen, even when the
  // page can't scroll far enough to bring that section to the top (short
  // sections near the end). Any manual scroll input releases it.
  let pinned = null;
  const release = () => { pinned = null; };
  if (root) {
    root.addEventListener("wheel", release, { passive: true });
    root.addEventListener("touchstart", release, { passive: true });
    root.addEventListener("keydown", release);
  }

  nav.addEventListener("click", (e) => {
    const b = e.target.closest("[data-jump]");
    if (!b) return;
    if (!clicks) { pinned = b.dataset.jump; setCurrent(pinned); return; }
    const t = document.getElementById(b.dataset.jump);
    if (!t) return;
    // <details> targets won't show their content unless open.
    if (t.tagName === "DETAILS") t.open = true;
    pinned = b.dataset.jump;
    setCurrent(pinned);
    const k = zoomOf(root);
    const rootTop = root ? root.getBoundingClientRect().top : 0;
    const line = (nav.getBoundingClientRect().bottom - rootTop) / k;
    const top = (root ? root.scrollTop : window.scrollY) + (t.getBoundingClientRect().top - rootTop) / k - line - 12;
    (root || window).scrollTo({ top, behavior: "smooth" });
  });

  let raf = 0;
  const compute = () => {
    raf = 0;
    if (nav.offsetParent === null) return;          // its tab isn't visible
    const list = chips();
    if (!list.length) return;
    const rootBox = root ? root.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    if (pinned) {
      const t = document.getElementById(pinned);
      const r = t && t.getBoundingClientRect();
      if (r && r.bottom > rootBox.top && r.top < rootBox.bottom) { setCurrent(pinned); return; }
      pinned = null;
    }
    // The line just under the sticky nav; the last section to cross it is current.
    const base = nav.getBoundingClientRect().bottom + 20 * zoomOf(root);
    let current = list[0].dataset.jump;
    for (const c of list) {
      const t = document.getElementById(c.dataset.jump);
      if (t && t.offsetParent !== null && t.getBoundingClientRect().top <= base) current = c.dataset.jump;
    }
    // At the very bottom, the last visible section wins — but only once the
    // user has actually scrolled there. A page that doesn't scroll yet (its
    // sections still loading, or all of it fitting on a wide screen) is "at the
    // bottom" at scrollTop 0, which used to light up the LAST chip on load.
    if (root && root.scrollTop > 0 && root.scrollTop + root.clientHeight >= root.scrollHeight - 2) {
      const boxes = [];
      for (const c of list) {
        const t = document.getElementById(c.dataset.jump);
        if (!t || t.offsetParent === null) continue;
        const r = t.getBoundingClientRect();
        boxes.push({ id: c.dataset.jump, top: r.top, bottom: r.bottom });
      }
      // The sticky nav covers the top of the scroll area.
      current = bottomPick(boxes, Math.max(rootBox.top, nav.getBoundingClientRect().bottom), rootBox.bottom, atBottom) || current;
    }
    setCurrent(current);
  };
  const onScroll = () => { if (!raf) raf = requestAnimationFrame(compute); };
  (root || window).addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  // A page switch recomputes only the nav of the page that showed, in the
  // next frame (every nav measuring itself inside the click forced a layout
  // per nav).
  const sec = nav.closest(".section");
  document.addEventListener("g:tab_activated", (e) => {
    const tab = e.detail && e.detail.tab;
    if (sec && tab && sec.id !== "sec-" + tab) return;
    if (!raf) raf = requestAnimationFrame(compute);
  });
  onScroll();   // first measure in a frame, not while wiring
}

// Kept for callers that only want the spy (Analytics wires its own clicks).
export function wireScrollSpy(nav) {
  wireJumpNav(nav, { clicks: false });
}
