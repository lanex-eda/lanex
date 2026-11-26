// schlayout.js — a layered (Sugiyama) layout for one module's schematic,
// left to right: inputs in the first column, outputs in the last, every cell
// in the column after the latest thing that drives it. Pure: no DOM, so it
// runs in a Web Worker (schworker.js) for big modules and in the tests.
//
// LanEx draws its own schematic instead of shipping a layout library (elkjs
// is EPL/GPL; netlistsvg is unmaintained): the steps are the textbook ones.
//   1. break cycles (a register's output feeding logic before it) by turning
//      the back edges around for the layering only;
//   2. layers by longest path; module inputs first, outputs last;
//   3. a dummy node per column a long edge crosses;
//   4. order each column by the barycentre of its neighbours, sweeping
//      down and up a few times (crossings drop fast);
//   5. y by order, then nudged toward the neighbours without overlapping;
//   6. orthogonal wires: each column gap is a channel, each net gets one
//      vertical track there (a fan-out shares it), tracks packed greedily.
//
//   layoutGraph({ nodes: [{ id, w, h, kind, ins: [portId], outs: [portId] }],
//                 edges: [{ id, from: [nodeId, portId], to: [nodeId, portId], net }] })
//   -> { nodes: { id: { x, y, w, h, ports: { portId: [x, y] } } },
//        edges: { id: [[x, y], ...] }, width, height, ms }

const PORT_GAP = 18;          // between two ports on one side
const NODE_GAP = 22;          // between two nodes in a column
const TRACK = 7;              // between two wires in a channel
const CHAN_PAD = 18;          // a channel's margin on each side
const SWEEPS = 8;

export function portOffsets(n) {
  // Ports spread over the node's side, PORT_GAP apart, centred.
  // A node with a header (a submodule's name) keeps its ports below it.
  const ins = n.ins || [], outs = n.outs || [];
  const pad = n.padTop || 0;
  const off = {};
  const side = (list, h) => {
    const span = (list.length - 1) * PORT_GAP;
    const top = pad + (h - pad - span) / 2;
    list.forEach((p, i) => { off[p] = top + i * PORT_GAP; });
  };
  side(ins, n.h);
  side(outs, n.h);
  return off;
}

export function layoutGraph(graph) {
  const t0 = (typeof performance !== "undefined" ? performance : Date).now();
  const nodes = new Map();
  for (const n of graph.nodes) nodes.set(n.id, { ...n, succ: [], pred: [], layer: 0, order: 0, y: 0, dummy: false });
  const edges = graph.edges.filter((e) => nodes.has(e.from[0]) && nodes.has(e.to[0]) && e.from[0] !== e.to[0]);
  for (const e of edges) { nodes.get(e.from[0]).succ.push(e); nodes.get(e.to[0]).pred.push(e); }

  // 1. cycle breaking: DFS from the inputs, then from anything left
  const state = new Map();
  const back = new Set();
  const roots = [...nodes.values()].sort((a, b) => (a.kind === "in" ? 0 : a.pred.length ? 2 : 1) - (b.kind === "in" ? 0 : b.pred.length ? 2 : 1));
  for (const r of roots) {
    if (state.has(r.id)) continue;
    const stack = [[r, 0]];
    state.set(r.id, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const [n, i] = top;
      if (i < n.succ.length) {
        top[1]++;
        const e = n.succ[i];
        const m = nodes.get(e.to[0]);
        const s = state.get(m.id);
        if (s === 1) back.add(e.id);
        else if (!s) { state.set(m.id, 1); stack.push([m, 0]); }
      } else { state.set(n.id, 2); stack.pop(); }
    }
  }
  const fwd = (e) => back.has(e.id) ? [e.to[0], e.from[0]] : [e.from[0], e.to[0]];

  // 2. layers: longest path in the acyclic version; inputs at 0, cells >= 1
  const indeg = new Map([...nodes.keys()].map((k) => [k, 0]));
  const outs = new Map([...nodes.keys()].map((k) => [k, []]));
  for (const e of edges) { const [a, b] = fwd(e); indeg.set(b, indeg.get(b) + 1); outs.get(a).push(b); }
  const queue = [...nodes.keys()].filter((k) => indeg.get(k) === 0);
  for (const k of nodes.keys()) { const n = nodes.get(k); n.layer = n.kind === "in" ? 0 : 1; }
  const topo = [];
  while (queue.length) {
    const k = queue.shift();
    topo.push(k);
    const n = nodes.get(k);
    for (const b of outs.get(k)) {
      const m = nodes.get(b);
      if (m.layer < n.layer + 1) m.layer = n.layer + 1;
      indeg.set(b, indeg.get(b) - 1);
      if (indeg.get(b) === 0) queue.push(b);
    }
  }
  // a node with no inputs (a constant driver) sits just before what it feeds
  for (const k of [...topo].reverse()) {
    const n = nodes.get(k);
    if (n.kind === "in" || n.kind === "out") continue;
    const hasPred = edges.some((e) => fwd(e)[1] === k);
    if (hasPred) continue;
    const next = outs.get(k).map((b) => nodes.get(b).layer);
    if (next.length) n.layer = Math.max(1, Math.min(...next) - 1);
  }
  let maxLayer = 1;
  for (const n of nodes.values()) if (n.kind !== "out") maxLayer = Math.max(maxLayer, n.layer);
  for (const n of nodes.values()) if (n.kind === "out") n.layer = maxLayer + 1;
  const L = maxLayer + 2;

  // 3. dummies: every edge becomes a chain of unit-length segments
  const cols = Array.from({ length: L }, () => []);
  for (const n of nodes.values()) cols[n.layer].push(n);
  const chains = new Map();   // edge id -> [ node ids from source to target ]
  let dn = 0;
  const mkDummy = (layer, e) => {
    const d = { id: "\u0000d" + (dn++), w: 0, h: 0, kind: "dummy", dummy: true, layer, succ: [], pred: [], ins: [], outs: [], net: e.net, edge: e.id };
    nodes.set(d.id, d);
    cols[layer].push(d);
    return d.id;
  };
  // One dummy per net and column for forward edges: a bus that fans out to
  // cells in many columns runs as one trunk (and one slot in each column),
  // instead of a ribbon of parallel wires pushing the cells apart.
  const shared = new Map();
  for (const e of edges) {
    const a = nodes.get(e.from[0]), b = nodes.get(e.to[0]);
    const chain = [a.id];
    if (back.has(e.id)) {
      // around: from the source's right side back to the target's left side,
      // through every column from the source's to the target's
      for (let l = a.layer; l >= b.layer; l--) chain.push(mkDummy(l, e));
    } else {
      for (let l = a.layer + 1; l < b.layer; l++) {
        const k = e.net + "\u0001" + l;
        if (!shared.has(k)) shared.set(k, mkDummy(l, e));
        chain.push(shared.get(k));
      }
    }
    chain.push(b.id);
    chains.set(e.id, chain);
  }
  // neighbour lists between adjacent columns (for ordering)
  const up = new Map(), down = new Map();
  for (const n of nodes.keys()) { up.set(n, []); down.set(n, []); }
  const portPos = new Map();   // node id -> port offsets (relative, by side index)
  for (const n of nodes.values()) if (!n.dummy) portPos.set(n.id, portOffsets(n));
  const link = (x, y, px, py) => {
    const a = nodes.get(x), b = nodes.get(y);
    if (a.layer < b.layer) { down.get(x).push([y, py, px]); up.get(y).push([x, px, py]); }
    else if (a.layer > b.layer) { up.get(x).push([y, py, px]); down.get(y).push([x, px, py]); }
  };
  for (const e of edges) {
    const ch = chains.get(e.id);
    for (let i = 0; i + 1 < ch.length; i++) {
      const pa = i === 0 ? e.from[1] : null, pb = i + 1 === ch.length - 1 ? e.to[1] : null;
      link(ch[i], ch[i + 1], pa, pb);
    }
  }

  // 4. ordering: barycentre sweeps
  const ordered = cols.map((c) => c.slice());
  const keyOf = (n) => n.kind === "in" || n.kind === "out" ? (n.sort ?? 0) : 0;
  ordered[0].sort((a, b) => keyOf(a) - keyOf(b));
  ordered[L - 1].sort((a, b) => keyOf(a) - keyOf(b));
  const pos = new Map();
  const setPos = () => ordered.forEach((c) => c.forEach((n, i) => pos.set(n.id, i)));
  // first pass: DFS-ish order by first predecessor
  setPos();
  const portFrac = (nid, port) => {
    if (port === null || port === undefined) return 0.5;
    const n = nodes.get(nid);
    const list = (n.ins || []).includes(port) ? n.ins : n.outs || [];
    const i = list.indexOf(port);
    return list.length > 1 ? (i + 0.5) / list.length : 0.5;
  };
  const bary = (n, nbrs) => {
    if (!nbrs.length) return null;
    let s = 0;
    for (const [m, mp] of nbrs) s += pos.get(m) + portFrac(m, mp);
    return s / nbrs.length;
  };
  const crossingsBetween = (a, b) => {
    const segs = [];
    for (const n of a) for (const [m, mp, np] of down.get(n.id)) if (pos.has(m) && b.includes(nodes.get(m))) segs.push([pos.get(n.id) + portFrac(n.id, np), pos.get(m) + portFrac(m, mp)]);
    segs.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
    let c = 0;
    for (let i = 0; i < segs.length; i++) for (let j = i + 1; j < segs.length && j < i + 400; j++) if (segs[j][1] < segs[i][1]) c++;
    return c;
  };
  let best = ordered.map((c) => c.slice());
  let bestX = Infinity;
  for (let it = 0; it < SWEEPS; it++) {
    const downward = it % 2 === 0;
    const range = downward ? [...Array(L).keys()].slice(1) : [...Array(L).keys()].reverse().slice(1);
    for (const l of range) {
      if (l === 0 || l === L - 1) continue;
      const col = ordered[l];
      const b = new Map(col.map((n) => [n.id, bary(n, downward ? up.get(n.id) : down.get(n.id))]));
      col.forEach((n, i) => { if (b.get(n.id) === null) b.set(n.id, i); });
      col.sort((x, y) => b.get(x.id) - b.get(y.id));
      col.forEach((n, i) => pos.set(n.id, i));
    }
    // outputs follow what drives them
    const last = ordered[L - 1];
    const bl = new Map(last.map((n) => [n.id, bary(n, up.get(n.id))]));
    last.sort((x, y) => (bl.get(x.id) ?? 0) - (bl.get(y.id) ?? 0));
    last.forEach((n, i) => pos.set(n.id, i));
    if (nodes.size < 1500) {
      let x = 0;
      for (let l = 0; l + 1 < L; l++) x += crossingsBetween(ordered[l], ordered[l + 1]);
      if (x < bestX) { bestX = x; best = ordered.map((c) => c.slice()); }
    } else best = ordered.map((c) => c.slice());
  }
  for (let l = 0; l < L; l++) ordered[l] = best[l];
  setPos();

  // 5. y: stack each column, then pull toward the neighbours (priority to
  // real nodes), keeping the order and the gaps
  const height = (n) => n.dummy ? 0 : n.h;
  const gapAfter = (n) => n.dummy ? TRACK + 2 : NODE_GAP;
  for (const col of ordered) {
    let y = 0;
    for (const n of col) { n.y = y; y += height(n) + gapAfter(n); }
  }
  const anchorY = (nid, port) => {
    const n = nodes.get(nid);
    if (n.dummy) return n.y;
    const off = portPos.get(nid);
    return n.y + (port !== null && port !== undefined && off[port] !== undefined ? off[port] : n.h / 2);
  };
  const pull = (col, side) => {
    // wanted centre y per node
    const want = col.map((n) => {
      let nb = side === "up" ? up.get(n.id) : side === "down" ? down.get(n.id) : up.get(n.id).concat(down.get(n.id));
      // A module port feeds many cells; it must not drag them all to its
      // row when they have other neighbours (a chain stays straight).
      const inner = nb.filter(([m]) => { const k = nodes.get(m).kind; return k !== "in" && k !== "out"; });
      if (inner.length) nb = inner;
      if (!nb.length) return null;
      const ys = nb.map(([m, mp, np]) => anchorY(m, mp) - (n.dummy ? 0 : (np !== null && np !== undefined && portPos.get(n.id)[np] !== undefined ? portPos.get(n.id)[np] : n.h / 2)));
      ys.sort((a, b) => a - b);
      return ys[Math.floor((ys.length - 1) / 2)];
    });
    // place in order, each as near its wish as the previous allows
    for (let i = 0; i < col.length; i++) {
      const n = col[i];
      const minY = i ? col[i - 1].y + height(col[i - 1]) + gapAfter(col[i - 1]) : -1e9;
      n.y = Math.max(minY, want[i] === null ? n.y : want[i]);
    }
    // then push back up where the wish was lower than the stack allowed
    for (let i = col.length - 2; i >= 0; i--) {
      const n = col[i], nx = col[i + 1];
      const maxY = nx.y - height(n) - gapAfter(n);
      if (n.y > maxY) n.y = maxY;
    }
  };
  for (let it = 0; it < 6; it++) {
    for (let l = 1; l < L; l++) pull(ordered[l], it < 4 ? "up" : "both");
    for (let l = L - 2; l >= 0; l--) pull(ordered[l], it < 4 ? "down" : "both");
  }
  let minY = Infinity;
  for (const n of nodes.values()) minY = Math.min(minY, n.y);
  for (const n of nodes.values()) n.y -= minY - 10;

  // 6. channels and tracks. A vertical run in channel c (between column c
  // and c+1) for net k spans [y1, y2]; runs of one net in one channel merge.
  const runs = Array.from({ length: L + 1 }, () => new Map());   // channel -> net -> {lo, hi}
  const addRun = (c, net, y1, y2) => {
    const m = runs[c];
    const r = m.get(net);
    const lo = Math.min(y1, y2), hi = Math.max(y1, y2);
    if (r) { r.lo = Math.min(r.lo, lo); r.hi = Math.max(r.hi, hi); } else m.set(net, { lo, hi });
  };
  // the points of each chain, before x is known: [column, y, kind]
  const plan = new Map();
  for (const e of edges) {
    const ch = chains.get(e.id);
    const isBack = back.has(e.id);
    const pts = [];
    const ys = ch.map((nid, i) => anchorY(nid, i === 0 ? e.from[1] : i === ch.length - 1 ? e.to[1] : null));
    if (!isBack) {
      // src (col la, right side) -> channel la -> ... -> target (col lb, left side)
      for (let i = 0; i + 1 < ch.length; i++) {
        const c = nodes.get(ch[i]).layer;
        addRun(c, e.net + "|" + (i === 0 ? e.from.join(":") : ch[i]), ys[i], ys[i + 1]);
        pts.push({ c, y1: ys[i], y2: ys[i + 1], key: e.net + "|" + (i === 0 ? e.from.join(":") : ch[i]) });
      }
    } else {
      // around: right of the source, back through the dummies, left of the target
      const la = nodes.get(ch[0]).layer;
      addRun(la, e.net + "|" + e.from.join(":"), ys[0], ys[1]);
      pts.push({ c: la, y1: ys[0], y2: ys[1], key: e.net + "|" + e.from.join(":") });
      for (let i = 1; i + 2 < ch.length; i++) {
        const c = nodes.get(ch[i + 1]).layer;     // the channel right of the next column
        addRun(c, e.net + "|b" + ch[i], ys[i], ys[i + 1]);
        pts.push({ c, y1: ys[i], y2: ys[i + 1], key: e.net + "|b" + ch[i] });
      }
      const lb = nodes.get(ch[ch.length - 1]).layer;
      addRun(lb - 1, e.net + "|t" + e.to.join(":"), ys[ch.length - 2], ys[ch.length - 1]);
      pts.push({ c: lb - 1, y1: ys[ch.length - 2], y2: ys[ch.length - 1], key: e.net + "|t" + e.to.join(":") });
    }
    plan.set(e.id, { pts, ys, isBack });
  }
  const trackOf = runs.map((m) => {
    const list = [...m.entries()].sort((a, b) => a[1].lo - b[1].lo);
    const ends = [];   // per track, the last hi
    const out = new Map();
    for (const [k, r] of list) {
      let t = ends.findIndex((hi) => hi + 4 < r.lo);
      if (t < 0) { t = ends.length; ends.push(r.hi); } else ends[t] = r.hi;
      out.set(k, t);
    }
    return { out, n: ends.length };
  });
  // column x positions
  const colW = ordered.map((col) => Math.max(0, ...col.map((n) => n.w || 0)));
  const colX = [];
  let x = 10;
  for (let l = 0; l < L; l++) {
    colX.push(x);
    x += colW[l] + CHAN_PAD * 2 + Math.max(1, trackOf[l].n) * TRACK;
  }
  const chanX = (c, t) => colX[c] + colW[c] + CHAN_PAD + t * TRACK;
  const outNodes = {};
  let W = 0, H = 0;
  for (const n of nodes.values()) {
    if (n.dummy) continue;
    // right-align output pins, left-align input pins, centre the rest
    const nx = n.kind === "out" ? colX[n.layer] : n.kind === "in" ? colX[n.layer] + colW[n.layer] - n.w : colX[n.layer] + (colW[n.layer] - n.w) / 2;
    const off = portPos.get(n.id);
    const ports = {};
    for (const p of n.ins || []) ports[p] = [nx, n.y + off[p]];
    for (const p of n.outs || []) ports[p] = [nx + n.w, n.y + off[p]];
    outNodes[n.id] = { x: nx, y: n.y, w: n.w, h: n.h, ports, layer: n.layer };
    W = Math.max(W, nx + n.w);
    H = Math.max(H, n.y + n.h);
  }
  const outEdges = {};
  for (const e of edges) {
    const p = plan.get(e.id);
    const a = outNodes[e.from[0]].ports[e.from[1]];
    const b = outNodes[e.to[0]].ports[e.to[1]];
    if (!a || !b) continue;
    const pts = [[a[0], a[1]]];
    for (const s of p.pts) {
      const t = trackOf[s.c] ? trackOf[s.c].out.get(s.key) || 0 : 0;
      const cx = chanX(s.c, t);
      pts.push([cx, s.y1], [cx, s.y2]);
    }
    pts.push([b[0], b[1]]);
    // drop repeated points and straight-line middles
    const clean = [];
    for (const q of pts) {
      const last = clean[clean.length - 1];
      if (last && Math.abs(last[0] - q[0]) < 0.01 && Math.abs(last[1] - q[1]) < 0.01) continue;
      if (clean.length >= 2) {
        const pr = clean[clean.length - 2];
        if ((Math.abs(pr[0] - last[0]) < 0.01 && Math.abs(last[0] - q[0]) < 0.01) || (Math.abs(pr[1] - last[1]) < 0.01 && Math.abs(last[1] - q[1]) < 0.01)) clean.pop();
      }
      clean.push(q);
    }
    outEdges[e.id] = clean;
    for (const q of clean) { W = Math.max(W, q[0]); H = Math.max(H, q[1]); }
  }
  const t1 = (typeof performance !== "undefined" ? performance : Date).now();
  return { nodes: outNodes, edges: outEdges, width: W + 10, height: H + 10, ms: Math.round(t1 - t0), crossings: bestX === Infinity ? null : bestX };
}
