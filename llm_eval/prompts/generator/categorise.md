# Instructions

Your task is to read the following RTL optimisation diff, and classify it into one of the following categories:
* *Logic simplification*: small syntax changes to combinational logic which are functionally identical.
* *Combinational restructuring*: rearranging arithmetic expressions or mux trees to reduce depth.
* *Parallelisation*: duplicating logic to eliminate muxes.
* *Pipeline insertion*: inserting registers into the critical path.
* *Fanout reduction*: reducing the number of inputs driven by a group of cells.
* *Mixed*: a mixture of these classes.
* *None*: a no-op/empty edit.
* *Other*: anything else.

Output only your chosen class.


# Diff

<diff>
