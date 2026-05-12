#!/usr/bin/env bash
# Drive yosys synthesis + OpenROAD P&R for the counter test design.
#
# Usage: ./run.sh
# Outputs land in ./out/ alongside this script.
set -euo pipefail

cd "$(dirname "$0")"

DESIGN_NAME=counter
OUT_DIR=out

PLATFORM=/OpenROAD-flow-scripts/flow/platforms/nangate45
export LIB=$PLATFORM/lib/NangateOpenCellLibrary_typical.lib
export TECH_LEF=$PLATFORM/lef/NangateOpenCellLibrary.tech.lef
# Use `.macro.mod.lef` (the platform's SC_LEF), not the plain `.macro.lef` —
# only the mod version includes TAPCELL_X1, which tapcell.tcl needs.
export CELL_LEF=$PLATFORM/lef/NangateOpenCellLibrary.macro.mod.lef

mkdir -p "$OUT_DIR"

echo "=== [1/2] yosys synthesis ==="
yosys -q -c synth.tcl 2>&1 | tee "$OUT_DIR/synth.log"

echo
echo "=== [2/2] OpenROAD place-and-route ==="
# pnr.tcl reads these via $env(...).
export DESIGN_NAME
export NETLIST="$OUT_DIR/${DESIGN_NAME}.synth.v"
export SDC=constraint.sdc
export OUT_DIR
openroad -no_init -exit pnr.tcl 2>&1 | tee "$OUT_DIR/pnr.log"

echo
echo "Done. Artifacts in $OUT_DIR/:"
ls -1 "$OUT_DIR"
