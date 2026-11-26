`timescale 1ns / 1ps
// Testbench for blinky. It runs the design next to a model of its counter,
// checks the LED against the model on every cycle and ends with PASS, or
// with FAIL and a non-zero exit when the design got it wrong.
module blinky_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    reg  clk = 1'b0;
    reg  rst = 1'b1;
    wire led;

    // Small WIDTH so the LED toggles quickly in simulation.
    blinky #(.WIDTH(4)) dut (.clk(clk), .rst(rst), .led(led));

    always #5 clk = ~clk;

    // The model: what the counter holds after each rising edge.
    reg [3:0] model = 4'd0;
    always @(posedge clk) model <= rst ? 4'd0 : model + 4'd1;

    integer errors  = 0;
    integer toggles = 0;
    integer i;
    reg     last    = 1'b0;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, blinky_tb);
        // Inputs change on the falling edge, half a clock away from the
        // rising edge the design samples on, so nothing races.
        repeat (2) @(negedge clk);
        rst = 1'b0;
        for (i = 0; i < 40; i = i + 1) begin
            @(negedge clk);
            if (led !== model[3]) begin
                errors = errors + 1;
                $display("FAIL at %t: led = %b, expected %b", $realtime, led, model[3]);
            end
            if (led !== last) toggles = toggles + 1;
            last = led;
        end
        if (toggles < 4) begin
            errors = errors + 1;
            $display("FAIL: the LED toggled %0d times in 40 cycles, expected 5", toggles);
        end
        if (errors == 0) $display("PASS: blinky, the LED followed the counter for 40 cycles (%0d toggles)", toggles);
        else $fatal(1, "FAIL: blinky, %0d errors", errors);
        $finish;
    end
endmodule
