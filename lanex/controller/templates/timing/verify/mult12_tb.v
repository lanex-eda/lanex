`timescale 1ns / 1ps
// Testbench for mult12. Every cycle it feeds a new pair, and two cycles later
// (one for the input registers, one for the product register) checks the
// product against a model. Pairs: the edges (0, 1, the largest value) and
// 200 random ones. It ends with PASS, or with FAIL and a non-zero exit.
// Simulation has no wire delays: it checks the logic, the run checks timing.
module mult12_tb;
    localparam W = 12;
    reg            clk = 1'b0;
    reg            rst = 1'b1;
    reg  [W-1:0]   a = 0, b = 0;
    wire [2*W-1:0] p;

    mult12 #(.W(W)) dut (.clk(clk), .rst(rst), .a(a), .b(b), .p(p));

    always #5 clk = ~clk;

    // The model: the pairs fed in, two cycles back.
    reg [2*W-1:0] want1 = 0, want2 = 0;
    reg           valid1 = 1'b0, valid2 = 1'b0;
    always @(posedge clk) begin
        want2  <= want1;  valid2 <= valid1;
        want1  <= a * b;  valid1 <= !rst;
    end

    integer errors = 0;
    integer checks = 0;
    integer i;

    task feed(input [W-1:0] x, input [W-1:0] y);
        begin
            // Inputs change on the falling edge, away from the sampling edge.
            @(negedge clk);
            if (valid2) begin
                checks = checks + 1;
                if (p !== want2) begin
                    errors = errors + 1;
                    $display("FAIL at %0t ns: p = %0d, expected %0d", $time, p, want2);
                end
            end
            a = x; b = y;
        end
    endtask

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, mult12_tb);
        repeat (2) @(negedge clk);
        rst = 1'b0;
        feed(0, 0); feed(1, 1); feed({W{1'b1}}, 1); feed(1, {W{1'b1}}); feed({W{1'b1}}, {W{1'b1}});
        for (i = 0; i < 200; i = i + 1) feed($random, $random);
        feed(0, 0); feed(0, 0); feed(0, 0);       // let the last products out
        if (errors == 0) $display("PASS: mult12, %0d products checked two cycles after their inputs", checks);
        else $fatal(1, "FAIL: mult12, %0d errors", errors);
        $finish;
    end
endmodule
