### Design summary

This is a **parameterizable hardware bitonic sorting network** in Verilog (by Dmitry Matyunin, MIT-licensed). It sorts `CHAN_NUM` parallel data words of `DATA_WIDTH` bits each, presented as one wide flattened bus, and emits them in fully sorted order (ascending or descending) on a same-width output bus.

It implements the classic **bitonic sort** algorithm purely with combinational compare-and-swap elements, structured as a fixed network of O(N·log²N) comparators. The hierarchy is built recursively with Verilog `generate` loops:
- The number of channels is rounded up to the next power of two (`CHAN_ACT`); padding lanes are filled with min/max sentinel values so unused slots sort to the discarded end.
- Sorting proceeds through `log2(CHAN_ACT)` **stages**; each stage builds increasingly large bitonic sequences and merges them.
- **Optional pipelining**: `PIPE_REG` inserts output registers every N-th comparator layer to break the long combinational path (latency = number of registered layers), or runs fully combinational when `PIPE_REG = 0`.

Key parameters (top level `bitonic_sort`): `DATA_WIDTH`, `CHAN_NUM`, `DIR` (0 = ascending, 1 = descending), `SIGNED` (signed/unsigned compare), `PIPE_REG` (pipeline density).

### Directory organisation

```
.
└── rtl/
    ├── bitonic_sort.v    # top-level sorter
    ├── bitonic_block.v   # one bitonic merge block
    ├── bitonic_node.v    # one comparator layer (butterfly)
    └── bitonic_comp.v    # leaf compare-and-swap element
```

### Key modules/files

A strict 4-level instantiation hierarchy, top → bottom:

- **`bitonic_sort.v` (`bitonic_sort`)** — Top module / user entry point. Pads input up to a power-of-two channel count (sentinel fill depends on `SIGNED`), then generates `STAGES = clog2(CHAN_ACT)` sort stages. Each stage instantiates `BLOCKS` copies of `bitonic_block`, alternating each block's sort direction via `BLOCK_POLARITY` (the alternating-direction trick that produces bitonic sequences). Finally slices the result back down to `CHAN_NUM` channels, selecting the high or low half based on `DIR`.

- **`bitonic_block.v` (`bitonic_block`)** — A bitonic **merge** block of order `ORDER`, which sorts `2^(ORDER+1)` channels. Expands into `STAGES = ORDER+1` internal stages; each stage has `2^stage` nodes, with node order decreasing per stage (the halving merge structure). Uses the `index()` function to compute a global layer index passed down as `INDEX`, so pipeline registers can be placed consistently across the whole network. Instantiates `bitonic_node`.

- **`bitonic_node.v` (`bitonic_node`)** — One comparator **layer** ("butterfly") that performs `COMP_NUM = 2^ORDER` parallel compare-and-swap operations between the two halves of its input bus. Computes `REGOUT_EN` from `INDEX % PIPE_REG` to decide whether this layer's comparators register their outputs. Instantiates `bitonic_comp`.

- **`bitonic_comp.v` (`bitonic_comp`)** — Leaf **compare-and-swap** cell. Compares two `DATA_WIDTH` words (signed or unsigned per `SIGNED`) and drives high/low outputs `H`/`L`; `POLARITY` chooses sort direction, `REGOUT_EN` selects registered (pipelined) vs. combinational output.

**Interaction flow:** `bitonic_sort` → many `bitonic_block` (with alternating polarity) → each block → many `bitonic_node` layers → each node → many `bitonic_comp` cells. All data is passed as flattened buses sliced via `generate`-indexed assigns; `clk` threads through only to feed the optional pipeline registers in `bitonic_comp`.
