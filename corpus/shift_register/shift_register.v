// 8-bit serial-in / parallel-out shift register with synchronous reset.
module shift_register (
    input  wire       clk,
    input  wire       rst,
    input  wire       sin,
    output reg  [7:0] q
);
    always @(posedge clk) begin
        if (rst)
            q <= 8'd0;
        else
            q <= {q[6:0], sin};
    end
endmodule
