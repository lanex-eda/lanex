/*
 * A Tiny Tapeout tile: the interface every Tiny Tapeout project has.
 *
 * The top module is named tt_um_<something> and has exactly these ports: 8
 * dedicated inputs (ui_in), 8 dedicated outputs (uo_out), 8 bidirectional
 * pins (uio_in, uio_out and uio_oe, where a 1 in uio_oe makes that pin an
 * output), ena (high when this project is the one selected on the chip), clk
 * and rst_n (reset, active low). Every output must be driven.
 *
 * This example is a counter you can load: on every clock it counts up by
 * ui_in[3:0] (so ui_in = 1 counts by one), shows the count on uo_out, and
 * drives the upper 4 bidirectional pins with the count's top bits.
 */
`default_nettype none

module tt_um_lanex_example (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    reg [7:0] count;

    always @(posedge clk) begin
        if (!rst_n)
            count <= 8'd0;
        else
            count <= count + {4'd0, ui_in[3:0]};
    end

    assign uo_out  = count;
    assign uio_out = {count[7:4], 4'b0000};
    assign uio_oe  = 8'b1111_0000;          // the upper 4 are outputs, the lower 4 inputs

    // Inputs this design doesn't use, tied off so lint doesn't warn.
    wire _unused = &{ena, ui_in[7:4], uio_in, 1'b0};
endmodule
