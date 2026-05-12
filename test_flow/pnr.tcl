# OpenROAD place-and-route script for the counter test design.
#
# Unlike the benchmark's `openroad_ppa` scorer (which only links the netlist
# and runs STA), this script runs a real flow: floorplan -> IO placement ->
# tapcells -> PDN -> global+detail placement -> CTS -> global+detail route ->
# final STA report. Outputs land in `./out/` next to this script.
#
# Inputs are taken from env vars set by run.sh:
#   DESIGN_NAME, NETLIST, SDC, TECH_LEF, CELL_LEF, LIB, OUT_DIR

# ---------- Library + design ----------
read_lef  $env(TECH_LEF)
read_lef  $env(CELL_LEF)
read_liberty $env(LIB)
read_verilog $env(NETLIST)
link_design $env(DESIGN_NAME)
read_sdc $env(SDC)

# ---------- Floorplan ----------
# 60% utilization, square aspect ratio; small core margin so this fits a
# tiny design like an 8-bit counter without OpenROAD complaining about
# unplaceable IOs.
# Low utilization keeps the die big enough that the platform's M4/M7 PDN
# strap pitches (40/30 um) actually fit inside the core.
initialize_floorplan \
    -utilization 15 \
    -aspect_ratio 1.0 \
    -core_space 5.0 \
    -site FreePDK45_38x28_10R_NP_162NW_34O

# Snap routing/placement tracks to the platform's standard pitches.
source /OpenROAD-flow-scripts/flow/platforms/nangate45/make_tracks.tcl

# ---------- IO placement ----------
# Pick horizontal pins on metal3, vertical on metal2 — the bottom routing
# layers, which keeps the design open for the router.
place_pins -hor_layers metal3 -ver_layers metal2

# ---------- Endcap / tapcells ----------
# The platform's tapcell.tcl reads $::env(TAP_CELL_NAME); set it from
# config.mk's value.
set ::env(TAP_CELL_NAME) TAPCELL_X1
source /OpenROAD-flow-scripts/flow/platforms/nangate45/tapcell.tcl

# ---------- Power grid ----------
# Use the platform's M1/M4/M7 grid strategy verbatim.
source /OpenROAD-flow-scripts/flow/platforms/nangate45/grid_strategy-M1-M4-M7.tcl
pdngen

# ---------- Global placement ----------
global_placement \
    -density 0.7 \
    -pad_left 2 \
    -pad_right 2

# ---------- Clock tree synthesis ----------
# Use the platform's RC estimates so wire delay is sane pre-route.
source /OpenROAD-flow-scripts/flow/platforms/nangate45/setRC.tcl
set_propagated_clock [all_clocks]
clock_tree_synthesis \
    -buf_list "BUF_X1 BUF_X2 BUF_X4 BUF_X8 BUF_X16 BUF_X32" \
    -root_buf BUF_X4 \
    -sink_clustering_enable

# ---------- Detailed placement (legalize post-CTS) ----------
detailed_placement

# ---------- Fill ----------
filler_placement {FILLCELL_X1 FILLCELL_X2 FILLCELL_X4 FILLCELL_X8 FILLCELL_X16 FILLCELL_X32}
check_placement -verbose

# ---------- Routing ----------
set_routing_layers -signal metal2-metal10 -clock metal4-metal10
global_route \
    -guide_file $env(OUT_DIR)/route.guide \
    -congestion_iterations 50

detailed_route \
    -output_drc $env(OUT_DIR)/route.drc \
    -output_maze $env(OUT_DIR)/route.maze.log \
    -verbose 0

# ---------- Final reports ----------
# Re-extract parasitics from routed geometry, then report timing/area/power.
estimate_parasitics -placement
report_design_area
report_checks -path_delay max
report_power

# ---------- Outputs ----------
write_verilog $env(OUT_DIR)/$env(DESIGN_NAME).routed.v
write_def     $env(OUT_DIR)/$env(DESIGN_NAME).routed.def
write_db      $env(OUT_DIR)/$env(DESIGN_NAME).routed.odb

exit
