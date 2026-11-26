// uart_pkg.sv: the transmitter's states, in a SystemVerilog package.
//
// config.json lists this file first, before the module that uses it: a
// package has to be read before anything refers to it.
package uart_pkg;
    typedef enum logic [1:0] {
        IDLE  = 2'd0,
        START = 2'd1,
        DATA  = 2'd2,
        STOP  = 2'd3
    } state_t;
endpackage
