// signoff.js — Verify's Export menu: the sign-off report as a file (HTML or
// Markdown, written by controller/signoff.py into <run>/lanex-report/), the
// share card (sharecard.js), and the plain-text summary for the clipboard.
//
// The report says what Verify says: the page sends its own verdict words
// (verify-model.js verdictCopy), and every number comes from the same
// /api/verify data and STA reports. "Not a foundry sign-off" is its first line.

import { api } from "./api.js";
import { wireMenu } from "./overlay.js";
import { toast } from "./toast.js";
import { cardData, drawCard, altText, loadImage, SIZES } from "./sharecard.js";

let _busy = false;

export function wireExport({ btn, menu, ctx, copySummary }) {
  if (!btn || !menu) return;
  wireMenu(btn, menu, {
    onOpen: () => {
      const c = ctx();
      const note = menu.querySelector("#vf-export-note");
      if (note) note.textContent = c.tag ? "Report files are saved in " + c.tag + "/lanex-report/." : "";
      const has = !!(c.tag && c.rep);
      menu.querySelectorAll("[data-export]").forEach((b) => { b.disabled = !has || (_busy && b.dataset.export !== "copy"); });
    },
  });
  menu.addEventListener("click", (e) => {
    const it = e.target.closest("[data-export]");
    if (!it || it.disabled) return;
    const c = ctx();
    const what = it.dataset.export;
    if (what === "copy") { copySummary(); return; }
    if (what === "card") { openShareCard(c); return; }
    // Open the report window inside the click, so it isn't a blocked pop-up;
    // it fills in when the file is written.
    const win = what === "html" ? window.open("", "_blank") : null;
    if (win) {
      try {
        win.document.write("<!doctype html><title>Writing the report…</title><body style='background:#06080d;color:#c2cee0;font:15px system-ui;padding:40px'>Writing the sign-off report…</body>");
      } catch (_e) {}
    }
    writeReport(c, what, win);
  });
}

// The chip as an image: the in-app KLayout renderer, in LanEx's layer colours
// on the dark substrate. *px* is the long side.
export async function chipImage(tag, px = 1100) {
  try {
    const info = await api.layoutOpen(tag, "final");
    if (info && info.ok !== false && info.sid) {
      const bb = info.bbox || info.die || null;
      let w = px, h = px;
      if (Array.isArray(bb) && bb.length === 4) {
        const bw = Math.abs(bb[2] - bb[0]), bh = Math.abs(bb[3] - bb[1]);
        if (bw > 0 && bh > 0) { if (bw >= bh) h = Math.round(px * bh / bw); else w = Math.round(px * bw / bh); }
      }
      const url = "/api/layout/render?sid=" + encodeURIComponent(info.sid) + "&w=" + w + "&h=" + h + "&labels=0&boxes=0&bg=%2306080d&os=2";
      const img = await loadImage(url);
      if (img) return img;
    }
  } catch (_e) { /* fall back to the stage thumbnail */ }
  return loadImage("/api/layout/thumb?tag=" + encodeURIComponent(tag) + "&source=final&w=640&h=640&bg=%2306080d");
}

async function asDataUrl(img, max = 900) {
  if (!img) return null;
  try {
    const k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
    const c = document.createElement("canvas");
    c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL("image/png");
  } catch (_e) { return null; }
}

export async function writeReport(c, what, win = null) {
  if (!c.tag || _busy) return;
  _busy = true;
  const t0 = Date.now();
  toast.show("Writing the sign-off report for " + c.tag + "…", "info", 4000);
  try {
    let chip = null;
    if (what === "html") {
      const img = await Promise.race([chipImage(c.tag, 900), new Promise((r) => setTimeout(() => r(null), 20000))]);
      chip = await asDataUrl(img);
    }
    const v = c.verdict || {};
    const res = await api.verifyReport({ tag: c.tag, base: c.base || null, verdict: { tone: v.tone, title: v.title, sub: v.sub }, chip_png: chip });
    const rel = res.files[what === "md" ? "md" : "html"];
    const url = api.runFileUrl(c.tag, rel);
    if (what === "html") {
      if (win && !win.closed) win.location.href = url;
      else window.open(url, "_blank");
    } else {
      download(url, (c.rep && c.rep.design ? c.rep.design + "-" : "") + c.tag + "-signoff.md");
    }
    toast.show("Sign-off report written: " + c.tag + "/" + rel + " (" + Math.max(1, Math.round((Date.now() - t0) / 1000)) + " s).", "success", 9000, {
      label: "Show in folder", run: () => api.revealFile(c.tag, rel).catch((ex) => toast.show("Couldn't open the folder: " + (ex.message || ex), "warn")),
    });
  } catch (ex) {
    if (win && !win.closed) win.close();
    toast.show("Couldn't write the report: " + (ex.message || ex), "error", 10000);
  } finally {
    _busy = false;
  }
}

function download(url, name) {
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------- the share card
let _logo = null;

export async function openShareCard(c) {
  if (!c.tag) return;
  const { customDialog } = await import("./dialog.js");
  const st = { layout: "wide", showRun: true, showDate: true, sum: null, chip: undefined };
  const body =
    "<div class='sc-dlg'>" +
      "<div class='sc-tools'><div class='seg' role='group' aria-label='Card shape'>" +
        "<button class='seg-btn is-active' data-sc-layout='wide' aria-pressed='true'>Landscape <small>1200 × 630</small></button>" +
        "<button class='seg-btn' data-sc-layout='square' aria-pressed='false'>Square <small>1080 × 1080</small></button></div>" +
        "<label class='wn-check'><input type='checkbox' data-sc-run checked/> Show the run name</label>" +
        "<label class='wn-check'><input type='checkbox' data-sc-date checked/> Show the date</label></div>" +
      "<div class='sc-stage'><canvas class='sc-canvas' role='img' aria-label='Share card preview'></canvas><p class='sc-status' role='status'>Drawing the chip…</p></div>" +
      "<p class='muted sc-note'>Saved at twice the size (2400 × 1260 or 2160 × 2160 px) so it stays sharp on high-density screens. Every number is this run's own; the card says it is not a foundry sign-off.</p>" +
    "</div>";
  let blob = null;
  await customDialog({
    title: "Share card · " + c.tag, wide: true, bodyHtml: body,
    buttons: [{ label: "Close", value: null, cls: "btn-ghost" }],
    onMount: async (back) => {
      const acts = back.querySelector(".dlg-actions");
      const mk = (label, cls, fn) => { const b = document.createElement("button"); b.className = "btn " + cls; b.textContent = label; b.disabled = true; b.addEventListener("click", fn); acts.insertBefore(b, acts.firstChild); return b; };
      const bAlt = mk("Copy alt text", "btn-ghost", async () => {
        try { await navigator.clipboard.writeText(altText(cardData(st.sum, c.verdict, st))); toast.show("Alt text copied", "success"); } catch (_e) { toast.show("Clipboard not available", "warn"); }
      });
      const bCopy = mk("Copy image", "btn-ghost", async () => {
        if (!blob) return;
        try { await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]); toast.show("Card copied: paste it into a post", "success"); }
        catch (_e) { toast.show("This browser can't copy images; use Save PNG.", "warn"); }
      });
      const bSave = mk("Save PNG", "btn-primary", () => {
        if (!blob) return;
        const u = URL.createObjectURL(blob);
        download(u, ((c.rep && c.rep.design) || "chip") + "-" + c.tag + (st.layout === "square" ? "-square" : "") + ".png");
        setTimeout(() => URL.revokeObjectURL(u), 4000);
      });
      // The buttons sit Close · … in reading order: Save last is the primary.
      acts.appendChild(bAlt); acts.appendChild(bCopy); acts.appendChild(bSave);
      const canvas = back.querySelector(".sc-canvas");
      const status = back.querySelector(".sc-status");
      const redraw = async () => {
        if (!st.sum) return;
        const d = cardData(st.sum, c.verdict, st);
        await drawCard(canvas, d, { layout: st.layout, scale: 2, chip: st.chip, logo: _logo });
        const [W, H] = SIZES[st.layout];
        canvas.style.aspectRatio = W + " / " + H;
        canvas.setAttribute("aria-label", "Share card preview. " + altText(d));
        blob = await new Promise((r) => canvas.toBlob(r, "image/png"));
        [bSave, bCopy, bAlt].forEach((b) => { b.disabled = !blob; });
      };
      back.addEventListener("click", (e) => {
        const l = e.target.closest("[data-sc-layout]");
        if (!l) return;
        st.layout = l.dataset.scLayout;
        back.querySelectorAll("[data-sc-layout]").forEach((b) => { b.classList.toggle("is-active", b === l); b.setAttribute("aria-pressed", String(b === l)); });
        redraw();
      });
      back.addEventListener("change", (e) => {
        if (e.target.matches("[data-sc-run]")) { st.showRun = e.target.checked; redraw(); }
        if (e.target.matches("[data-sc-date]")) { st.showDate = e.target.checked; redraw(); }
      });
      try {
        const [sum, chip, logo] = await Promise.all([
          api.verifySummary(c.tag, c.base),
          Promise.race([chipImage(c.tag, 1100), new Promise((r) => setTimeout(() => r(null), 25000))]),
          _logo ? Promise.resolve(_logo) : loadImage("/static/vendor/lanex-logo-dark.png", 8000),
        ]);
        st.sum = sum; st.chip = chip; _logo = logo;
        status.textContent = chip ? "" : "Couldn't draw the layout (KLayout may not be available, or the run has no final GDS); the card still has the numbers.";
        status.classList.toggle("is-warn", !chip);
        await redraw();
      } catch (ex) {
        status.textContent = "Couldn't read this run's results: " + (ex.message || ex);
        status.classList.add("is-warn");
      }
    },
  });
}
