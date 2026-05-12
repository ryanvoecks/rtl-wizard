# Generic Yosys synthesis script. Configured by config.tcl.

# Source config
source config.tcl

yosys -import

read_verilog -sv $DESIGN_V
hierarchy -check -top $DESIGN_NAME

# Generic synthesis pipeline
procs; opt
fsm; opt
memory; opt
techmap; opt

# Map flops and combinational cells to PDK
dfflibmap -liberty $PDK_LIB
abc -liberty $PDK_LIB -D $PERIOD_PS
clean

# Sanity report so the run log shows the post-synth cell mix
stat -liberty $PDK_LIB

write_verilog -noexpr -nohex -nodec $SYNTH_V
