// 32-bit synchronous accumulator with enable and synchronous reset.
// Wide enough to give the platform's standard PDN pitch room to place
// straps inside the core, and exercises flops + a non-trivial adder +
// a clock so the flow runs CTS and timing meaningfully.
module counter (
    input  wire        clk,
    input  wire        rst,
    input  wire        en,
    input  wire [31:0] din,
    output reg  [31:0] count
);
    always @(posedge clk) begin
        if (rst)
            count <= 32'd0;
        else if (en)
            count <= count + din;
    end
endmodule
