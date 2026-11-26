`timescale 1ns / 1ps
// Testbench for seg7. It drives all 16 input values, checks the segments of
// each against a table written out by hand, and ends with PASS, or with FAIL
// and a non-zero exit when a segment is wrong. There is no clock: each value
// is held for 10 ns and checked after the logic has settled.
module seg7_tb;
    reg  [3:0] hex;          // starts unknown, so the first value is a change the logic sees
    wire [6:0] seg;

    seg7 dut (.hex(hex), .seg(seg));

    // The expected segments, gfedcba, for 0 to F.
    reg [6:0] want [0:15];
    initial begin
        want[0]  = 7'h3f; want[1]  = 7'h06; want[2]  = 7'h5b; want[3]  = 7'h4f;
        want[4]  = 7'h66; want[5]  = 7'h6d; want[6]  = 7'h7d; want[7]  = 7'h07;
        want[8]  = 7'h7f; want[9]  = 7'h6f; want[10] = 7'h77; want[11] = 7'h7c;
        want[12] = 7'h39; want[13] = 7'h5e; want[14] = 7'h79; want[15] = 7'h71;
    end

    integer errors = 0;
    integer i;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, seg7_tb);
        for (i = 0; i < 16; i = i + 1) begin
            hex = i[3:0];
            #10;
            if (seg !== want[i]) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: hex = %h, seg = %b, expected %b", $time, hex, seg, want[i]);
            end
        end
        if (errors == 0) $display("PASS: seg7, all 16 digits show the right segments");
        else $fatal(1, "FAIL: seg7, %0d errors", errors);
        $finish;
    end
endmodule
