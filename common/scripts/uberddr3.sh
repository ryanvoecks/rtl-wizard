#!/usr/bin/env bash
# Run the BIST against ddr3_top + Micron model in three parallel variants:
# (a) self-refresh + second-wishbone, (b) BIST_MODE=2, (c) ECC SECDED.
set -eo pipefail
cd testbench

SRCS="ddr3_dimm_micron_sim.sv ddr3.sv ddr3_module.sv models/*.v ../rtl/ddr3_*.v"
ECC_SRCS="../rtl/ecc/*.sv"
COMMON="-g2012 -DNO_TEST_MODEL -DSIM_MODEL -s ddr3_dimm_micron_sim -I ."

build() {
    local name="$1"; shift
    iverilog $COMMON "$@" -o "uberddr3_${name}.vvp" $SRCS
}

build sr_wb2 -DCFG_TEST_SELF_REFRESH=1 -DCFG_SECOND_WISHBONE=1
build bist2  -DCFG_BIST_MODE=2
iverilog $COMMON -DCFG_ECC_ENABLE=3 -DCFG_SECOND_WISHBONE=1 \
    -o uberddr3_ecc3.vvp $SRCS $ECC_SRCS

run() {
    local name="$1"
    local datadir="model_data_${name}"
    rm -rf "$datadir"
    mkdir -p "$datadir"
    vvp -n "./uberddr3_${name}.vvp" "+model_data+${datadir}" \
        > "uberddr3_${name}.log" 2>&1
}

PIDS=()
run sr_wb2 & PIDS+=("$!")
run bist2  & PIDS+=("$!")
run ecc3   & PIDS+=("$!")

fail=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then fail=1; fi
done

for name in sr_wb2 bist2 ecc3; do
    if ! awk '/^Number of Fails/{f=$NF} /^Number of Injected Errors/{i=$NF}
              END { exit !(i+0 > 0 && f+0 == i+0) }' "uberddr3_${name}.log"; then
        echo "uberddr3: variant $name had unexpected fail/injected mismatch:" >&2
        tail -10 "uberddr3_${name}.log" >&2
        exit 1
    fi
done
[ "$fail" -eq 0 ] || exit 1
