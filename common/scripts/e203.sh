#!/usr/bin/env bash
# Curated subset of the bundled riscv-tests suite. Coverage:
# add/sub, sra, auipc, lw/sw, jal, and csr/illegal machine mode.
set -eo pipefail
cd vsim
GEN_DIR=../riscv-tools/riscv-tests/isa/generated
TESTS=(
    rv32ui-p-add
    rv32ui-p-sra
    rv32ui-p-auipc
    rv32ui-p-lw
    rv32ui-p-sw
    rv32ui-p-beq
    rv32ui-p-jal
    rv32mi-p-csr
    rv32mi-p-illegal
)
for t in "${TESTS[@]}"; do
    if [ ! -f "$GEN_DIR/$t.dump" ]; then
        echo "E203_NO_TESTS_FOUND:$GEN_DIR/$t.dump"
        exit 1
    fi
done
make install >/dev/null
make compile >/dev/null
JOBS=$(nproc 2>/dev/null || echo 4)
JOBS=$((JOBS < 8 ? JOBS : 8))
JOBS=$((JOBS < ${#TESTS[@]} ? JOBS : ${#TESTS[@]}))
export RUN_DIR=$(pwd)/run
export GEN_DIR_ABS=$(cd "$GEN_DIR" && pwd)
# Bypass recursive `make run_test` (heavy when N copies run in parallel) and
# invoke vvp directly per test.
printf '%s\n' "${TESTS[@]}" | xargs -n 1 -P "$JOBS" -I{} bash -c '
    t="$1"
    rm -rf "${RUN_DIR}/${t}"
    mkdir -p "${RUN_DIR}/${t}"
    cd "${RUN_DIR}/${t}"
    vvp "${RUN_DIR}/vvp.exec" -lxt2 \
        +DUMPWAVE=0 \
        +TESTCASE="${GEN_DIR_ABS}/${t}" \
        +SIM_TOOL=iverilog > "${t}.log" 2>&1
' _ {}
for t in "${TESTS[@]}"; do
    if ! grep -q TEST_PASS "run/$t/$t.log"; then
        echo "E203_TEST_FAILED:$t"
        exit 1
    fi
    echo "E203_TEST_PASS:$t"
done
