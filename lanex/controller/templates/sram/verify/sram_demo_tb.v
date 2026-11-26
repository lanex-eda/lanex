// sram_demo_tb: writes words into the SRAM (one with a byte mask) and reads
// them back through the registered read port. Prints PASS or FAIL.
`timescale 1ns/1ps
module sram_demo_tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;

    reg         rst_n = 1'b0;
    reg         en = 1'b0;
    reg         we = 1'b0;
    reg  [3:0]  wmask = 4'h0;
    reg  [7:0]  addr = 8'h00;
    reg  [31:0] wdata = 32'h0;
    wire [31:0] rdata;
    integer     errors = 0;
    integer     checks = 0;

    sram_demo dut (.clk(clk), .rst_n(rst_n), .en(en), .we(we), .wmask(wmask),
                   .addr(addr), .wdata(wdata), .rdata(rdata));

    // Inputs change on the falling edge, away from the rising edge the SRAM samples.
    task write(input [7:0] a, input [31:0] d, input [3:0] m);
        begin
            @(negedge clk); en = 1'b1; we = 1'b1; addr = a; wdata = d; wmask = m;
            @(negedge clk); en = 1'b0; we = 1'b0;
        end
    endtask

    // The SRAM takes the address at a rising edge and has the word at the next
    // falling edge; sram_demo registers it at the rising edge after that.
    task check(input [7:0] a, input [31:0] want);
        begin
            @(negedge clk); en = 1'b1; we = 1'b0; addr = a;
            @(negedge clk); en = 1'b0;
            @(negedge clk);
            checks = checks + 1;
            if (rdata !== want) begin
                $display("FAIL: address %0d read %h, wrote %h", a, rdata, want);
                errors = errors + 1;
            end
        end
    endtask

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, sram_demo_tb);
        repeat (2) @(negedge clk);
        rst_n = 1'b1;
        write(8'h00, 32'h01234567, 4'hF);
        write(8'h01, 32'hDEADBEEF, 4'hF);
        write(8'hFF, 32'hA5A5A5A5, 4'hF);
        write(8'h01, 32'h00000000, 4'b0011);      // only the two low bytes change
        check(8'h00, 32'h01234567);
        check(8'h01, 32'hDEAD0000);
        check(8'hFF, 32'hA5A5A5A5);
        if (errors == 0) $display("PASS: sram_demo, %0d words read back as written (byte mask included)", checks);
        else $fatal(1, "FAIL: sram_demo, %0d errors", errors);
        $finish;
    end
endmodule

// For simulation only: a stand-in with the timing of the PDK's own model
// (libs.ref/sky130_sram_macros/verilog): inputs taken at the rising edge, the
// write and the read done at the falling edge. The run uses the real SRAM.
module sky130_sram_1kbyte_1rw1r_32x256_8 (
    input             clk0,
    input             csb0,
    input             web0,
    input      [3:0]  wmask0,
    input      [7:0]  addr0,
    input      [31:0] din0,
    output reg [31:0] dout0,
    input             clk1,
    input             csb1,
    input      [7:0]  addr1,
    output reg [31:0] dout1
);
    reg [31:0] mem [0:255];
    reg        csb0_r = 1'b1;
    reg        web0_r = 1'b1;
    reg [3:0]  wmask0_r = 4'h0;
    reg [7:0]  addr0_r = 8'h00;
    reg [31:0] din0_r = 32'h0;
    reg        csb1_r = 1'b1;
    reg [7:0]  addr1_r = 8'h00;
    integer    k;

    initial begin
        dout0 = 32'h0;
        dout1 = 32'h0;
        for (k = 0; k < 256; k = k + 1) mem[k] = 32'h0;
    end

    always @(posedge clk0) begin
        csb0_r <= csb0; web0_r <= web0; wmask0_r <= wmask0; addr0_r <= addr0; din0_r <= din0;
    end
    always @(negedge clk0) begin
        if (!csb0_r && !web0_r) begin
            if (wmask0_r[0]) mem[addr0_r][7:0]   <= din0_r[7:0];
            if (wmask0_r[1]) mem[addr0_r][15:8]  <= din0_r[15:8];
            if (wmask0_r[2]) mem[addr0_r][23:16] <= din0_r[23:16];
            if (wmask0_r[3]) mem[addr0_r][31:24] <= din0_r[31:24];
        end
        if (!csb0_r && web0_r) dout0 <= mem[addr0_r];
    end
    always @(posedge clk1) begin
        csb1_r <= csb1; addr1_r <= addr1;
    end
    always @(negedge clk1) begin
        if (!csb1_r) dout1 <= mem[addr1_r];
    end
endmodule
