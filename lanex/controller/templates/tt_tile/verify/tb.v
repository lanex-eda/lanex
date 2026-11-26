`default_nettype none
`timescale 1ns / 1ps
// Testbench for the Tiny Tapeout tile, wired the way Tiny Tapeout's own
// tb.v is (the instance is called user_project). It resets the tile, counts
// by 1 and then by 5, and checks uo_out, the bidirectional outputs and their
// enables every cycle against a model. It ends with PASS, or with FAIL and a
// non-zero exit.
module tb;
    reg  [7:0] ui_in = 8'd0;
    reg  [7:0] uio_in = 8'd0;
    reg        ena = 1'b1;
    reg        clk = 1'b0;
    reg        rst_n = 1'b0;
    wire [7:0] uo_out, uio_out, uio_oe;

    tt_um_lanex_example user_project (
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
        .ena(ena), .clk(clk), .rst_n(rst_n)
    );

    always #10 clk = ~clk;     // 20 ns: Tiny Tapeout's default clock period

    reg [7:0] model = 8'd0;
    always @(posedge clk) model <= !rst_n ? 8'd0 : model + {4'd0, ui_in[3:0]};

    integer errors = 0;
    integer checks = 0;
    integer i;

    task check;
        begin
            checks = checks + 1;
            if (uo_out !== model || uio_out !== {model[7:4], 4'b0} || uio_oe !== 8'hf0) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: uo_out = %0d uio_out = %h uio_oe = %h, expected %0d %h f0",
                         $time, uo_out, uio_out, uio_oe, model, {model[7:4], 4'b0});
            end
        end
    endtask

    initial begin
        $dumpfile("tb.vcd");
        $dumpvars(0, tb);
        // Inputs change on the falling edge, away from the sampling edge.
        repeat (3) @(negedge clk);
        check;
        rst_n = 1'b1;
        ui_in = 8'd1;
        for (i = 0; i < 20; i = i + 1) begin @(negedge clk); check; end
        ui_in = 8'd5;
        for (i = 0; i < 60; i = i + 1) begin @(negedge clk); check; end
        if (errors == 0) $display("PASS: tt_um_lanex_example, %0d cycles checked, the count wrapped past 255", checks);
        else $fatal(1, "FAIL: tt_um_lanex_example, %0d errors", errors);
        $finish;
    end
endmodule
