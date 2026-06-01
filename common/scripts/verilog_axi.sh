#!/usr/bin/env bash
# Run the upstream 33-case cocotb regression across 5 parallel sims.
set -eo pipefail

run_group() {
    local group="$1"; shift
    local testcases="$1"
    SIM_BUILD="sim_build_g${group}" make -C tb/axi_crossbar \
        PARAM_S_COUNT=8 PARAM_M_COUNT=8 PARAM_M_ID_WIDTH=11 \
        TESTCASE="$testcases" \
        > "axi_sim_g${group}.log" 2>&1
}

# Stripe write/read factory tests across 4 groups by suffix index.
build_list() {
    local mod="$1"
    local out=""
    for base in run_test_write run_test_read; do
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
            if [ $(( (i - 1) % 4 )) -eq "$mod" ]; then
                out="${out:+$out,}${base}_$(printf '%03d' "$i")"
            fi
        done
    done
    echo "$out"
}

# Stress test runs in its own group.
PIDS=()
run_group 0 "$(build_list 0)" & PIDS+=("$!")
run_group 1 "$(build_list 1)" & PIDS+=("$!")
run_group 2 "$(build_list 2)" & PIDS+=("$!")
run_group 3 "$(build_list 3)" & PIDS+=("$!")
run_group 4 "run_stress_test_001" & PIDS+=("$!")

fail=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then fail=1; fi
done

total_tests=0
for g in 0 1 2 3 4; do
    if ! grep -q 'FAIL=0 SKIP=0' "axi_sim_g${g}.log"; then
        echo "verilog_axi: group $g had failures or skips:" >&2
        tail -20 "axi_sim_g${g}.log" >&2
        exit 1
    fi
    line=$(grep -E 'TESTS=[0-9]+ PASS=[0-9]+ FAIL=0 SKIP=0' "axi_sim_g${g}.log" \
           | tail -1)
    n=$(echo "$line" | sed -nE 's/.*TESTS=([0-9]+).*/\1/p')
    total_tests=$(( total_tests + ${n:-0} ))
    echo "verilog_axi g${g}: ${line#* ** }"
done
echo "verilog_axi: ${total_tests}/${total_tests} cocotb cases PASS across 5 groups"
[ "$fail" -eq 0 ] || exit 1
