// pwm_defs.vh: settings both PWM files share, kept in one header.
//
// A header is pulled in with `include, never built on its own, so it is not
// in VERILOG_FILES. LibreLane finds it through VERILOG_INCLUDE_DIRS
// (config.json lists src/include), and so do the RTL IDE's lint and simulator.
`ifndef PWM_DEFS_VH
`define PWM_DEFS_VH

// Bits of resolution: the duty cycle is set in 1/256 steps.
`define PWM_BITS 8

`endif
