module counter_array (
    input  wire           clk,
    input  wire           rst_n,
    input  wire           enable,
    output reg  [1023:0]  counts
);
    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            counts <= 1024'b0;
        end else if (enable) begin
            for (i = 0; i < 128; i = i + 1) begin
                counts[i*8 +: 8] <= counts[i*8 +: 8] + 8'd1;
            end
        end
    end
endmodule
