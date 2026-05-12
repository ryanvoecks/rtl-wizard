#!/usr/bin/env bash
# Drive the ORFS make-based flow (yosys synth -> floorplan -> place -> CTS ->
# route -> finish) for the counter test design.
#
# This mirrors ../test_flow but replaces all the hand-written TCL with the
# stock ORFS Makefile. config.mk in this directory configures the design;
# WORK_HOME is pointed at ./out so artifacts land next to this script instead
# of in the upstream ORFS tree.
#
# Usage: ./run.sh [make-target ...]   (default target: finish)
set -euo pipefail

cd "$(dirname "$0")"

DESIGN_DIR="$(pwd)"
OUT_DIR="$DESIGN_DIR/out"
FLOW_HOME="${FLOW_HOME:-/OpenROAD-flow-scripts/flow}"

if [[ ! -f "$FLOW_HOME/Makefile" ]]; then
    echo "error: ORFS flow Makefile not found at $FLOW_HOME/Makefile" >&2
    echo "       set FLOW_HOME to your OpenROAD-flow-scripts/flow checkout." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# Targets: default to running the full flow up through the final routed STA
# report. We deliberately stop short of `finish`/`gds` — ORFS's `finish` also
# depends on GDS generation via KLayout, which isn't part of the hand-written
# flow this directory mirrors and isn't installed in every sandbox.
# `do-finish` is the ORFS phony that emits the final STA/power/area report
# (`6_report.log`) without the GDS dep.
#
# Pass explicit targets on the command line to override
# (e.g. `./run.sh synth`, `./run.sh clean`, `./run.sh finish` for full GDS).
targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(synth floorplan place cts route do-finish)
fi

make -C "$FLOW_HOME" \
    DESIGN_CONFIG="$DESIGN_DIR/config.mk" \
    DESIGN_DIR="$DESIGN_DIR" \
    WORK_HOME="$OUT_DIR" \
    "${targets[@]}" 2>&1 | tee "$OUT_DIR/flow.log"

echo
echo "Done. ORFS artifacts under $OUT_DIR/{logs,objects,reports,results}/nangate45/counter/base/"
