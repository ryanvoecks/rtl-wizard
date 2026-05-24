#!/usr/bin/env bash
# Runs all user-level and machine-level tests in the pre-baked suite
set -eo pipefail
cd vsim
GEN_DIR=../riscv-tools/riscv-tests/isa/generated
TESTS=()
for d in "$GEN_DIR"/rv32ui-p-*.dump "$GEN_DIR"/rv32mi-p-*.dump; do
    TESTS+=("$(basename "$d" .dump)")
done
if [ "${#TESTS[@]}" -eq 0 ]; then
    echo "E203_NO_TESTS_FOUND:$GEN_DIR"
    exit 1
fi
make install >/dev/null
make compile >/dev/null
for t in "${TESTS[@]}"; do
    make run_test TESTNAME="$t" DUMPWAVE=0 >/dev/null 2>&1
    if ! grep -q TEST_PASS "run/$t/$t.log"; then
        echo "E203_TEST_FAILED:$t"
        exit 1
    fi
    echo "E203_TEST_PASS:$t"
done
