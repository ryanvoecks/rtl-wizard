module data_switch (
    input  wire         clk,
    input  wire [127:0] din,
    input  wire [63:0]  sel,
    output reg  [127:0] dout
);
    // Drop the genvar-built `word[]` aliases and index `din` directly.
    // The byte offset is `sel*8`, but expressed as `{sel, 3'b000}` so
    // synthesis sees a plain bit-slice instead of a multiply.
    integer i;
    always @(posedge clk) begin
        for (i = 0; i < 16; i = i + 1) begin
            dout[i*8 +: 8] <= din[{sel[i*4 +: 4], 3'b000} +: 8];
        end
    end
endmodule
