#!/usr/bin/env bash
# Drive yosys synthesis + OpenSTA + OpenROAD P&R for the counter test design.
# All three TCL scripts read their paths from config.tcl.
#
# Usage: ./run.sh
# Outputs land in ./out/ alongside this script.
set -euo pipefail

cd "$(dirname "$0")"

OUT_DIR=out
mkdir -p "$OUT_DIR"

echo "=== [1/3] yosys synthesis ==="
yosys -q -c synth.tcl 2>&1 | tee "$OUT_DIR/synth.log"

echo
echo "=== [2/3] OpenSTA on synthesized netlist ==="
openroad -no_init -exit sta.tcl 2>&1 | tee "$OUT_DIR/sta.log"

echo
echo "=== [3/3] OpenROAD place-and-route ==="
openroad -no_init -exit pnr.tcl 2>&1 | tee "$OUT_DIR/pnr.log"

echo
echo "Done. Artifacts in $OUT_DIR/:"
ls -1 "$OUT_DIR"
