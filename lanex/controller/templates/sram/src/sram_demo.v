// sram_demo: one sky130_sram_1kbyte_1rw1r_32x256_8 behind a registered read port.
// Port 0 is read/write, port 1 (read-only) is switched off.
module sram_demo (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        we,
    input  wire [3:0]  wmask,
    input  wire [7:0]  addr,
    input  wire [31:0] wdata,
    output reg  [31:0] rdata
);
    wire [31:0] dout0;
    wire [31:0] dout1;

    sky130_sram_1kbyte_1rw1r_32x256_8 u_sram (
        .clk0  (clk),
        .csb0  (~en),
        .web0  (~we),
        .wmask0(wmask),
        .addr0 (addr),
        .din0  (wdata),
        .dout0 (dout0),
        .clk1  (1'b0),
        .csb1  (1'b1),
        .addr1 (8'h00),
        .dout1 (dout1)
    );

    always @(posedge clk) begin
        if (!rst_n) rdata <= 32'h0;
        else        rdata <= dout0;
    end
endmodule
