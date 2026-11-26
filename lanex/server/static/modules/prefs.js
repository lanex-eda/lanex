// prefs.js — the registry of preferences: one list of what LanEx remembers
// about how you like it, with the key it is stored under, its default and how
// it is shown. The Settings page is drawn from this list, "Reset" walks it,
// and nothing else should read a preference key that is not in it.
//
// The values stay where they always were (localStorage, the same keys), so
// the modules that own a preference keep working unchanged.

import { safeStorage } from "./state.js";

// type: "bool" (on/off values), "choice" (options), "custom" (the page draws it)
export const PREFS = [
  // ---- Appearance
  { id: "theme", key: "ll.theme", group: "Appearance", label: "Theme", type: "choice", def: "dark",
    options: [["dark", "Dark"], ["light", "Light"], ["system", "System"]],
    hint: "System follows your computer's light or dark setting." },
  { id: "zoom", key: "ll.zoom", group: "Appearance", label: "Interface size", type: "custom", def: null,
    hint: "Automatic sizes the interface for this screen." },
  { id: "density", key: "ll.density", group: "Appearance", label: "Compact spacing", type: "bool", def: "comfortable", on: "compact", off: "comfortable",
    hint: "Tighter rows and controls, to fit more on screen." },
  // The inline glossary (term.js) and the clock fields (clockfield.js).
  { id: "termUnderline", key: "ll.termUnderline", group: "Appearance", label: "Underline glossary terms", type: "bool", def: "on", on: "on", off: "off",
    hint: "A dotted line under the first label on a page that has a definition. Hover or focus still shows it when this is off." },
  { id: "clockUnit", key: "ll.clockUnit", group: "Appearance", label: "Type the clock in", type: "choice", def: "ns",
    options: [["ns", "Nanoseconds (period)"], ["mhz", "MHz (frequency)"]],
    hint: "The config always stores the period in ns; this only changes how you type and read it." },

  // ---- Runs
  { id: "engine", key: "ll.runMode", group: "Runs", label: "Engine", type: "choice", def: "container",
    options: [["container", "Container"], ["local", "Local tools"]],
    hint: "Container uses the EDA tools in LibreLane's image. Local tools uses the ones installed on this machine." },
  { id: "showFlow", key: "ll.showFlowOnRun", group: "Runs", label: "Show Flow when a run starts", type: "bool", def: "1", on: "1", off: "0",
    hint: "Otherwise you stay on the page you are on." },
  // On by default: runs take 5 to 60 minutes. The browser asks for permission
  // the first time Run is pressed (runend.askNotifyOnRun).
  { id: "notify", key: "ll.notifyRuns", group: "Runs", label: "Tell me when a run finishes", type: "bool", def: "1", on: "1", off: "0",
    hint: "A notification with the result while LanEx is in the background." },
  { id: "chime", key: "ll.runChime", group: "Runs", label: "Play a sound when a run ends", type: "bool", def: "0", on: "1", off: "0" },

  // ---- Editor
  { id: "lintOnSave", key: "ll.lintOnSave", group: "Editor", label: "Check syntax on save", type: "bool", def: "1", on: "1", off: "0",
    hint: "Runs Verilator each time you save a Verilog file." },
  { id: "simTimeLimit", key: "ll.simTimeLimit", group: "Editor", label: "Simulation time limit", type: "choice", def: "120",
    options: [["30", "30 s"], ["120", "2 min"], ["600", "10 min"], ["1800", "30 min"], ["3600", "1 h"]],
    hint: "A testbench that never reaches $finish is stopped after this long. Its waveform so far still shows. A testbench's own Run settings can set another." },

  // ---- Layout (modules/layout.js)
  { id: "layoutKey", key: "ll.lx.key", group: "Layout", label: "Show the key on the layout", type: "bool", def: "1", on: "1", off: "0",
    hint: "The card in the corner of the Layout view that says what each colour is. Its arrow folds it." },
  { id: "layoutRamp", key: "ll.lx.ramp", group: "Layout", label: "Heat map colours", type: "choice", def: "lanex",
    options: [["lanex", "LanEx (brighter is more)"], ["turbo", "OpenROAD (rainbow)"]],
    hint: "LanEx's ramp reads in greyscale and with colour-blindness. OpenROAD's matches its own GUI." },

  // ---- Keyboard
  { id: "keys", key: "ll.keys", group: "Keyboard", label: "Single-key shortcuts", type: "bool", def: "1", on: "1", off: "0",
    hint: "Number keys for pages, r to run, t for the theme, ? for help. Ctrl shortcuts always work." },

  // ---- Updates (modules/updatebanner.js, controller/updatecheck.py)
  { id: "updateCheck", key: "ll.updateCheck", group: "Updates", label: "Check for updates", type: "bool", def: "1", on: "1", off: "0",
    hint: "Once a day LanEx asks GitHub for the newest LanEx release and tells you when there is one. Nothing about you, this computer or your projects is sent." },
];

// What LanEx stores that is state, not preference (the tab you were on, panel
// heights, recent lists, drafts). "Reset every preference" leaves these.
export const STATE_PREFIXES = ["ll.recentDesigns", "ll.tab", "ll.context", "ll.draft:", "ll.rtl:", "ll.wave:", "ll.simok.",
  "ll.guide.", "ll.paletteRecent", "ll.consoleHistory", "ll.runsBaseline", "ll.userPresets", "ll.ranOnce",
  "ll.updateDismissed", "ll.notifyAsked", "ll.bench:", "ll.ideRecent:", "ll.ideSearch", "ll.ideRunSet"];

const BY_ID = Object.fromEntries(PREFS.map((p) => [p.id, p]));

export function pref(id) { return BY_ID[id] || null; }

export function getPref(id) {
  const p = BY_ID[id];
  if (!p) return null;
  const v = safeStorage.get(p.key, null);
  return v === null || v === "" ? p.def : v;
}

export function prefOn(id) {
  const p = BY_ID[id];
  return !!p && getPref(id) === p.on;
}

export function setPref(id, value) {
  const p = BY_ID[id];
  if (!p) return;
  if (value === null || value === undefined) safeStorage.remove(p.key);
  else safeStorage.set(p.key, String(value));
  document.dispatchEvent(new CustomEvent("g:pref", { detail: { id, value } }));
}

// Back to the defaults: every registered preference, and the view options
// (columns, sort, panel sizes) that are kept per machine.
export function resetPrefs() {
  for (const p of PREFS) safeStorage.remove(p.key);
  try {
    const drop = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith("ll.")) continue;
      if (PREFS.some((p) => p.key === k)) continue;
      if (STATE_PREFIXES.some((s) => k.startsWith(s))) continue;
      drop.push(k);
    }
    drop.forEach((k) => localStorage.removeItem(k));
  } catch (_e) {}
}

// Single-key shortcuts can be turned off (WCAG 2.1.4).
export function singleKeysOn() { return getPref("keys") !== "0"; }
