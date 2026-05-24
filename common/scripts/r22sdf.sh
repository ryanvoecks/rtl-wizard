#!/usr/bin/env bash
# TB128.v $fdisplays bit-reversed outputs to output{4,5}.txt; we diff
# against the snapshot goldens captured by the patch. The patch also adds
# real/imag impulse vectors that the TB self-checks against the analytic
# DFT result (constant across all bins), printing R22SDF_(IMAG_)IMPULSE_PASS.
set -eo pipefail
cd sim/fft_128_tc
SRCS=(
    ../../verilog/FFT128.v ../../verilog/SdfUnit.v ../../verilog/SdfUnit2.v
    ../../verilog/Butterfly.v ../../verilog/DelayBuffer.v
    ../../verilog/Multiply.v ../../verilog/Twiddle128.v
)
iverilog -o tb128.vvp "${SRCS[@]}" TB128.v
vvp tb128.vvp | tee r22sdf_sim.log
# Reference goldens are CRLF (Windows-authored repo); normalize before diff.
sed -i 's/\r$//' output4.txt output5.txt
diff output4.txt output4_golden.txt
diff output5.txt output5_golden.txt
grep -q R22SDF_IMPULSE_PASS r22sdf_sim.log
grep -q R22SDF_IMAG_IMPULSE_PASS r22sdf_sim.log
