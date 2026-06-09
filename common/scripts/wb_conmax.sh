#!/usr/bin/env bash
# Upstream OpenCores WISHBONE Conmax testbench (Usselmann). The bench drives
# the 8x16 connection matrix with self-checking master/slave models across
# datapath, register-file and arbiter tests.
set -eo pipefail

iverilog -g2005 -o wb_conmax.vvp -s test \
    -I rtl/verilog -I bench/verilog \
    rtl/verilog/*.v \
    bench/verilog/test_bench_top.v \
    bench/verilog/wb_slv_model.v \
    bench/verilog/wb_mast_model.v

vvp -n wb_conmax.vvp > wb_conmax_sim.log 2>&1

if ! grep -q 'Test DONE' wb_conmax_sim.log; then
    echo "wb_conmax: simulation did not reach 'Test DONE'" >&2
    tail -20 wb_conmax_sim.log >&2
    exit 1
fi

# Any non-zero "Total ERRORS: N" (or a watchdog ERROR line) fails the run.
if grep -qE 'Total ERRORS:[[:space:]]*[1-9]' wb_conmax_sim.log \
   || grep -q '\*\*\* ERROR' wb_conmax_sim.log; then
    echo "wb_conmax: testbench reported errors:" >&2
    grep -E 'Total ERRORS:|\*\*\* ERROR' wb_conmax_sim.log >&2
    exit 1
fi

errs=$(grep -cE 'Total ERRORS:[[:space:]]*0' wb_conmax_sim.log)
echo "wb_conmax: ${errs} self-check phases reported 0 errors; Test DONE"
