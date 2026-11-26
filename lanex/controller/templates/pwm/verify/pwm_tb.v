`timescale 1ns / 1ps
`include "pwm_defs.vh"
// Testbench for pwm_top. It loads a duty value into each channel, counts how
// many of the next 256 cycles each output is high, and checks the count is the
// duty value exactly: 25 %, 78 %, never and almost always, on both channels
// at once. It ends with PASS, or with FAIL and a non-zero exit.
module pwm_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    reg        clk = 1'b0;
    reg        rst = 1'b1;
    reg        load = 1'b0;
    reg        sel = 1'b0;
    reg  [7:0] value = 8'd0;
    wire       pwm0, pwm1;

    pwm_top dut (.clk(clk), .rst(rst), .load(load), .sel(sel), .value(value), .pwm0(pwm0), .pwm1(pwm1));

    always #5 clk = ~clk;

    integer errors = 0;
    integer checks = 0;
    integer high0, high1, i;

    // Inputs change on the falling edge, away from the rising edge the
    // design samples on.
    task set_duty(input ch, input [7:0] v);
        begin
            @(negedge clk); sel = ch; value = v; load = 1'b1;
            @(negedge clk); load = 1'b0;
        end
    endtask

    task measure(input [7:0] want0, input [7:0] want1);
        begin
            // One whole period to settle, then count one period.
            repeat (256) @(negedge clk);
            high0 = 0; high1 = 0;
            for (i = 0; i < 256; i = i + 1) begin
                @(negedge clk);
                high0 = high0 + pwm0;
                high1 = high1 + pwm1;
            end
            checks = checks + 2;
            if (high0 != want0) begin
                errors = errors + 1;
                $display("FAIL at %t: pwm0 was high %0d of 256 cycles, expected %0d", $realtime, high0, want0);
            end
            if (high1 != want1) begin
                errors = errors + 1;
                $display("FAIL at %t: pwm1 was high %0d of 256 cycles, expected %0d", $realtime, high1, want1);
            end
        end
    endtask

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, pwm_tb);
        repeat (2) @(negedge clk);
        rst = 1'b0;
        set_duty(1'b0, 8'd64);  set_duty(1'b1, 8'd200); measure(8'd64, 8'd200);
        set_duty(1'b0, 8'd0);   set_duty(1'b1, 8'd255); measure(8'd0, 8'd255);
        if (errors == 0) $display("PASS: pwm, %0d duty cycles measured over whole periods (%0d-bit)", checks, `PWM_BITS);
        else $fatal(1, "FAIL: pwm, %0d errors", errors);
        $finish;
    end
endmodule
