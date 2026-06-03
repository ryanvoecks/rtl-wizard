### Design summary

This is an **8×8 systolic-array TPU (Tensor Processing Unit)** that performs matrix multiplication of 8-bit signed operands. Weights and input data are streamed in from four 32-bit SRAM read ports (two for weights, two for data), shifted into the systolic array's weight/data registers, multiplied-and-accumulated along the array's anti-diagonals, then quantized from the wide accumulator width back to 16 bits and written out to three result SRAMs (a/b/c) for comparison/storage.

How it works:
- A FSM controller (`systolic_controll`) sequences the whole operation (IDLE → LOAD_DATA → WAIT1 → ROLLING) and drives a global `cycle_num`, a `matrix_index` (selects which output anti-diagonal is ready), a `data_set` counter, and read-address and write-enable strobes.
- `addr_sel` turns the controller's `addr_serial_num` into the four SRAM read addresses (with skew between the w0/d0 and w1/d1 lanes).
- `systolic` holds the 8×8 weight/data register queues and the 8×8 MAC accumulators. Each cycle it shifts in new weights/data and accumulates products on the active diagonal, exposing one diagonal of results via `mul_outcome` indexed by `matrix_index`.
- `quantize` saturates/truncates the wide (21-bit) accumulator results down to 16-bit signed values.
- `write_out` registers the quantized vector and routes it to result SRAM a, b, or c with the correct address and write-enable based on `data_set` and `matrix_index` (handling the diagonal "mix" boundary cases).

Parameters: `ARRAY_SIZE=8`, `SRAM_DATA_WIDTH=32`, `DATA_WIDTH=8`, `OUTPUT_DATA_WIDTH=16`; internal accumulator width `ORI_WIDTH = 2*DATA_WIDTH+5 = 21`.

### Directory organisation

```
.
└── rtl/
    ├── tpu_top.v            # top-level wrapper, instantiates all submodules
    ├── systolic_controll.v  # FSM controller / sequencer
    ├── addr_sel.v           # SRAM read-address generator
    ├── systolic.v           # 8x8 systolic MAC array
    ├── quantize.v           # 21-bit -> 16-bit saturating quantizer
    └── write_out.v          # result routing/write to 3 output SRAMs
```

### Key modules/files

- **`rtl/tpu_top.v`** — Top module `tpu_top`. Defines the chip-level I/O (4 SRAM read ports in, 3 SRAM write ports out, `tpu_start`/`tpu_done`) and wires the five submodules together via internal nets: `addr_serial_num`, `ori_data`, `quantized_data`, `alu_start`, `cycle_num`, `matrix_index`, `sram_write_enable`, `data_set`. Start here to understand connectivity.

- **`rtl/systolic_controll.v`** — Module `systolic_controll`. The brain. A 4-state FSM (IDLE/LOAD_DATA/WAIT1/ROLLING) that generates every control/timing signal: `addr_serial_num` (ramps 0→127 during ROLLING) → `addr_sel`; `alu_start`, `cycle_num` → `systolic`; `matrix_index`, `data_set`, `sram_write_enable` → both `systolic` and `write_out`. Asserts `tpu_done` when `matrix_index==15 && data_set==1`.

- **`rtl/addr_sel.v`** — Module `addr_sel`. Combinational + output FF. Maps `addr_serial_num` to the four read addresses `sram_raddr_{w0,w1,d0,d1}`; the w1/d1 lanes are offset by 4 to skew the two halves of each 8-element column, and addresses clamp to 127 (a zero entry) outside the valid window.

- **`rtl/systolic.v`** — Module `systolic`. The compute core. Holds `weight_queue`/`data_queue` (8×8, shift each cycle from the 32-bit SRAM words) and `matrix_mul_2D` accumulators. Per cycle, PEs on the active anti-diagonal (selected via `cycle_num` relative to `FIRST_OUT`/`PARALLEL_START`) compute `weight*data` and accumulate. `mul_outcome` (8 × 21-bit, packed) outputs the diagonal selected by `matrix_index` (upper/lower-bound logic handles the two triangular halves). Feeds `ori_data` into `quantize`.

- **`rtl/quantize.v`** — Module `quantize`. Pure combinational. For each of the 8 lanes, saturates the 21-bit accumulator to [−32768, 32767] and outputs 16-bit `quantized_data`. Sits between `systolic` and `write_out`.

- **`rtl/write_out.v`** — Module `write_out`. Output stage. Registers `quantized_data` and steers it to result SRAM a, b, or c with computed `sram_waddr_*` and active-low `sram_write_enable_*0`, based on `data_set` (0 vs 1) and whether `matrix_index` is below `ARRAY_SIZE` (pure diagonal) or above ("mix type" spanning two output rows). This is the most case-heavy module — the index arithmetic encodes how each diagonal of results maps into the output matrices.

**Interaction flow:** `systolic_controll` drives `addr_sel` (→ SRAM read addrs) and `systolic` (load/MAC) → `systolic` produces wide results → `quantize` narrows them → `write_out` (also timed by `systolic_controll`) stores them to the three output SRAMs; `tpu_done` signals completion.
