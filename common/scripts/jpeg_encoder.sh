#!/usr/bin/env bash
# Pass = "Total errors: 0" in the sim log. dct_cos_table.v is `included by
# dctu.v and dct_syn.v is a standalone synth-test wrapper -- both excluded.
set -eo pipefail
shopt -s extglob
iverilog -o jpeg.sim \
    -I common/qnr/bench/verilog -I common/dct/rtl/verilog \
    common/jpeg/bench/verilog/bench_top.v \
    common/jpeg/rtl/verilog/*.v \
    common/dct/rtl/verilog/!(dct_cos_table|dct_syn).v \
    common/qnr/rtl/verilog/*.v \
    common/run_length_coding/rtl/verilog/*.v
vvp -n jpeg.sim | tee jpeg_sim.log
grep -qE 'Total errors:[[:space:]]+0$' jpeg_sim.log
