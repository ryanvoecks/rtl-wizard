// Synchronous FIFO, 8-bit data, depth 16.
module fifo8 #(
    parameter DEPTH = 16,
    parameter AW    = 4
) (
    input  wire       clk,
    input  wire       rst,
    input  wire       wr_en,
    input  wire [7:0] din,
    input  wire       rd_en,
    output reg  [7:0] dout,
    output wire       full,
    output wire       empty
);
    reg [7:0]    mem [0:DEPTH-1];
    reg [AW:0]   wr_ptr;
    reg [AW:0]   rd_ptr;

    wire do_wr = wr_en & ~full;
    wire do_rd = rd_en & ~empty;

    always @(posedge clk) begin
        if (rst) begin
            wr_ptr <= 0;
            rd_ptr <= 0;
            dout   <= 8'd0;
        end else begin
            if (do_wr) begin
                mem[wr_ptr[AW-1:0]] <= din;
                wr_ptr              <= wr_ptr + 1'b1;
            end
            if (do_rd) begin
                dout   <= mem[rd_ptr[AW-1:0]];
                rd_ptr <= rd_ptr + 1'b1;
            end
        end
    end

    assign empty = (wr_ptr == rd_ptr);
    assign full  = (wr_ptr[AW] != rd_ptr[AW]) &&
                   (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]);
endmodule
