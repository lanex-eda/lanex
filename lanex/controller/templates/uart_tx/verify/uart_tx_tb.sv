`timescale 1ns / 1ps
// Testbench for uart_tx. It sends four bytes and decodes the serial line the
// way a receiver would: it waits for the start bit's falling edge, samples
// each bit in the middle, and checks the start bit is low, the 8 data bits
// arrive least significant first and the stop bit is high. It ends with PASS,
// or with FAIL and a non-zero exit. A watchdog stops a bench that hangs.
module uart_tx_tb;
    localparam int CPB = 4;            // clocks per bit: short, for a quick simulation

    logic       clk = 1'b0;
    logic       rst = 1'b1;
    logic       send = 1'b0;
    logic [7:0] data = 8'h00;
    logic       tx, busy;

    uart_tx #(.CLKS_PER_BIT(CPB)) dut (.clk(clk), .rst(rst), .send(send), .data(data), .tx(tx), .busy(busy));

    always #5 clk = ~clk;

    integer errors = 0;
    integer sent = 0;
    integer i;
    logic [7:0] got;

    // Send one byte and read it back off the line.
    task send_byte(input [7:0] b);
        begin
            @(negedge clk); data = b; send = 1'b1;
            @(negedge clk); send = 1'b0;
            if (!busy) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: busy is low right after send", $time);
            end
            wait (tx == 1'b0);                              // the start bit begins
            repeat (CPB / 2) @(negedge clk);                // the middle of it
            if (tx !== 1'b0) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: the start bit isn't low", $time);
            end
            for (i = 0; i < 8; i = i + 1) begin
                repeat (CPB) @(negedge clk);
                got[i] = tx;
            end
            repeat (CPB) @(negedge clk);
            if (tx !== 1'b1) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: the stop bit isn't high", $time);
            end
            if (got !== b) begin
                errors = errors + 1;
                $display("FAIL at %0t ns: sent %h, the line carried %h", $time, b, got);
            end
            wait (!busy);
            sent = sent + 1;
        end
    endtask

    initial begin
        #200_000;
        $fatal(1, "FAIL: uart_tx, timeout: the bench never finished");
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, uart_tx_tb);
        repeat (2) @(negedge clk);
        rst = 1'b0;
        repeat (3) @(negedge clk);
        if (tx !== 1'b1) begin
            errors = errors + 1;
            $display("FAIL: the idle line isn't high");
        end
        send_byte(8'h55);
        send_byte(8'hA3);
        send_byte(8'h00);
        send_byte(8'hFF);
        if (errors == 0) $display("PASS: uart_tx, %0d bytes decoded off the line, start, data and stop bits checked", sent);
        else $fatal(1, "FAIL: uart_tx, %0d errors", errors);
        $finish;
    end
endmodule
