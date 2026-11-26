// Hex to 7-segment decoder: logic with no clock.
//
// A 4-bit value in, the seven segments of a display out (segment a is bit 0,
// g is bit 6; 1 lights a segment). There is no register, so the design has no
// clock port: LibreLane times the path from the inputs to the outputs against
// a virtual clock of CLOCK_PERIOD, with IO_DELAY_CONSTRAINT (20 %) of it given
// to the outside world on each side.
//
//      a
//     ---
//  f |   | b
//     -g-
//  e |   | c
//     ---
//      d
module seg7 (
    input  wire [3:0] hex,
    output reg  [6:0] seg
);
    // Every input value is listed, and the default gives seg a value on
    // every path: an always @(*) that misses one would infer a latch.
    always @(*) begin
        case (hex)
            4'h0: seg = 7'b0111111;
            4'h1: seg = 7'b0000110;
            4'h2: seg = 7'b1011011;
            4'h3: seg = 7'b1001111;
            4'h4: seg = 7'b1100110;
            4'h5: seg = 7'b1101101;
            4'h6: seg = 7'b1111101;
            4'h7: seg = 7'b0000111;
            4'h8: seg = 7'b1111111;
            4'h9: seg = 7'b1101111;
            4'ha: seg = 7'b1110111;
            4'hb: seg = 7'b1111100;
            4'hc: seg = 7'b0111001;
            4'hd: seg = 7'b1011110;
            4'he: seg = 7'b1111001;
            default: seg = 7'b1110001;   // 4'hf
        endcase
    end
endmodule
