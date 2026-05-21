// Thin wrapper for ORFS calibration: exposes a single port named `clk`
// (the project's SDC template hardcodes that name). Ties the two original
// clocks together -- xgmii_rxclk and rxclk_2x become the same clock,
// which is fine for synth/PPA characterization since we're not modelling
// the actual 2x ratio anyway. All other ports passthrough unchanged.
module rxReceiveEngine_clk_wrapper(
    input              clk,
    input              reset_in,
    input      [31:0]  xgmii_rxd,
    input      [3:0]   xgmii_rxc,
    output     [17:0]  rxStatRegPlus,
    input      [64:0]  cfgRxRegData_in,
    output     [63:0]  rx_data,
    output     [7:0]   rx_data_valid,
    output             rx_good_frame,
    output             rx_bad_frame,
    output     [2:0]   rxCfgofRS,
    output     [1:0]   rxTxLinkFault,
    output             rxclk_out
);
    rxReceiveEngine inner (
        .xgmii_rxclk      (clk),
        .rxclk_2x         (clk),
        .reset_in         (reset_in),
        .xgmii_rxd        (xgmii_rxd),
        .xgmii_rxc        (xgmii_rxc),
        .rxStatRegPlus    (rxStatRegPlus),
        .cfgRxRegData_in  (cfgRxRegData_in),
        .rx_data          (rx_data),
        .rx_data_valid    (rx_data_valid),
        .rx_good_frame    (rx_good_frame),
        .rx_bad_frame     (rx_bad_frame),
        .rxCfgofRS        (rxCfgofRS),
        .rxTxLinkFault    (rxTxLinkFault),
        .rxclk_out        (rxclk_out)
    );
endmodule
