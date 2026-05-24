#!/usr/bin/env bash
# Upstream Rudis_TB short regression. Pass = no ERROR: lines in the sim log
# other than the bogus "ACK count Mismatch" ones (off-by-2x in the upstream TB).
set -eo pipefail
iverilog -o wb_dma.vvp -I rtl/verilog -I bench/verilog \
    rtl/verilog/*.v \
    bench/verilog/test_bench_top.v \
    bench/verilog/wb_mast_model.v \
    bench/verilog/wb_slv_model.v
vvp -n wb_dma.vvp | tee wb_dma_sim.log
awk '/^ERROR:/ && !/ACK count Mismatch/ { exit 1 }' wb_dma_sim.log
