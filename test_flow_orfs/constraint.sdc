# Timing constraints for the counter test design.
# 1 ns clock matches the period the benchmark's PPA scorer uses, so numbers
# from this flow are directly comparable to scorer output.
create_clock -name clk -period 1.0 [get_ports clk]

# 20% of the clock budgeted at the IO boundary in each direction.
# `all_inputs -no_clocks` excludes the clk port so we don't constrain a
# nonsensical clk-to-clk path.
set_input_delay  -clock clk 0.2 [all_inputs -no_clocks]
set_output_delay -clock clk 0.2 [all_outputs]
