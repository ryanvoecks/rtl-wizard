#!/usr/bin/env bash
# Runs the bundled BIST against ddr3_top + Micron model; PHY UNISIMs are
# stubbed by testbench/models/ under -DSIM_MODEL. The BIST injects bit errors
# on purpose, so the pass criterion is fails == injected (not fails == 0).
set -eo pipefail
cd testbench
iverilog -g2012 -o uberddr3_sim -DNO_TEST_MODEL -DSIM_MODEL \
    -s ddr3_dimm_micron_sim -I . \
    ddr3_dimm_micron_sim.sv ddr3.sv ddr3_module.sv \
    models/*.v ../rtl/ddr3_*.v
vvp -n ./uberddr3_sim | tee uberddr3_sim.log
awk '/^Number of Fails/           { f=$NF }
     /^Number of Injected Errors/ { i=$NF }
     END { exit !(i+0 > 0 && f+0 == i+0) }' uberddr3_sim.log
