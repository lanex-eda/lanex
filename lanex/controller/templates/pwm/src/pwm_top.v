`include "pwm_defs.vh"

// Two-channel PWM: the top module of a design in two files.
//
// pwm_top (this file) holds the duty registers and instantiates pwm_core
// (src/pwm_core.v) twice. config.json lists both files by name, in order,
// rather than a glob, so a stale copy of a file can never slip into the chip.
// DESIGN_NAME is the top: the one module no other module instantiates.
module pwm_top (
    input  wire                 clk,
    input  wire                 rst,
    input  wire                 load,     // take `value` into the channel `sel`
    input  wire                 sel,
    input  wire [`PWM_BITS-1:0] value,
    output wire                 pwm0,
    output wire                 pwm1
);
    reg [`PWM_BITS-1:0] duty0, duty1;

    always @(posedge clk) begin
        if (rst) begin
            duty0 <= {`PWM_BITS{1'b0}};
            duty1 <= {`PWM_BITS{1'b0}};
        end else if (load) begin
            if (sel) duty1 <= value;
            else     duty0 <= value;
        end
    end

    pwm_core u_ch0 (.clk(clk), .rst(rst), .duty(duty0), .out(pwm0));
    pwm_core u_ch1 (.clk(clk), .rst(rst), .duty(duty1), .out(pwm1));
endmodule
