# Shared ORFS knobs for designs under `corpus/`. Consumed by
# /OpenROAD-flow-scripts/flow/Makefile via DESIGN_CONFIG.
#
# Per-design values (DESIGN_NAME, VERILOG_FILES, SDC_FILE, DESIGN_DIR) are
# passed by run.py on the make command line and override anything set here.

export PLATFORM    ?= nangate45

# The corpus designs are tiny, so we need a low utilization to give the
# nangate45 PDN's M4 (28 um) and M7 (15 um) strap pitches room to land
# inside the core.
export CORE_UTILIZATION   ?= 15
export PLACE_DENSITY      ?= 0.30
