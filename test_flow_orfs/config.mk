# ORFS design config for the counter test. Consumed by /OpenROAD-flow-scripts/flow/Makefile.
#
# `run.sh` is the canonical entry point — it sets DESIGN_DIR to this directory
# and WORK_HOME to ./out so the standard ORFS make targets (synth, floorplan,
# place, cts, route, finish) drop their artifacts next to this config rather
# than into the upstream tree.

export DESIGN_NAME = counter
export PLATFORM    = nangate45

export VERILOG_FILES = $(DESIGN_DIR)/counter.v
export SDC_FILE      = $(DESIGN_DIR)/constraint.sdc

# Match the geometry of the hand-written flow: the 32-bit counter is tiny, so
# we need a low utilization to give the nangate45 PDN's M4 (28 um) and M7
# (15 um) strap pitches room to land inside the core.
export CORE_UTILIZATION   ?= 15
export PLACE_DENSITY      ?= 0.30
