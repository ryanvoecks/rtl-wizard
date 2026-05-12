// 4-bit ALU with 8 operations selected by op[2:0].
//   000: add        001: sub
//   010: and        011: or
//   100: xor        101: not a
//   110: shift left 111: shift right
module alu4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire [2:0] op,
    output reg  [3:0] y,
    output wire       zero
);
    always @(*) begin
        case (op)
            3'b000: y = a + b;
            3'b001: y = a - b;
            3'b010: y = a & b;
            3'b011: y = a | b;
            3'b100: y = a ^ b;
            3'b101: y = ~a;
            3'b110: y = a << 1;
            3'b111: y = a >> 1;
        endcase
    end

    assign zero = (y == 4'd0);
endmodule
