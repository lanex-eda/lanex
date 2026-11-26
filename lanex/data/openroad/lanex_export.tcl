# Copyright 2026 LanEx Contributors
# SPDX-License-Identifier: Apache-2.0
#
# LanEx's analysis export (controller/odbexport.py): one headless OpenROAD
# session on one stage's database, in the LibreLane container. It writes what
# the Layout tab draws over the chip:
#   heat-Placement.csv   cell density per tile (%)
#   heat-RUDY.csv        wire demand estimated from net boxes (%)
#   heat-Routing*.csv    global-route congestion per GCell (%), all layers and per layer
#   paths-setup.json     worst setup paths, report_checks -format json
#   paths-hold.json      worst hold paths
#   clocktree.tsv        every clock net: port, driver and sinks with boxes and arrival
#
# Run: QT_QPA_PLATFORM=offscreen openroad -no_init -no_splash -gui -no_settings lanex_export.tcl
# (gui::dump_heatmap needs the GUI in OpenROAD of LibreLane 3.0.4; offscreen it
# opens no window). Inputs come from the environment: ODB OUT LIBS SDCF SPEF
# PROPAGATE GRID. In -gui mode an uncaught error leaves OpenROAD waiting
# forever, so everything is inside one catch and the script always exits.

proc lx_ms {} { return [clock milliseconds] }
proc lx_log {msg} { puts "LANEX: $msg"; flush stdout }
proc lx_env {name} { if {[info exists ::env($name)]} { return $::env($name) }; return "" }
# The routing layers from lo to hi (tech order); "" = from the first / to the last.
proc lx_layers {lo hi} {
  set names {}
  set on [expr {$lo eq ""}]
  foreach L [[ord::get_db_tech] getLayers] {
    if {[$L getType] ne "ROUTING"} continue
    set n [$L getName]
    if {$n eq $lo} { set on 1 }
    if {$on} { lappend names $n }
    if {$n eq $hi} { break }
  }
  return $names
}

set T [lx_ms]
set code 0
if {[catch {
  set out [lx_env OUT]
  file mkdir $out
  lx_log "step load"
  read_db [lx_env ODB]
  set have_lib 0
  foreach lib [lx_env LIBS] {
    if {[catch {read_liberty $lib} e]} { lx_log "warn liberty $lib: $e" } else { set have_lib 1 }
  }
  set have_sdc 0
  if {[lx_env SDCF] ne "" && $have_lib} {
    if {[catch {read_sdc [lx_env SDCF]} e]} { lx_log "warn sdc: $e" } else { set have_sdc 1 }
  }
  set have_spef 0
  if {[lx_env SPEF] ne "" && $have_lib} {
    if {[catch {read_spef [lx_env SPEF]} e]} { lx_log "warn spef: $e" } else { set have_spef 1 }
  }
  if {$have_sdc && [lx_env PROPAGATE] eq "1"} { catch {set_propagated_clock [all_clocks]} }
  if {$have_sdc && !$have_spef} {
    # Before extraction: wire delay estimated from placement, with the wire
    # RC of the routing layers the run may use (as LibreLane's mid-flow STA).
    set sl [lx_layers [lx_env RT_MIN] [lx_env RT_MAX]]
    set cl [lx_layers [lx_env RT_CLOCK_MIN] [lx_env RT_MAX]]
    if {[llength $sl]} { catch {set_wire_rc -signal -layers $sl} }
    if {[llength $cl]} { catch {set_wire_rc -clock -layers $cl} }
    if {[catch {estimate_parasitics -placement} e]} { lx_log "warn parasitics: $e" } else { lx_log "parasitics estimated" }
  }
  lx_log "loaded [expr {[lx_ms]-$T}] ms lib=$have_lib sdc=$have_sdc spef=$have_spef"

  set block [ord::get_db_block]
  set dbu [[ord::get_db_tech] getDbUnitsPerMicron]
  set die [$block getDieArea]
  set W [expr {([$die xMax]-[$die xMin])/double($dbu)}]
  set H [expr {([$die yMax]-[$die yMin])/double($dbu)}]
  set rowh 0
  set row [lindex [$block getRows] 0]
  if {$row ne ""} { set rowh [expr {[[$row getSite] getHeight]/double($dbu)}] }
  # About 120 tiles on the long side, never smaller than one cell row.
  set grid [expr {max($rowh, max($W,$H)/120.0)}]
  if {$grid <= 0} { set grid 10 }
  if {[lx_env GRID] ne ""} { set grid [lx_env GRID] }
  lx_log "die $W $H row $rowh grid $grid"

  lx_log "step heat"
  foreach {name opts} [list Placement [list GridX $grid GridY $grid Filler 0 Taps 0 IO 0] \
                            RUDY [list GridX $grid GridY $grid] \
                            Routing [list]] {
    set t0 [lx_ms]
    foreach {k v} $opts { catch {gui::set_heatmap $name $k $v} }
    catch {gui::set_heatmap $name rebuild}
    if {[catch {gui::dump_heatmap $name $out/heat-$name.csv} e]} {
      lx_log "unavailable $name: $e"
    } else {
      lx_log "heat $name [expr {[lx_ms]-$t0}] ms"
    }
  }
  if {[file exists $out/heat-Routing.csv]} {
    foreach L [[ord::get_db_tech] getLayers] {
      if {[$L getType] ne "ROUTING"} continue
      set ln [$L getName]
      if {[catch {gui::set_heatmap Routing Layer $ln; gui::set_heatmap Routing rebuild; gui::dump_heatmap Routing $out/heat-Routing@$ln.csv} e]} {
        lx_log "warn routing layer $ln: $e"
      }
    }
    catch {gui::set_heatmap Routing Layer All}
  }

  if {$have_sdc} {
    lx_log "step paths"
    set t0 [lx_ms]
    if {[catch {
      report_checks -format json -group_path_count 25 -endpoint_path_count 1 -unique_paths_to_endpoint -path_delay max > $out/paths-setup.json
      report_checks -format json -group_path_count 25 -endpoint_path_count 1 -unique_paths_to_endpoint -path_delay min > $out/paths-hold.json
    } e]} { lx_log "warn paths: $e" } else { lx_log "paths [expr {[lx_ms]-$t0}] ms" }
  } else {
    lx_log "unavailable paths: no constraints (SDC) or Liberty for this stage"
  }

  lx_log "step clock"
  set t0 [lx_ms]
  set f [open $out/clocktree.tsv w]
  puts $f "net\trole\tpin\tinst\tcell\tx0\ty0\tx1\ty1\tarrival_ns\tslew_ns"
  foreach net [$block getNets] {
    if {[$net getSigType] ne "CLOCK"} continue
    set nn [$net getName]
    foreach bt [$net getBTerms] {
      set bb [$bt getBBox]
      puts $f "$nn\tport\t[$bt getName]\t\tPORT\t[expr {[$bb xMin]/double($dbu)}]\t[expr {[$bb yMin]/double($dbu)}]\t[expr {[$bb xMax]/double($dbu)}]\t[expr {[$bb yMax]/double($dbu)}]\t\t"
    }
    foreach it [$net getITerms] {
      set inst [$it getInst]
      set bb [$inst getBBox]
      set iname [$inst getName]
      set pn "$iname/[[$it getMTerm] getName]"
      set role [expr {[$it isOutputSignal] ? "driver" : "sink"}]
      set arr ""
      set sl ""
      if {$have_sdc} {
        catch {set arr [get_property [get_pins $pn] arrival_max_rise]}
        catch {set sl [get_property [get_pins $pn] slew_max]}
      }
      puts $f "$nn\t$role\t$pn\t$iname\t[[$inst getMaster] getName]\t[expr {[$bb xMin]/double($dbu)}]\t[expr {[$bb yMin]/double($dbu)}]\t[expr {[$bb xMax]/double($dbu)}]\t[expr {[$bb yMax]/double($dbu)}]\t$arr\t$sl"
    }
  }
  close $f
  lx_log "clock [expr {[lx_ms]-$t0}] ms"
  lx_log "done [expr {[lx_ms]-$T}] ms"
} err]} {
  lx_log "FAILED: $err"
  set code 1
}
exit $code
