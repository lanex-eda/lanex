# Can LanEx take a design to real chips? (audit, 25 Nov 2026)

Question from Akshat: can someone use LanEx to design, verify and send files that
meet every requirement of the shuttles that run worldwide (Tiny Tapeout, IHP, …),
and can someone use it for a direct foundry run with foundry pad frames, pads and
macros? If yes, how, and is it easy?

**Short answer.** LanEx does the core of every submission today: write and simulate
RTL, harden it to GDS with LibreLane on sky130, gf180mcu and IHP sg13g2, and read
LibreLane's own DRC, LVS, antenna and timing checks. **No shuttle can be submitted
to from LanEx alone yet.** Each program wants a fixed wrapper or template, runs its
own precheck, and wants its own package; LanEx does none of those three. For
Tiny Tapeout the gap is small and worth closing first. A direct foundry run is
possible for an expert who writes LibreLane's Chip-flow settings by hand, but it is
not easy or guided, and seal ring and fill are missing for all three PDKs.

Sources: the program pages and repositories listed under each section, read on
25 Nov 2026; the three PDKs' LibreLane configs (open_pdks sky130 and gf180mcu,
IHP-Open-PDK sg13g2); LanEx's own code. Anything not confirmed from a primary
source is marked *unconfirmed*.

## What LanEx can do today

| Capability | Status | Where |
|---|---|---|
| RTL editing, lint, simulation, waveforms | yes | RTL IDE, GTKWave hand-off |
| RTL to GDS with LibreLane 3.0.4 (Classic flow) | yes | Flow, Runs |
| Any LibreLane flow the config names, including **Chip** (pad ring) | yes, if the config says `meta.flow: Chip` | `configcheck.flow_names`, Setup shows the flow |
| Any LibreLane variable (PAD_SOUTH…, DIE_AREA, FP_PIN_ORDER_CFG, …) | yes, typed by hand | All settings |
| Hard macros (foundry IP, SRAM): GDS + LEF + lib + placement | yes | Cells & macros (`custommacros.py`, MACROS overlay) |
| Sign-off checks LibreLane runs: Magic + KLayout DRC with the PDK deck, Netgen LVS, antenna, STA in every corner | yes, reported per check | Verify |
| Seal ring, metal fill for a full chip | **no** on all three PDKs (see below) | — |
| A shuttle's wrapper / template (TT tile, Caravel, wafer.space slot, IHP repo) | **no** | — |
| A shuttle's own precheck | **no**; LanEx says plainly it is not a sign-off tool | Verify page text |
| Export a submission package (info.yaml, doc/info.json, wrapper GDS repo) | **no** | — |

Seal ring and fill, checked in each PDK's `librelane/config.tcl`: all three define
the pad cell library (sky130_ef_io, gf180mcu_fd_io, sg13g2_io) and a KLayout DRC
deck, but **none sets `KLAYOUT_SEALRING_SCRIPT` or `KLAYOUT_FILLER_SCRIPT`**, so the
Chip flow's KLayout.SealRing and KLayout.Filler steps do nothing. IHP adds a density
deck and a Magic fill script; sky130's deck runs with `seal 1 offgrid 1`.

## Shuttle by shuttle

### Tiny Tapeout (sky130, gf180 via wafer.space, IHP)
Open now: TTSKY26d/e close **30 Nov 2026**, TTGF26c/d close **7 Dec 2026**; next IHP
in Mar 2027. Sources: tinytapeout.com/chips, /faq, /specs/analog,
TinyTapeout/tt-gds-action, ttsky-verilog-template.

- **What TT wants:** a GitHub repo made from their template: RTL in `src/`, a top
  module named `tt_um_<name>` with the fixed pins (`ui_in[7:0]`, `uo_out[7:0]`,
  `uio_in/out/oe[7:0]`, `ena`, `clk`, `rst_n`), `info.yaml` (yaml_version 6: tiles,
  top module, clock, pinout) and `docs/info.md`. **TT's own CI hardens it** with
  LibreLane **3.0.14** and runs its precheck and a gate-level test; the person does
  not hand in GDS (except analog tiles).
- **With LanEx today:** usable for the design work. Write the `tt_um_` module, simulate
  it, harden it to see it fits and meets timing, then copy `src/` into a TT template
  repo by hand and push. LanEx gives no tile-sized floorplan, no TT pin layout, no
  info.yaml and no export, and its LibreLane (3.0.4) differs from TT's (3.0.14), so
  its area/timing answer is close to TT's but not the same run.
- **To make it one click (best value, about 2-3 days):** a "Tiny Tapeout" project
  type per PDK (the fixed ports, the die of the chosen tile size, TT's pin order and
  60 % density), an info.yaml form, "Export for Tiny Tapeout" that writes the repo
  files, and TT's precheck run locally on the result.

### wafer.space (gf180mcuD, Run 3)
Sources: wafer-space/gf180mcu-project-template, wafer-space/gf180mcu-precheck.

- **What it wants:** one GDS of the whole chip, exactly the slot size (1x1 is
  3.88 x 5.07 mm usable), max Metal5, their ID/marker cells, the template's pad ring
  with pad count and power pads unchanged, and a clean `gf180mcu-precheck` (density,
  KLayout antenna, Magic DRC, KLayout DRC, one top cell, dbu 0.001). The person
  hardens it; the template uses LibreLane's Chip flow on a forked LibreLane branch.
- **With LanEx today:** not practical. The Chip flow runs if the config is written by
  hand, but there is no slot template, no ID cells, and no precheck. The template's
  forked LibreLane may not match LanEx's (*unconfirmed which changes it carries*).
- **To support it (about a week):** a slot-template project type built from
  wafer.space's template, and the precheck run from Verify.

### IHP Open-Source MPW (SG13G2)
Next SG13G2 run: registration by **29 Mar 2027**, 2,800 EUR/mm². Sources:
IHP-GmbH/Open-Silicon-MPW (Submission-process, IP-development-steps, issues #63, #68),
IHP-Open-PDK docs.

- **What it wants:** an Apache-2.0 repo in IHP's layout (`doc/info.json`, a TRL
  document, `release/<ver>/` with GDS and netlist), a **seal ring (mandatory)**,
  metal fill, DRC clean with IHP's KLayout deck including the minimal precheck rules,
  LVS clean, a signed agreement and a GitHub issue to submit.
- **With LanEx today:** the core block can be hardened and LibreLane's IHP DRC deck
  runs, but there is no seal ring or fill, no IHP precheck-level DRC run as its own
  step, and no package. Known sharp edge: LVS with IHP's pad cells passes only in
  "projected-reference" mode (issue #68), and the reference IHP chip template turns
  top-level DRC off because the pad ring trips `metal1_pin_offgrid`.
- **To support it (about a week, after the Chip-flow work below):** IHP seal ring and
  fill steps, the precheck deck run, and an export of IHP's repo layout.

### ChipFoundry chipIgnite (sky130, Caravel / OpenFrame)
CI-2612 shuttle in Dec 2026 at $14,950 per project (from a search result; the page
itself was not read). Sources: chipfoundry.io/faqs, chipfoundry/cf-cli,
chipfoundry/cf-precheck.

- **What it wants:** a repo with exactly one wrapper GDS (`user_project_wrapper` etc.)
  and `verilog/rtl/user_defines.v`, passing `cf-precheck` (XOR against the golden
  wrapper, LVS, density, KLayout FEOL/BEOL/off-grid, OEB, …). ChipFoundry's tools
  harden with **LibreLane 2**.
- **With LanEx today:** no Caravel wrapper template, no cf-precheck, and LanEx runs
  LibreLane 3. Whether a LibreLane 3 wrapper passes their XOR and LVS is
  *unconfirmed*.
- **To support it (3-4 days, after confirming LibreLane 3 is accepted):** a wrapper
  project type and the cf-precheck run.

## Direct foundry runs (pad frames, foundry pads and macros)

- **What exists:** LibreLane's Chip flow places a pad ring (`PAD_SOUTH/EAST/NORTH/WEST`
  instance lists, `PAD_CFG`, `PDN_CORE_RING_CONNECT_TO_PADS`), and every PDK's config
  points at its pad library. LanEx runs that flow when the config names it, takes any
  variable through All settings, and adds foundry macros (GDS + LEF + lib + placement)
  through Cells & macros.
- **What is missing:**
  1. A pad-ring editor: today pads per side are typed as name lists, with no picture of
     the frame until a run finishes.
  2. Seal ring and fill on all three PDKs (above).
  3. A foundry-provided frame used as one macro does not fit the PadRing step, which
     expects PDK pad cells. Such a frame has to be a placed macro with the core inside
     by hand.
  4. The foundry's tapeout deck as its own step: LibreLane runs the PDK's standard deck,
     not a foundry checklist (for example the GF180 design manual's tapeout checklist,
     not read here).
  5. Bonding diagram and pinout export.
- **Verdict:** possible for someone who already knows LibreLane's Chip flow; not
  convenient or intuitive for a student. The reference full-chip examples are the
  wafer.space template (gf180) and iic-jku's ihp-sg13g2-ams-chip-template (IHP, with a
  bond-plan generator).

## Recommended order of work

| # | Work | Why first | Size |
|---|---|---|---|
| 1 | Tiny Tapeout project type + info.yaml + export + local TT precheck | Biggest audience, cheapest chips, shuttles every month; smallest gap | 2-3 days |
| 2 | "Shuttle precheck" step on Verify (TT, gf180mcu-precheck, cf-precheck, IHP precheck deck, run in the container) | Turns "LibreLane says clean" into "the program says clean" | 2 days |
| 3 | Chip-flow builder: pad-ring editor with a picture, seal ring and fill per PDK | Unlocks wafer.space, IHP and direct runs | 1-2 weeks |
| 4 | IHP package export (doc/info.json, release/) | Small once #3 exists | 1 day |
| 5 | Caravel/OpenFrame wrapper + cf export | After ChipFoundry confirms LibreLane 3 output | 3-4 days |
| — | Move LanEx's pinned LibreLane from 3.0.4 toward TT's 3.0.14 | Same results as the TT CI; needs its own test pass | separate |

## Not confirmed

- TT precheck's exact DRC deck and the use of tt-gds-action's `custom_gds` folder.
- Whether ChipFoundry accepts a LibreLane 3 hardened wrapper.
- Whether IHP mandates a pad ring; the `doc/info.json` schema.
- The wafer.space direct Run 3 slot deadline and which changes its forked LibreLane has.
- Google/SkyWater, GlobalFoundries open MPWs and Indian (SCL/IIT) programs were not
  researched.
