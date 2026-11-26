`timescale 1ns / 1ps
// Testbench for fifo. It fills the FIFO to full, tries one write too many,
// reads everything back in order, reads once too many, then writes and reads
// in the same cycle. Every value and flag is checked; the run ends with PASS,
// or with FAIL and a non-zero exit when something is wrong.
module fifo_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    reg        clk = 1'b0;
    reg        rst = 1'b1;
    reg        wr_en = 1'b0, rd_en = 1'b0;
    reg  [7:0] din = 8'h00;
    wire [7:0] dout;
    wire       full, empty;

    fifo dut (.clk(clk), .rst(rst), .wr_en(wr_en), .rd_en(rd_en),
              .din(din), .dout(dout), .full(full), .empty(empty));

    always #6 clk = ~clk;

    integer   errors = 0;
    integer   i;
    reg [7:0] want;

    task fail(input [8*64-1:0] what);
        begin
            errors = errors + 1;
            $display("FAIL at %t: %0s (dout = %h, full = %b, empty = %b)", $realtime, what, dout, full, empty);
        end
    endtask

    // Inputs change on the falling edge, half a clock away from the rising
    // edge the FIFO samples on: set right after a rising edge with blocking
    // assignments, they would race the design's own reads of them.
    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, fifo_tb);
        repeat (2) @(negedge clk);
        rst = 1'b0;
        @(negedge clk);
        if (!empty || full) fail("empty after reset");

        // Fill it: 16 writes; full only after the last one.
        wr_en = 1'b1;
        for (i = 0; i < 16; i = i + 1) begin
            din = 8'hA0 + i[7:0];
            @(negedge clk);
            if (empty) fail("not empty after a write");
            if (full !== (i == 15)) fail("full exactly when 16 are in");
        end
        din = 8'hFF;                       // one write too many: ignored
        @(negedge clk);
        if (!full) fail("still full after a write while full");
        wr_en = 1'b0;

        // Read all 16 back, in the order they went in.
        rd_en = 1'b1;
        for (i = 0; i < 16; i = i + 1) begin
            @(negedge clk);
            want = 8'hA0 + i[7:0];
            if (dout !== want) fail("values come out in order");
            if (full) fail("not full after a read");
        end
        rd_en = 1'b0;
        @(negedge clk);
        if (!empty) fail("empty after 16 reads");
        rd_en = 1'b1;                      // one read too many: dout holds
        @(negedge clk);
        if (dout !== 8'hAF || !empty) fail("a read while empty changes nothing");
        rd_en = 1'b0;

        // A write and a read in the same cycle keep the count.
        wr_en = 1'b1; din = 8'h11;
        @(negedge clk);
        wr_en = 1'b1; rd_en = 1'b1; din = 8'h22;
        @(negedge clk);
        if (dout !== 8'h11) fail("first in, first out with a read and a write together");
        if (empty || full) fail("one value left after a read and a write together");
        wr_en = 1'b0; rd_en = 1'b1;
        @(negedge clk);
        rd_en = 1'b0;
        if (dout !== 8'h22 || !empty) fail("the last value out, then empty");

        if (errors == 0) $display("PASS: fifo, 16 deep, order and flags checked");
        else $fatal(1, "FAIL: fifo, %0d errors", errors);
        #20 $finish;
    end
endmodule
