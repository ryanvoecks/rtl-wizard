#!/usr/bin/env bash
set -eo pipefail
cd Pre-Synthesis_Simulation
iverilog -g2012 -o tpu.sim \
    test_tpu.v tpu_top.v systolic.v systolic_controll.v \
    quantize.v addr_sel.v write_out.v \
    sram_16x128b.v sram_256x32b.v
vvp tpu.sim | tee tpu_sim.log
! grep -q "wrong answer" tpu_sim.log
grep -q "Total cycle count" tpu_sim.log
