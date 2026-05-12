// 4-to-1 multiplexer, 8-bit wide.
module mux4 (
    input  wire [7:0] in0,
    input  wire [7:0] in1,
    input  wire [7:0] in2,
    input  wire [7:0] in3,
    input  wire [1:0] sel,
    output reg  [7:0] y
);
    always @(*) begin
        case (sel)
            2'b00: y = in0;
            2'b01: y = in1;
            2'b10: y = in2;
            2'b11: y = in3;
        endcase
    end
endmodule
