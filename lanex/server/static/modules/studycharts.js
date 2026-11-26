// studycharts.js — the charts a study (sweep) is read by, as pure ECharts
// option builders (plain data in, an option out), so node tests check them
// and every page draws them alike:
//
//   byValueOption   one metric against one swept setting: points at their
//                   real values, failed points as crosses on a rail at the
//                   bottom (never dropped), queued points hollow, the best
//                   ringed; slack gets the 0 line and a shaded "fails timing"
//                   band; a clock sweep reads in ns and MHz (two x axes)
//   heatmapOption   two swept settings: one cell per pair, slack on a scale
//                   that diverges at 0 (blue meets, orange misses), failed
//                   cells crossed, queued cells outlined
//   paretoOption    any two metrics: the front (the points no other point
//                   beats on both) as a step line with filled diamonds, the
//                   rest hollow, failed points counted under the chart
//
// Colours come from theme-echarts.js (the CSS tokens), so they follow the
// theme. Points: { x, y, key, tag, label } with key a studymodel POINT key.

import { chartTheme, chartInk } from "./theme-echarts.js";

const fin = (v) => typeof v === "number" && Number.isFinite(v);
const CROSS = "path://M2,2 L10,10 M10,2 L2,10";
const escT = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmtN = (v, d = 3) => (fin(v) ? (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en-US") : Number(v.toFixed(d)).toString()) : "—");
const mhz = (ns) => (ns > 0 ? (1000 / ns >= 100 ? Math.round(1000 / ns) : (1000 / ns).toPrecision(3)) + " MHz" : "");

// The front of a set of points for two directions (+1 higher is better, -1
// lower): the points no other point beats on both. Sorted by x.
export function paretoFront(points, dirX, dirY) {
  const P = points.filter((p) => fin(p.x) && fin(p.y));
  const better = (a, b) => {   // a dominates b
    const gx = dirX * (a.x - b.x), gy = dirY * (a.y - b.y);
    return gx >= 0 && gy >= 0 && (gx > 0 || gy > 0);
  };
  return P.filter((p) => !P.some((q) => q !== p && better(q, p))).sort((a, b) => a.x - b.x);
}

// One metric by one swept setting.
// `line`: join the points (a study, one point per value); off for a history
// of runs, where several runs can share a value.
export function byValueOption({ xvar, xunit = "", metric, points, clock = false, line = true }) {
  const k = chartInk(), th = chartTheme();
  const ok = points.filter((p) => fin(p.x) && fin(p.y) && (p.key === "clean" || p.key === "warnings" || p.key === "missed"));
  const failed = points.filter((p) => fin(p.x) && (p.key === "failed" || p.key === "stopped" || p.key === "pruned" || p.key === "interrupted"));
  const queued = points.filter((p) => fin(p.x) && (p.key === "queued" || p.key === "running" || p.key === "skipped"));
  if (!ok.length && !failed.length && !queued.length) return null;
  const xs = points.map((p) => p.x).filter(fin);
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const padX = (hi - lo || Math.abs(hi) || 1) * 0.06;
  const slack = metric.unit === "ns";
  const best = metric.dir ? ok.filter((p) => p.key !== "missed").sort((a, b) => metric.dir * (b.y - a.y))[0] : null;
  const ticks = [...new Set(xs)].sort((a, b) => a - b);
  const xAxisBase = { ...th.valueAxis, type: "value", min: lo - padX, max: hi + padX, scale: true,
    axisLabel: { ...th.valueAxis.axisLabel, customValues: ticks, formatter: (v) => fmtN(v, 3) },
    axisTick: { show: true, customValues: ticks, lineStyle: { color: k.grid } }, splitLine: { show: false } };
  const xAxis = [{ ...xAxisBase, position: "bottom", name: xvar + (xunit ? " (" + xunit + ")" : ""), nameLocation: "middle", nameGap: 28,
    nameTextStyle: { color: k.text, fontSize: 11.5, fontFamily: k.mono } }];
  // The same periods read as frequencies along the top ("125 MHz").
  if (clock) xAxis.push({ ...xAxisBase, position: "top", name: "",
    axisLabel: { ...xAxisBase.axisLabel, formatter: (v) => mhz(v) } });
  const ys = ok.map((p) => p.y);
  const series = [{
    type: line ? "line" : "scatter", name: metric.label, data: ok.slice().sort((a, b) => a.x - b.x).map((p) => ({ value: [p.x, p.y], p })),
    connectNulls: false, showSymbol: true, symbol: "circle", symbolSize: 8, z: 3,
    lineStyle: { color: k.accent, width: 2 }, itemStyle: { color: k.accent, borderColor: k.surface, borderWidth: 1.5 },
    markLine: slack ? { silent: true, symbol: "none", data: [{ yAxis: 0 }], lineStyle: { color: k.text, type: "dashed", width: 1 },
      label: { formatter: "slack 0", position: "insideStartTop", color: k.text, fontSize: 11 } } : undefined,
    markArea: slack && ys.some((y) => y < 0) ? { silent: true, itemStyle: { color: k.fail, opacity: 0.08 },
      label: { show: true, position: "insideBottomLeft", color: k.fail, fontSize: 11, formatter: "fails timing" },
      data: [[{ name: "fails timing" }, { yAxis: 0 }]] } : undefined,
  }];
  if (best) series.push({ type: "scatter", name: "best", data: [{ value: [best.x, best.y], p: best }], symbol: "circle", symbolSize: 16, z: 4,
    itemStyle: { color: "transparent", borderColor: k.accent, borderWidth: 2 }, label: { show: true, position: "left", color: k.strong, fontSize: 11, formatter: "best" } });
  // Failed points have no number: crosses on a rail at the bottom (a hidden
  // second y axis pinned 0..1), so they are seen, never silently dropped.
  if (failed.length) series.push({ type: "scatter", name: "failed", yAxisIndex: 1, data: failed.map((p) => ({ value: [p.x, 0.04], p })), clip: false,
    symbol: CROSS, symbolSize: 11, itemStyle: { color: k.fail, borderColor: k.fail, borderWidth: 2 }, z: 4 });
  if (queued.length) series.push({ type: "scatter", name: "not finished", yAxisIndex: 1, data: queued.map((p) => ({ value: [p.x, 0.04], p })), clip: false,
    symbol: "emptyCircle", symbolSize: 9, itemStyle: { color: k.text }, z: 3 });
  return {
    ...th, animationDuration: 250,
    aria: { enabled: true, label: { description: metric.label + " for " + points.length + " values of " + xvar + ": " + ok.length + " finished, " + failed.length + " failed." } },
    grid: { left: 58, right: clock ? 46 : 24, top: clock ? 46 : 30, bottom: 46 },
    tooltip: { ...th.tooltip, trigger: "item", formatter: (it) => {
      const p = it.data && it.data.p;
      if (!p) return "";
      const xv = clock ? p.x + " ns (" + mhz(p.x) + ")" : xvar + " " + fmtN(p.x);
      return "<b>" + escT(xv) + "</b><br>" + escT(p.word || p.key) + (fin(p.y) && it.seriesName !== "failed" && it.seriesName !== "not finished" ? " · " + escT(metric.label) + " " + escT(fmtN(p.y)) + " " + escT(metric.unit) : "") +
        (p.detail ? "<br><span style='opacity:.75'>" + escT(p.detail) + "</span>" : "") + "<br><span style='opacity:.6'>" + escT(p.tag || "") + "</span>";
    } },
    xAxis,
    yAxis: [
      { ...th.valueAxis, type: "value", scale: !slack, name: metric.label + (metric.unit ? " (" + metric.unit + ")" : ""), nameLocation: "middle", nameGap: 44,
        nameTextStyle: { color: k.text, fontSize: 11.5 }, axisLabel: { ...th.valueAxis.axisLabel, formatter: (v) => fmtN(v, 2) } },
      { type: "value", min: 0, max: 1, show: false },
    ],
    series,
  };
}

// Two swept settings: a heatmap of one metric. `xs`, `ys`: the values in
// order; points carry x and y as those values.
export function heatmapOption({ xvar, yvar, xs, ys, metric, points }) {
  const k = chartInk(), th = chartTheme();
  const xi = new Map(xs.map((v, i) => [String(v), i])), yi = new Map(ys.map((v, i) => [String(v), i]));
  const cells = [], fails = [], waits = [];
  for (const p of points) {
    const a = xi.get(String(p.xv)), b = yi.get(String(p.yv));
    if (a == null || b == null) continue;
    if (fin(p.y) && (p.key === "clean" || p.key === "warnings" || p.key === "missed")) cells.push({ value: [a, b, p.y], p });
    else if (p.key === "failed" || p.key === "stopped" || p.key === "pruned" || p.key === "interrupted") fails.push({ value: [a, b], p });
    else waits.push({ value: [a, b], p });
  }
  if (!cells.length && !fails.length) return null;
  const vals = cells.map((c) => c.value[2]);
  const slack = metric.unit === "ns";
  const M = Math.max(...vals.map(Math.abs), 1e-9);
  const lo = slack ? -M : Math.min(...vals), hi = slack ? M : Math.max(...vals);
  // Slack diverges at 0: orange misses, blue meets, the middle the surface
  // (so near-zero cells are not the brightest on a dark theme). Others: one
  // blue hue, stronger where better.
  const inRange = slack ? ["#d9622b", "#f1a340", k.surface2, "#6aa9e8", k.accent]
    : metric.dir < 0 ? [k.accent, k.surface2] : [k.surface2, k.accent];
  const tip = (it) => {
    const p = it.data && it.data.p;
    if (!p) return "";
    return "<b>" + escT(xvar + " " + p.xv + " × " + yvar + " " + p.yv) + "</b><br>" + escT(p.word || p.key) +
      (fin(p.y) && p.key !== "failed" ? " · " + escT(metric.label) + " " + escT(fmtN(p.y)) + " " + escT(metric.unit) : "") + (p.detail ? "<br>" + escT(p.detail) : "");
  };
  return {
    ...th, animationDuration: 200,
    aria: { enabled: true, label: { description: metric.label + " for each pair of " + xvar + " and " + yvar + "." } },
    grid: { left: 70, right: 90, top: 14, bottom: 50 },
    tooltip: { ...th.tooltip, trigger: "item", formatter: tip },
    xAxis: { type: "category", data: xs.map(String), name: xvar, nameLocation: "middle", nameGap: 30, nameTextStyle: { color: k.text, fontFamily: k.mono, fontSize: 11.5 },
      axisLabel: { color: k.text }, axisLine: { lineStyle: { color: k.grid } }, axisTick: { show: false }, splitArea: { show: false } },
    yAxis: { type: "category", data: ys.map(String), inverse: true, name: yvar, nameLocation: "middle", nameGap: 50, nameTextStyle: { color: k.text, fontFamily: k.mono, fontSize: 11.5 },
      axisLabel: { color: k.text }, axisLine: { lineStyle: { color: k.grid } }, axisTick: { show: false } },
    visualMap: { type: "continuous", min: lo, max: hi, dimension: 2, seriesIndex: 0, calculable: false, orient: "vertical", right: 6, top: "middle", itemHeight: 140,
      text: [slack ? "meets" : metric.dir < 0 ? "worse" : "better", slack ? "misses" : metric.dir < 0 ? "better" : "worse"], textStyle: { color: k.text, fontSize: 11 }, inRange: { color: inRange } },
    series: [
      { type: "heatmap", data: cells, label: { show: xs.length <= 16 && ys.length <= 16, color: k.strong, fontSize: 11, formatter: (it) => fmtN(it.data.value[2], 2) },
        itemStyle: { borderColor: k.surface, borderWidth: 2 }, emphasis: { itemStyle: { borderColor: k.accent } } },
      { type: "scatter", data: fails, symbol: CROSS, symbolSize: 14, itemStyle: { color: k.fail, borderColor: k.fail, borderWidth: 2 }, z: 5 },
      { type: "scatter", data: waits, symbol: "emptyCircle", symbolSize: 10, itemStyle: { color: k.text }, z: 5 },
    ],
  };
}

// Any two metrics: the trade-off, with the front marked.
export function paretoOption({ x, y, points }) {
  const k = chartInk(), th = chartTheme();
  const ok = points.filter((p) => fin(p.x) && fin(p.y) && (p.key === "clean" || p.key === "warnings"));
  const missed = points.filter((p) => fin(p.x) && fin(p.y) && p.key === "missed");
  if (ok.length + missed.length < 2) return null;
  const front = paretoFront(ok, x.dir || -1, y.dir || 1);
  const onFront = new Set(front);
  const dom = ok.filter((p) => !onFront.has(p));
  const arrow = (m) => (m.dir > 0 ? ", larger is better" : m.dir < 0 ? ", smaller is better" : "");
  const pt = (p) => ({ value: [p.x, p.y], p });
  const tip = (it) => {
    const p = it.data && it.data.p;
    if (!p) return "";
    return "<b>" + escT(p.label || p.tag) + "</b><br>" + escT(x.label) + " " + escT(fmtN(p.x)) + " " + escT(x.unit) + " · " + escT(y.label) + " " + escT(fmtN(p.y)) + " " + escT(y.unit) +
      "<br>" + escT(onFront.has(p) ? "On the front: nothing here beats it on both." : p.key === "missed" ? "Timing missed: not on the front." : "Another point beats it on both.");
  };
  return {
    ...th, animationDuration: 250,
    aria: { enabled: true, label: { description: y.label + " against " + x.label + ": " + front.length + " points on the front of " + (ok.length + missed.length) + "." } },
    grid: { left: 62, right: 24, top: 18, bottom: 50 },
    tooltip: { ...th.tooltip, trigger: "item", formatter: tip },
    xAxis: { ...th.valueAxis, type: "value", scale: true, name: x.label + (x.unit ? " (" + x.unit + ")" : "") + arrow(x), nameLocation: "middle", nameGap: 30, nameTextStyle: { color: k.text, fontSize: 11.5 } },
    yAxis: { ...th.valueAxis, type: "value", scale: true, name: y.label + (y.unit ? " (" + y.unit + ")" : "") + arrow(y), nameLocation: "middle", nameGap: 46, nameTextStyle: { color: k.text, fontSize: 11.5 } },
    series: [
      { type: "line", name: "front", step: "end", data: front.map(pt), symbol: "diamond", symbolSize: 11, z: 4,
        lineStyle: { color: k.accent, width: 2 }, itemStyle: { color: k.accent },
        label: { show: front.length <= 12, position: "top", color: k.strong, fontSize: 11, formatter: (it) => (it.data.p.label || "") } },
      { type: "scatter", name: "beaten", data: dom.map(pt), symbol: "emptyCircle", symbolSize: 8, itemStyle: { color: k.text, opacity: 0.75 }, z: 3 },
      { type: "scatter", name: "timing missed", data: missed.map(pt), symbol: "emptyCircle", symbolSize: 9, itemStyle: { color: k.warn, borderType: "dashed" }, z: 3 },
    ],
  };
}
