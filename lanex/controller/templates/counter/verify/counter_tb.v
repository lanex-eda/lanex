`timescale 1ns / 1ps
// Testbench for counter. It counts with enable on, holds with it off, wraps
// past 255 and resets mid-count, checks every cycle against a model, and
// ends with PASS, or with FAIL and a non-zero exit when a value is wrong.
module counter_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    reg        clk = 1'b0;
    reg        rst = 1'b1;
    reg        en  = 1'b0;
    wire [7:0] count;

    counter dut (.clk(clk), .rst(rst), .en(en), .count(count));

    always #5 clk = ~clk;

    // The model: what the counter must hold after each rising edge.
    reg [7:0] expected = 8'd0;
    always @(posedge clk) expected <= rst ? 8'd0 : (en ? expected + 8'd1 : expected);

    integer errors = 0;
    integer i;

    task check;
        begin
            if (count !== expected) begin
                errors = errors + 1;
                $display("FAIL at %t: count = %0d, expected %0d", $realtime, count, expected);
            end
        end
    endtask

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, counter_tb);
        // Inputs change on the falling edge, half a clock away from the
        // rising edge the counter samples on, so nothing races.
        repeat (2) @(negedge clk);
        check;                                   // held at 0 in reset
        rst = 1'b0;
        en  = 1'b1;
        for (i = 0; i < 20; i = i + 1) begin @(negedge clk); check; end
        en = 1'b0;                               // enable off: it holds
        for (i = 0; i < 5; i = i + 1) begin @(negedge clk); check; end
        en = 1'b1;                               // on past 255: it wraps to 0
        for (i = 0; i < 250; i = i + 1) begin @(negedge clk); check; end
        rst = 1'b1;                              // reset in the middle of a count
        @(negedge clk); check;
        rst = 1'b0;
        for (i = 0; i < 3; i = i + 1) begin @(negedge clk); check; end
        $display("final count = %0d", count);
        if (errors == 0) $display("PASS: counter, %0d cycles checked, wrapped past 255", 2 + 20 + 5 + 250 + 1 + 3);
        else $fatal(1, "FAIL: counter, %0d errors", errors);
        $finish;
    end
endmodule
