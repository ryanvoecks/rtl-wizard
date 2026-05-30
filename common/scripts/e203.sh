#!/usr/bin/env bash
# Curated subset of the bundled riscv-tests suite. The list below covers ALU,
# loads/stores, branch/jump, CSR access, and machine-mode exception handling.
set -eo pipefail
cd vsim
GEN_DIR=../riscv-tools/riscv-tests/isa/generated
TESTS=(
    rv32ui-p-add
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
JOBS=$((JOBS < ${#TESTS[@]} ? JOBS : ${#TESTS[@]}))
printf '%s\n' "${TESTS[@]}" | \
    xargs -n 1 -P "$JOBS" -I{} make run_test TESTNAME={} DUMPWAVE=0 >/dev/null 2>&1
for t in "${TESTS[@]}"; do
    if ! grep -q TEST_PASS "run/$t/$t.log"; then
        echo "E203_TEST_FAILED:$t"
        exit 1
    fi
    echo "E203_TEST_PASS:$t"
done
