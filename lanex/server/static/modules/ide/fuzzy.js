// fuzzy.js — one fuzzy matcher for pickers (the IDE's Ctrl+P; any list a
// person filters by typing a few letters). Pure, no DOM.
//
// The scoring follows fzf's constants (a match 16, a gap −3 then −1 per
// character, a match after a separator +9, at a word start +8, a camelCase
// hump +7, two in a row +4, the first letter of the query ×2), with VS Code's
// tiers on top: an exact name, then a name that starts with the query, then a
// fuzzy name match, then a fuzzy path match. A space splits the query into
// pieces that must all match ("tb cnt"). Zed's character bag throws out most
// candidates before any scoring.
//
//   score("cnt", "counter_tb.v") -> { score, hits: [0, 3, 4] } | null
//   rank("cnt", items, (it) => it.path, { limit: 512 }) -> [{ item, score, hits, nameHits }]

const SEP = new Set(["/", "\\", "_", "-", ".", ":", " ", "$"]);

function isUpper(c) { return c >= "A" && c <= "Z"; }
function isLower(c) { return c >= "a" && c <= "z"; }

// Best alignment of `q` (lower-case) inside `t` (original case): a small
// dynamic program over (query index, text index). Short strings only (names
// and paths), so O(n·m) is cheap.
function align(q, t) {
  const n = q.length, m = t.length;
  if (!n) return { score: 0, hits: [] };
  if (n > m) return null;
  const tl = t.toLowerCase();
  // quick reject: every query character, in order
  let k = 0;
  for (let i = 0; i < m && k < n; i++) if (tl[i] === q[k]) k++;
  if (k < n) return null;
  const bonus = new Array(m);
  for (let i = 0; i < m; i++) {
    const prev = i ? t[i - 1] : "/";
    bonus[i] = SEP.has(prev) ? (prev === "/" || prev === "\\" ? 9 : 8)
      : isLower(prev) && isUpper(t[i]) ? 7
      : /[0-9]/.test(t[i]) && !/[0-9]/.test(prev) ? 4 : 0;
  }
  const NEG = -1e9;
  // H[j]: the best score with q[0..i] matched and q[i] at t[j]. A match
  // right after the previous one gets the "two in a row" bonus; one after a
  // gap pays 3 for the gap's first character and 1 for each after it.
  let H = new Array(m).fill(NEG);
  const back = [];
  for (let i = 0; i < n; i++) {
    const next = new Array(m).fill(NEG);
    const from = new Array(m).fill(-1);
    // run: the best H[k] - gap cost for k <= j - 2, kept as j moves on
    let run = NEG, runAt = -1;
    for (let j = 0; j < m; j++) {
      if (i > 0 && j >= 2) {
        run = run > NEG ? run - 1 : NEG;
        if (H[j - 2] > NEG && H[j - 2] - 3 > run) { run = H[j - 2] - 3; runAt = j - 2; }
      }
      if (tl[j] !== q[i]) continue;
      if (i === 0) {
        next[j] = 16 + bonus[j] * 2 - Math.min(j, 12);   // nearer the start is better
        continue;
      }
      const consec = j >= 1 && H[j - 1] > NEG ? H[j - 1] + 16 + Math.max(bonus[j], 4) : NEG;
      const gapped = run > NEG ? run + 16 + bonus[j] : NEG;
      if (consec === NEG && gapped === NEG) continue;
      if (consec >= gapped) { next[j] = consec; from[j] = j - 1; } else { next[j] = gapped; from[j] = runAt; }
    }
    back.push(from);
    H = next;
  }
  let end = -1, top = NEG;
  for (let j = 0; j < m; j++) if (H[j] > top) { top = H[j]; end = j; }
  if (end < 0) return null;
  const hits = new Array(n);
  let j = end;
  for (let i = n - 1; i >= 0; i--) {
    hits[i] = j;
    j = i > 0 ? back[i][j] : -1;
    if (i > 0 && j < 0) return null;
  }
  return { score: top, hits };
}

// Score `text` for `query`. Pieces split on spaces must all match.
export function score(query, text) {
  const pieces = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const t = String(text || "");
  if (!pieces.length) return { score: 0, hits: [] };
  let total = 0;
  const hits = [];
  for (const p of pieces) {
    const a = align(p, t);
    if (!a) return null;
    total += a.score;
    hits.push(...a.hits);
  }
  return { score: total, hits: [...new Set(hits)].sort((x, y) => x - y) };
}

// Rank items whose text is a path: the file name counts first (VS Code's
// tiers), the folder second. key(item) -> the path; name is after the last /.
export function rank(query, items, key = (x) => x, { limit = 512, recency = null } = {}) {
  const q = String(query || "").trim();
  const out = [];
  const ql = q.toLowerCase();
  for (const item of items) {
    const path = String(key(item) || "");
    const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    const name = path.slice(cut + 1);
    if (!q) { out.push({ item, score: 0, hits: [], nameHits: [] }); continue; }
    const nl = name.toLowerCase();
    let tier = 0, s = null, nameHits = [], hits = [];
    if (nl === ql || nl.replace(/\.[^.]+$/, "") === ql) { tier = 4; nameHits = [...Array(Math.min(q.length, name.length)).keys()]; }
    else if (nl.startsWith(ql)) { tier = 3; nameHits = [...Array(q.length).keys()]; }
    else {
      const ns = !/\s/.test(q) ? score(q, name) : null;
      if (ns) { tier = 2; s = ns.score; nameHits = ns.hits; }
      else {
        const ps = score(q, path);
        if (!ps) continue;
        tier = 1; s = ps.score; hits = ps.hits;
        nameHits = ps.hits.filter((i) => i > cut).map((i) => i - cut - 1);
      }
    }
    const r = recency ? recency(item) : 0;
    out.push({ item, score: tier * 100000 + (s || 0) * 10 + r - name.length, hits, nameHits });
  }
  if (q) out.sort((a, b) => b.score - a.score);
  return out.slice(0, limit);
}

// The text with the hit positions wrapped in <b>, escaped. For a row label.
export function markHits(text, hits, esc) {
  const set = new Set(hits || []);
  const s = String(text || "");
  let out = "";
  let open = false;
  for (let i = 0; i < s.length; i++) {
    const on = set.has(i);
    if (on && !open) { out += "<b>"; open = true; }
    if (!on && open) { out += "</b>"; open = false; }
    out += esc(s[i]);
  }
  if (open) out += "</b>";
  return out;
}
