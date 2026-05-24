#!/usr/bin/env bash
set -eo pipefail
SRCS=(
    fpu_double.v fpu_add.v fpu_sub.v fpu_mul.v
    fpu_div.v fpu_round.v fpu_exceptions.v
    fpu_TB.v
)
verilator --binary --timing --top-module fpu_tb -Wno-fatal "${SRCS[@]}"
obj_dir/Vfpu_tb | tee fpu_sim.log
! grep -q 'Error! out is incorrect' fpu_sim.log
