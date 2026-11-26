// Traffic light: a state machine with a timer.
//
// Three states, each held for its own number of clock cycles: green, then
// yellow, then red, then green again. A walk request during green cuts green
// short (after at least MIN_GREEN cycles). Outputs are one-hot lights.
//
// The usual FSM shape: the state in a register (always @(posedge clk)), the
// next state worked out in an always @(*) that starts from "stay where you
// are", so every path gives every signal a value and no latch is inferred.
module traffic #(
    parameter GREEN_T  = 8'd20,
    parameter YELLOW_T = 8'd4,
    parameter RED_T    = 8'd16,
    parameter MIN_GREEN = 8'd6
) (
    input  wire clk,
    input  wire rst,
    input  wire walk,      // a pedestrian pressed the button
    output wire green,
    output wire yellow,
    output wire red
);
    localparam [1:0] S_GREEN = 2'd0, S_YELLOW = 2'd1, S_RED = 2'd2;

    reg [1:0] state, next;
    reg [7:0] timer;       // cycles spent in this state
    reg       waiting;     // a walk request is pending

    // Next state: stay, unless this state's time is up (or a walk request
    // ends green early).
    always @(*) begin
        next = state;
        case (state)
            S_GREEN:  if (timer == GREEN_T - 1 || (waiting && timer >= MIN_GREEN - 1)) next = S_YELLOW;
            S_YELLOW: if (timer == YELLOW_T - 1) next = S_RED;
            S_RED:    if (timer == RED_T - 1) next = S_GREEN;
            default:  next = S_RED;
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            state   <= S_RED;
            timer   <= 8'd0;
            waiting <= 1'b0;
        end else begin
            state <= next;
            timer <= (next != state) ? 8'd0 : timer + 8'd1;
            // A request is remembered until the light has gone red.
            if (walk && state == S_GREEN) waiting <= 1'b1;
            else if (state == S_RED)      waiting <= 1'b0;
        end
    end

    assign green  = (state == S_GREEN);
    assign yellow = (state == S_YELLOW);
    assign red    = (state == S_RED);
endmodule
