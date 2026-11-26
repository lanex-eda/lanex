// Timing exercise: a 12 x 12 multiplier between two rows of registers.
//
// The inputs are registered, then multiplied, then the product is registered:
// the slowest path runs from an input register through the whole multiplier
// to an output register. At a 5 ns clock (200 MHz) that path is too long for
// sky130 and the run fails setup timing, on purpose. Read the failure card,
// then find a clock period the design meets (10 ns does), or try a DELAY
// synthesis goal, or split the multiply over two clock cycles (pipelining).
module mult12 #(
    parameter W = 12
) (
    input  wire           clk,
    input  wire           rst,
    input  wire [W-1:0]   a,
    input  wire [W-1:0]   b,
    output reg  [2*W-1:0] p
);
    reg [W-1:0] ra, rb;

    always @(posedge clk) begin
        if (rst) begin
            ra <= {W{1'b0}};
            rb <= {W{1'b0}};
            p  <= {(2*W){1'b0}};
        end else begin
            ra <= a;
            rb <= b;
            p  <= ra * rb;
        end
    end
endmodule
