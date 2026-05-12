# Yosys synthesis script for the counter test design.
# Targets nangate45 (the only PDK installed in this container) and writes a
# gate-level netlist ready for OpenROAD to floorplan/place/route.
#
# Paths are hardcoded — yosys -s scripts don't do env-var expansion.
# Run via run.sh, which `cd`s into this directory first.

# Source config
source config.tcl

yosys -import

read_verilog -sv counter.v
hierarchy -check -top counter

# Generic synthesis pipeline.
procs; opt
fsm; opt
memory; opt
techmap; opt

# Map flops and combinational cells to nangate45.
dfflibmap -liberty $PDK_LIB
abc        -liberty $PDK_LIB
clean

# Sanity report so the run log shows the post-synth cell mix.
stat -liberty $PDK_LIB

write_verilog -noattr -noexpr -nohex -nodec out/counter.synth.v
