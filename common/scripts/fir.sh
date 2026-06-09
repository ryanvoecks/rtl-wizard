#!/usr/bin/env bash
# Self-checking FIR testbench, run under Verilator.
# The SystemVerilog TB compares every output sample bit-exact against a committed
# integer golden (Parks-McClellan coeffs, numpy filter model) and prints
# "Testcase PASSED" / "Testcase FAILED". It ends with $finish (not $fatal), so the
# printed banner -- not the exit code -- is the pass/fail signal. Golden vectors
# are pre-generated and committed.
set -eo pipefail
cd .drl_src_code/filt_fir
for tc in 1 2 3 4 5 6; do
    ln -sf "$PWD/sim/testcases/stimuli/defines_${tc}.sv" sim/testbench/defines.sv
    rm -rf obj_dir
    verilator --binary --timing --top-module filt_fir_tb \
        -Wno-fatal -Wno-WIDTH -Wno-CASEINCOMPLETE \
        -Irtl -Isim/testbench \
        rtl/dff.v rtl/filt_fir.v sim/testbench/filt_fir_tb.sv > "verilator_${tc}.log" 2>&1
    ./obj_dir/Vfilt_fir_tb | tee "sim_tc_${tc}.log"
    grep -q 'Testcase PASSED' "sim_tc_${tc}.log"
    ! grep -q 'Testcase FAILED' "sim_tc_${tc}.log"
done
echo "### INFO: All FIR testcases passed."
