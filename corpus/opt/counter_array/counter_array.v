module counter_array (
    input  wire           clk,
    input  wire           rst_n,
    input  wire           enable,
    output wire [1023:0]  counts
);
    // All 128 lanes share identical reset and increment, so they hold
    // the same value at every cycle. Keep one 8-bit counter and fan it
    // out to the 1024-bit port.
    reg [7:0] count;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)       count <= 8'b0;
        else if (enable)  count <= count + 8'd1;
    end

    assign counts = {128{count}};
endmodule
