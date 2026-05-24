#!/usr/bin/env bash
# The TB does per-symbol decoder output checks plus per-symbol error/erasure,
# fail-pin, and threshold checks.
set -eo pipefail
cd example/sim
iverilog -o simReedSolomon.vvp simReedSolomon.v ../rtl/*.v
vvp simReedSolomon.vvp
cat result.out
! grep -q NG result.out
