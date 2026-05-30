#!/usr/bin/env bash
# Upstream cocotb regression (33 cases).
set -eo pipefail
make -C tb/axi_crossbar PARAM_S_COUNT=8 PARAM_M_COUNT=8 PARAM_M_ID_WIDTH=11 2>&1 | tee axi_sim.log
grep -q 'FAIL=0 SKIP=0' axi_sim.log
