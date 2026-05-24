#!/usr/bin/env bash
set -eo pipefail
iverilog -g2012 -o sim.vvp defines.v cache.v mem_model.v testbench.v
vvp -n sim.vvp | tee mshr_sim.log
grep -q 'ALL TESTS PASSED' mshr_sim.log
