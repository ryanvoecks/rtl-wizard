# Config variables for synth + P&R flow

# Inputs
set PDK_LIB "/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"
set DESIGN_NAME "counter"
set DESIGN_V "$DESIGN_NAME.v"
set PERIOD_PS 1000

# Outputs
set OUT_DIR "out"
set SYNTH_V "$OUT_DIR/$DESIGN_NAME.synth.v"
set SYNTH_STAT "$OUT_DIR/synth_stat.json"
