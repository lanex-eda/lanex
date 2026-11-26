`timescale 1ns / 1ps
// Testbench for mult. It checks the product of the corner cases and of 32
// random pairs, one clock after each pair goes in, and ends with PASS, or
// with FAIL and a non-zero exit when a product is wrong.
module mult_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    reg         clk = 1'b0;
    reg         rst = 1'b1;
    reg  [7:0]  a = 8'd0, b = 8'd0;
    wire [15:0] p;

    mult dut (.clk(clk), .rst(rst), .a(a), .b(b), .p(p));

    always #5 clk = ~clk;

    integer    errors = 0;
    integer    checked = 0;
    integer    i;
    integer    seed = 1;
    reg [15:0] want;
    reg [31:0] r;

    // Put a pair in between clock edges (a blocking assignment right after a
    // rising edge would race the register reading it), then check the
    // product one rising edge later.
    task try(input [7:0] x, input [7:0] y);
        begin
            a = x;
            b = y;
            want = x * y;
            @(negedge clk);
            checked = checked + 1;
            if (p !== want) begin
                errors = errors + 1;
                $display("FAIL at %t: %0d * %0d = %0d, expected %0d", $realtime, x, y, p, want);
            end
        end
    endtask

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, mult_tb);
        repeat (2) @(negedge clk);
        if (p !== 16'd0) begin errors = errors + 1; $display("FAIL: p = %0d in reset, expected 0", p); end
        rst = 1'b0;
        try(8'd7, 8'd6);
        try(8'd15, 8'd15);
        try(8'd200, 8'd3);
        try(8'd0, 8'd123);
        try(8'd1, 8'd255);
        try(8'd255, 8'd255);
        try(8'd128, 8'd2);
        for (i = 0; i < 32; i = i + 1) begin
            r = $random(seed);
            try(r[7:0], r[15:8]);
        end
        if (errors == 0) $display("PASS: mult, %0d products checked", checked);
        else $fatal(1, "FAIL: mult, %0d of %0d products wrong", errors, checked);
        #20 $finish;
    end
endmodule
