`include "pwm_defs.vh"

// One PWM channel: a free-running counter compared with the duty value.
// The output is high for `duty` counts out of every 2^PWM_BITS.
module pwm_core (
    input  wire                 clk,
    input  wire                 rst,
    input  wire [`PWM_BITS-1:0] duty,
    output reg                  out
);
    reg [`PWM_BITS-1:0] count;

    always @(posedge clk) begin
        if (rst) begin
            count <= {`PWM_BITS{1'b0}};
            out   <= 1'b0;
        end else begin
            count <= count + 1'b1;
            out   <= (count < duty);
        end
    end
endmodule
