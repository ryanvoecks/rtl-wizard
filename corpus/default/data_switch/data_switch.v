module data_switch (
    input  wire         clk,
    input  wire [127:0] din,
    input  wire [63:0]  sel,
    output reg  [127:0] dout
);
    wire [7:0] word [0:15];
    genvar g;
    generate
        for (g = 0; g < 16; g = g + 1) begin : split
            assign word[g] = din[g*8 +: 8];
        end
    endgenerate

    integer i;
    always @(posedge clk) begin
        for (i = 0; i < 16; i = i + 1) begin
            dout[i*8 +: 8] <= word[sel[i*4 +: 4]];
        end
    end
endmodule
