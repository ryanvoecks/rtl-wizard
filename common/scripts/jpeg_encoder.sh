#!/usr/bin/env bash
set -eo pipefail
TB=common/jpeg/bench/verilog/bench_top.v
SRCS=(
    common/jpeg/rtl/verilog/jpeg_encoder.v
    common/dct/rtl/verilog/fdct.v
    common/dct/rtl/verilog/dct.v
    common/dct/rtl/verilog/dct_mac.v
    common/dct/rtl/verilog/dctu.v
    common/dct/rtl/verilog/dctub.v
    common/dct/rtl/verilog/zigzag.v
    common/qnr/rtl/verilog/jpeg_qnr.v
    common/qnr/rtl/verilog/div_uu.v
    common/qnr/rtl/verilog/div_su.v
    common/run_length_coding/rtl/verilog/jpeg_rle.v
    common/run_length_coding/rtl/verilog/jpeg_rzs.v
    common/run_length_coding/rtl/verilog/jpeg_rle1.v
)
iverilog -o jpeg.sim \
    -I common/qnr/bench/verilog \
    -I common/dct/rtl/verilog \
    "$TB" "${SRCS[@]}"
vvp -n jpeg.sim | tee jpeg_sim.log
grep -qE 'Total errors:[[:space:]]+0$' jpeg_sim.log
