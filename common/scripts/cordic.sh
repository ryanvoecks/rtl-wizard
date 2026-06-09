#!/usr/bin/env bash
# Self-checking TB: streams 4000 random vectors through both the rotation
# (p2r) and vectoring (r2p) cores and compares against IEEE real math with a
# tolerance
set -eo pipefail
cd rtl_gen
iverilog -g2012 -o tb.vvp \
    tb_cordic.v cordic_engine.v cordic_p2r.v cordic_r2p.v
vvp tb.vvp
