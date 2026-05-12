# OpenSTA pass over the synthesized netlist — pre-placement timing/area/power

source config.tcl

# Inputs
read_lef     $TECH_LEF
read_lef     $CELL_LEF
read_liberty $PDK_LIB
read_verilog $SYNTH_V
link_design  $DESIGN_NAME
read_sdc     $SDC

# Reports
report_checks -path_delay max
report_design_area
report_power
