// theme-echarts.js — derive an ECharts theme from the CSS design tokens at call
// time, so charts match the brand AND switch with the dark/light theme. Replaces
// the hardcoded hex palettes that were scattered across the chart modules.
//
// The look every chart shares: hairline solid gridlines one step off the
// surface, muted axis text, no axis line on the value axis, and a tooltip that
// is a small card in the app's own surface colours.

export function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.body || document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (_e) {
    return fallback;
  }
}

// Series colours in a fixed order (--chart-1..5, validated for colour-blind
// separation). A sixth series and beyond repeat; callers keep runs ≤ 5 or
// label every bar so identity never rests on colour alone.
export function chartPalette() {
  return [
    cssVar("--chart-1", "#378ee9"),
    cssVar("--chart-2", "#d164b4"),
    cssVar("--chart-3", "#e05e26"),
    cssVar("--chart-4", "#7d6cdb"),
    cssVar("--chart-5", "#009e92"),
  ];
}

// The inks charts draw text and structure with.
export function chartInk() {
  return {
    text: cssVar("--text-muted", "#7e8ea7"),
    body: cssVar("--text", "#c2cee0"),
    strong: cssVar("--text-strong", "#f2f7ff"),
    grid: cssVar("--border", "#1f2a3b"),
    surface: cssVar("--surface", "#0e131c"),
    surface2: cssVar("--surface-2", "#131b28"),
    accent: cssVar("--met1", "#4da3ff"),
    fail: cssVar("--fail", "#e5484d"),
    warn: cssVar("--warn", "#c98a14"),
    sans: cssVar("--sans", "system-ui, sans-serif"),
    mono: cssVar("--mono", "ui-monospace, monospace"),
  };
}

// A chart takes its colours from the tokens when it is drawn, so a theme
// switch (theme.js "g:theme_changed") redraws every chart on screen:
//   const stop = followTheme(chart, () => chart.setOption(buildOption(), true));
// A disposed chart drops out by itself.
const _follow = new Set();
export function followTheme(chart, redraw) {
  const e = { chart, redraw };
  _follow.add(e);
  return () => _follow.delete(e);
}
export function redrawCharts() {
  for (const e of [..._follow]) {
    if (!e.chart || (e.chart.isDisposed && e.chart.isDisposed())) { _follow.delete(e); continue; }
    try { e.redraw(); } catch (err) { console.error("chart theme", err); }
  }
}
if (typeof document !== "undefined" && !document._chartThemeWired) {
  document._chartThemeWired = true;
  // After the new theme's tokens apply (theme.js switches the body class first).
  document.addEventListener("g:theme_changed", () => requestAnimationFrame(redrawCharts));
}

export function chartTheme() {
  const k = chartInk();
  const axisLabel = { color: k.text, fontSize: 11.5, fontFamily: k.sans };
  return {
    color: chartPalette(),
    backgroundColor: "transparent",
    textStyle: { color: k.text, fontFamily: k.sans },
    title: { textStyle: { color: k.strong } },
    legend: { textStyle: { color: k.body }, icon: "roundRect", itemWidth: 10, itemHeight: 10 },
    tooltip: {
      backgroundColor: k.surface2,
      borderColor: k.grid,
      borderWidth: 1,
      padding: [8, 11],
      textStyle: { color: k.strong, fontSize: 12.5, fontFamily: k.sans },
      extraCssText: "border-radius:8px;box-shadow:0 10px 28px -12px rgba(0,0,0,.55);",
    },
    categoryAxis: {
      axisLine: { lineStyle: { color: k.grid } },
      axisTick: { show: false },
      axisLabel,
      splitLine: { show: false, lineStyle: { color: k.grid } },
    },
    valueAxis: {
      axisLine: { show: false, lineStyle: { color: k.grid } },
      axisTick: { show: false },
      axisLabel,
      nameTextStyle: { color: k.text, fontSize: 11 },
      splitLine: { lineStyle: { color: k.grid, width: 1, type: "solid" } },
    },
  };
}

