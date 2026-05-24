#!/usr/bin/env bash
# TB self-checks and calls $fatal on mismatch, so vvp's exit code is the
# pass/fail signal -- no grep needed.
set -eo pipefail
SRCS=(
    hdl/basic/bitonic_comp.v
    hdl/basic/bitonic_node.v
    hdl/basic/bitonic_block.v
    hdl/basic/bitonic_sort.v
)
iverilog -g2012 -o test/tb.vvp test/tb_bitonic_sort.v "${SRCS[@]}"
vvp test/tb.vvp
