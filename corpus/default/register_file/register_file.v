module register_file (
    input  wire        clk,
    input  wire [4:0]  raddr0, raddr1, raddr2, raddr3,
    input  wire [4:0]  waddr0, waddr1,
    input  wire [31:0] wdata0, wdata1,
    input  wire        we0, we1,
    output reg  [31:0] rdata0, rdata1, rdata2, rdata3
);
    reg [31:0] mem [0:31];

    always @(posedge clk) begin
        if (we0) mem[waddr0] <= wdata0;
        if (we1) mem[waddr1] <= wdata1;

        rdata0 <= mem[raddr0];
        rdata1 <= mem[raddr1];
        rdata2 <= mem[raddr2];
        rdata3 <= mem[raddr3];
    end
endmodule
