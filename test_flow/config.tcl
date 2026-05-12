# Shared config for the synth -> STA -> P&R flow.

# Platform
set PLATFORM_DIR "/OpenROAD-flow-scripts/flow/platforms/nangate45"
set PDK_LIB      "$PLATFORM_DIR/lib/NangateOpenCellLibrary_typical.lib"
set TECH_LEF     "$PLATFORM_DIR/lef/NangateOpenCellLibrary.tech.lef"
set CELL_LEF     "$PLATFORM_DIR/lef/NangateOpenCellLibrary.macro.mod.lef"

# Design
set DESIGN_NAME "counter"
set DESIGN_V    "$DESIGN_NAME.v"
set SDC         "constraint.sdc"
set PERIOD_PS   1000

# Outputs
set OUT_DIR    "out"
set SYNTH_V    "$OUT_DIR/$DESIGN_NAME.synth.v"
set SYNTH_STAT "$OUT_DIR/synth_stat.json"
