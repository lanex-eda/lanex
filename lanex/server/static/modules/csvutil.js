// csvutil.js — tiny CSV helpers shared by the export buttons (Analytics, Compare,
// DSE). Pure, zero dependency, RFC-4180 quoting.

export function csvCell(v, { quoteText = false } = {}) {
  if (typeof v === "number") {
    // LibreLane's own words for the values JSON can't hold.
    if (Number.isNaN(v)) return "NaN";
    if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
    return String(v);
  }
  let s = (v === null || v === undefined) ? "" : String(v);
  // Spreadsheet formula-injection hardening (OWASP): a NON-numeric cell
  // starting with = + - @, a tab, CR or LF, or their full-width forms would
  // run as a formula when the CSV is opened in Excel/Sheets. Plain numbers
  // (incl. negatives/exponents) pass unchanged.
  if (/^[=+\-@\t\r\n＝＋－＠]/.test(s) && !/^-?\d*\.?\d+([eE][+-]?\d+)?$/.test(s)) s = "'" + s;
  if (quoteText && s !== "") return '"' + s.replace(/"/g, '""') + '"';
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// rows: array of arrays. Returns a CSV string. `excel`: what opens right on a
// double-click in Excel and LibreOffice: a UTF-8 BOM (Excel reads UTF-8 only
// with it), CRLF row ends (RFC 4180, as LibreLane's own metrics.csv), every
// text field quoted, numbers bare. No "sep=" line (Excel then ignores the BOM).
export function toCsv(rows, { excel = false } = {}) {
  if (!excel) return (rows || []).map((r) => (r || []).map((c) => csvCell(c)).join(",")).join("\n");
  return "﻿" + (rows || []).map((r) => (r || []).map((c) => csvCell(c, { quoteText: true })).join(",") + "\r\n").join("");
}

// A text file the page made (JSON export), saved like a CSV.
export function downloadText(filename, text, mime = "application/json") {
  const blob = new Blob([text], { type: mime + ";charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Trigger a browser download of `text` as `filename`.
export function downloadCsv(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
