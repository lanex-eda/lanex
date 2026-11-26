// UART transmitter in SystemVerilog: 8 data bits, no parity, 1 stop bit.
//
// Pulse `send` for one clock with a byte on `data`, and the byte goes out on
// `tx`, least significant bit first, one bit every CLKS_PER_BIT clocks:
// a low start bit, the 8 data bits, a high stop bit. `busy` is high meanwhile.
//
// The SystemVerilog here is the part every open tool reads: logic, always_ff
// and always_comb, and an enum from a package named as uart_pkg::IDLE.
// Yosys's built-in reader (what LibreLane uses unless USE_SLANG is on) does
// not take `import uart_pkg::*;`, so the names are written with the package.
module uart_tx #(
    parameter int CLKS_PER_BIT = 16
) (
    input  logic       clk,
    input  logic       rst,
    input  logic       send,
    input  logic [7:0] data,
    output logic       tx,
    output logic       busy
);
    uart_pkg::state_t state, next;
    logic [15:0] ticks;     // clocks spent on this bit
    logic [2:0]  bitn;      // which data bit is going out
    logic [7:0]  shreg;     // the byte being sent

    localparam logic [15:0] LAST_TICK = CLKS_PER_BIT - 1;
    wire bit_done = (ticks == LAST_TICK);

    always_comb begin
        next = state;
        case (state)
            uart_pkg::IDLE:  if (send) next = uart_pkg::START;
            uart_pkg::START: if (bit_done) next = uart_pkg::DATA;
            uart_pkg::DATA:  if (bit_done && bitn == 3'd7) next = uart_pkg::STOP;
            uart_pkg::STOP:  if (bit_done) next = uart_pkg::IDLE;
            default:         next = uart_pkg::IDLE;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= uart_pkg::IDLE;
            ticks <= '0;
            bitn  <= '0;
            shreg <= '0;
        end else begin
            state <= next;
            if (state == uart_pkg::IDLE) begin
                ticks <= '0;
                bitn  <= '0;
                if (send) shreg <= data;
            end else if (bit_done) begin
                ticks <= '0;
                if (state == uart_pkg::DATA) begin
                    bitn  <= bitn + 3'd1;
                    shreg <= {1'b0, shreg[7:1]};
                end
            end else begin
                ticks <= ticks + 16'd1;
            end
        end
    end

    // Idle and stop are high, start is low, data is the next bit.
    assign tx = (state == uart_pkg::START) ? 1'b0 :
                (state == uart_pkg::DATA)  ? shreg[0] : 1'b1;

    assign busy = (state != uart_pkg::IDLE);
endmodule
