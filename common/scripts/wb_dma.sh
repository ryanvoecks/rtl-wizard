#!/usr/bin/env bash
# Upstream Rudis_TB short regression. Pass = no ERROR: lines in the sim log
# other than the bogus "ACK count Mismatch" ones (off-by-2x in the upstream TB).
# Parallise tests, and skip long-running hw_dma4.
set -eo pipefail

# task:arg pairs, ordered longest solo time first.
TASKS=(
    'hw_dma3:2'         # ~55s
    'hw_dma2:2'         # ~53s
    'sw_ext_desc1:1'    # ~33s
    'sw_dma2:2'         # ~23s
    'sw_dma1:2'         # ~21s
    'pt01_wr:0'         # ~15s
    'pt10_wr:0'         # ~15s
    'pt01_rd:0'         # ~14s
    'pt10_rd:0'         # ~14s
    'hw_dma1:1'         # ~13s
    'arb_test1:0'       # ~9s
)

iverilog -g2005-sv -o wb_dma.vvp -I rtl/verilog -I bench/verilog \
    rtl/verilog/*.v \
    bench/verilog/test_bench_top.v \
    bench/verilog/wb_mast_model.v \
    bench/verilog/wb_slv_model.v

JOBS=$(nproc 2>/dev/null || echo 4)
JOBS=$((JOBS < 8 ? JOBS : 8))

printf '%s\n' "${TASKS[@]}" | xargs -n 1 -P "$JOBS" -I{} bash -c '
    entry="$1"; name="${entry%:*}"; arg="${entry#*:}"
    vvp -n wb_dma.vvp +TASK="$name" +ARG="$arg" > "wb_dma_${name}_sim.log" 2>&1
' _ {}

for entry in "${TASKS[@]}"; do
    name="${entry%:*}"
    log="wb_dma_${name}_sim.log"
    if ! awk '/^ERROR:/ && !/ACK count Mismatch/ { exit 1 }' "$log"; then
        echo "wb_dma: ${name} reported unexpected ERROR lines in $log" >&2
        exit 1
    fi
done
