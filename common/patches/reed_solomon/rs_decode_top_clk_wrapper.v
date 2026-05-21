// Thin wrapper for ORFS calibration: exposes a single port named `clk`
// (the project's SDC template hardcodes that name). The upstream
// RsDecodeTop uses `CLK` (uppercase) which `get_ports clk` doesn't
// match, so we rename here. All other ports passthrough.
module RsDecodeTop_clk_wrapper(
    input          clk,
    input          RESET,
    input          enable,
    input          startPls,
    input          erasureIn,
    input  [7:0]   dataIn,
    output         outEnable,
    output         outStartPls,
    output         outDone,
    output [7:0]   errorNum,
    output [7:0]   erasureNum,
    output         fail,
    output [7:0]   delayedData,
    output [7:0]   outData
);
    RsDecodeTop inner (
        .CLK          (clk),
        .RESET        (RESET),
        .enable       (enable),
        .startPls     (startPls),
        .erasureIn    (erasureIn),
        .dataIn       (dataIn),
        .outEnable    (outEnable),
        .outStartPls  (outStartPls),
        .outDone      (outDone),
        .errorNum     (errorNum),
        .erasureNum   (erasureNum),
        .fail         (fail),
        .delayedData  (delayedData),
        .outData      (outData)
    );
endmodule
